"""SQLite journal for immutable catalog/decision snapshots and append-only feedback."""

import hashlib
import json
import sqlite3
import threading
import weakref
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from discovery.contracts import Catalog, Decision, Feedback, RequestIntent, UserProfile


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


_DIGESTS: dict[int, tuple[weakref.ref, tuple, str]] = {}


def _fingerprint(catalog: Catalog) -> tuple:
    # Cheap guard against stale digests when callers rewrite top-level fields or lists.
    return (catalog.snapshot_id, catalog.taxonomy_version, catalog.is_fixture,
            id(catalog.genres), id(catalog.artists), id(catalog.releases),
            len(catalog.genres), len(catalog.artists), len(catalog.releases))


def catalog_digest(catalog: Catalog) -> str:
    """Digest computed once per catalog object (it serialises the whole catalog)."""
    key = id(catalog)
    fingerprint = _fingerprint(catalog)
    cached = _DIGESTS.get(key)
    if cached and cached[0]() is catalog and cached[1] == fingerprint:
        return cached[2]
    value = digest(json.dumps(catalog.model_dump(mode="json"), sort_keys=True))
    try:
        _DIGESTS[key] = (weakref.ref(catalog, lambda _, k=key: _DIGESTS.pop(k, None)),
                         fingerprint, value)
    except TypeError:  # not weak-referenceable
        pass
    return value


def database_path(path: str | Path, catalog: Catalog) -> Path:
    """Demo history has a separate database even when the caller uses defaults."""
    path = Path(path)
    if catalog.is_fixture and not path.stem.endswith("-demo"):
        return path.with_name(path.stem + "-demo" + path.suffix)
    return path


_SCHEMA_V1 = (
    "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)",
    """CREATE TABLE IF NOT EXISTS catalogs (
        hash TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL, payload TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS decisions (
        id TEXT PRIMARY KEY, catalog_hash TEXT NOT NULL REFERENCES catalogs(hash),
        payload TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS impressions (
        decision_id TEXT NOT NULL REFERENCES decisions(id), release_id TEXT NOT NULL,
        shown_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY(decision_id, release_id)
    )""",
    """CREATE TABLE IF NOT EXISTS feedback (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
        decision_id TEXT NOT NULL REFERENCES decisions(id), payload TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS revoked_feedback (
        id TEXT PRIMARY KEY REFERENCES feedback(id),
        revoked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS hidden_collection (
        release_id TEXT PRIMARY KEY,
        through_seq INTEGER NOT NULL,
        exclude_from_recs INTEGER NOT NULL
    )""",
    "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, payload TEXT NOT NULL)",
)
# v2: a small index of catalog snapshots so the fixture/real check never parses payloads.
# The trigger keeps it correct for any writer (including legacy import); payloads stay in place.
_SCHEMA_V2 = (
    """CREATE TABLE IF NOT EXISTS catalog_meta (
        hash TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL, is_fixture INTEGER NOT NULL
    )""",
    """INSERT OR IGNORE INTO catalog_meta
        SELECT hash, snapshot_id, coalesce(json_extract(payload,'$.is_fixture'),0) FROM catalogs""",
    """CREATE TRIGGER IF NOT EXISTS catalogs_meta_insert AFTER INSERT ON catalogs BEGIN
        INSERT OR IGNORE INTO catalog_meta VALUES (
            new.hash, new.snapshot_id, coalesce(json_extract(new.payload,'$.is_fixture'),0));
    END""",
)
MIGRATIONS: tuple[tuple[str, ...], ...] = (_SCHEMA_V1, _SCHEMA_V2)
SCHEMA_VERSION = len(MIGRATIONS)

_INIT_LOCK = threading.Lock()
_INITIALIZED: set[tuple[str, int, int]] = set()


def _file_key(path: Path) -> tuple[str, int, int] | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return (str(path.resolve()), stat.st_dev, stat.st_ino)


def _current_version(conn: sqlite3.Connection) -> int:
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='schema_version'").fetchone():
        return 0
    return conn.execute("SELECT coalesce(max(version),0) FROM schema_version").fetchone()[0]


def _migrate(path: Path) -> None:
    """Bring the account database to SCHEMA_VERSION in one transaction."""
    conn = sqlite3.connect(path, timeout=15, isolation_level=None)
    try:
        current = _current_version(conn)
        if current > SCHEMA_VERSION:
            raise RuntimeError(
                f"База {path.name} создана более новой версией приложения "
                f"(схема v{current}, поддерживается до v{SCHEMA_VERSION}); обновите приложение.")
        if 0 < current < SCHEMA_VERSION:
            bak = path.with_name(f"{path.name}.pre-v{SCHEMA_VERSION}.bak")
            if not bak.exists():
                dest = sqlite3.connect(bak)
                try:
                    conn.backup(dest)
                finally:
                    dest.close()
        # Persistent in the file: new and pre-existing account databases (after the
        # backup above) let readers proceed while a recommendation holds the write lock.
        try:
            if conn.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
                conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            pass  # another process holds the file; keep the old journal mode, retry next start
        if current == SCHEMA_VERSION:
            return
        conn.execute("BEGIN IMMEDIATE")
        try:
            current = _current_version(conn)  # another process may have migrated meanwhile
            if current > SCHEMA_VERSION:
                raise RuntimeError("Схема базы изменилась во время миграции.")
            for version in range(current + 1, SCHEMA_VERSION + 1):
                for statement in MIGRATIONS[version - 1]:
                    conn.execute(statement)
                conn.execute("INSERT OR IGNORE INTO schema_version VALUES (?)", (version,))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


class Store:
    def __init__(self, path: str | Path = "runtime/discovery.sqlite3"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if _file_key(self.path) in _INITIALIZED:
            return
        with _INIT_LOCK:
            _migrate(self.path)
            _INITIALIZED.add(_file_key(self.path))

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=NORMAL")  # per connection; safe with WAL
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def get_decision(self, decision_id: str) -> Decision | None:
        with self.connection() as conn:
            row = conn.execute("SELECT payload FROM decisions WHERE id=?", (decision_id,)).fetchone()
        return Decision.model_validate_json(row[0]) if row else None

    def commit_decision(
        self, decision_id: str, catalog: Catalog, request: RequestIntent,
        factory: Callable[[], Decision],
    ) -> Decision:
        """Hold the write lock through sampling: retries never draw a second action."""
        hashed = catalog_digest(catalog)
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM catalog_meta WHERE is_fixture!=? LIMIT 1",
                            (int(catalog.is_fixture),)).fetchone():
                raise ValueError("Fixture and real history must use separate databases")
            row = conn.execute("SELECT payload FROM decisions WHERE id=?", (decision_id,)).fetchone()
            if row:
                existing = Decision.model_validate_json(row[0])
                if existing.request != request or existing.catalog_hash != hashed:
                    raise ValueError("request ID already belongs to a different request/catalog")
                return existing
            decision = factory()
            if decision.id != decision_id or decision.catalog_hash != hashed:
                raise ValueError("decision identity/snapshot mismatch")
            if not conn.execute("SELECT 1 FROM catalogs WHERE hash=?", (hashed,)).fetchone():
                conn.execute("INSERT INTO catalogs VALUES (?,?,?)",
                             (hashed, catalog.snapshot_id, catalog.model_dump_json()))
            conn.execute("INSERT INTO decisions VALUES (?,?,?)",
                         (decision.id, hashed, decision.model_dump_json()))
            return decision

    def mark_shown(self, decision: Decision) -> None:
        with self.connection() as conn:
            row = conn.execute("SELECT payload FROM decisions WHERE id=?", (decision.id,)).fetchone()
            if row is None or Decision.model_validate_json(row[0]) != decision:
                raise ValueError("impression must match the immutable stored decision")
            for item in decision.result.items:
                conn.execute("INSERT OR IGNORE INTO impressions(decision_id,release_id) VALUES (?,?)",
                             (decision.id, item.release_id))

    def append_feedback(self, feedback: Feedback) -> bool:
        decision = self.get_decision(feedback.decision_id)
        if decision is None or feedback.release_id not in {
            item.release_id for item in decision.result.items
        }:
            raise ValueError("feedback must refer to a release in a stored decision")
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT payload FROM feedback WHERE id=?", (feedback.id,)).fetchone()
            if row:
                if Feedback.model_validate_json(row[0]) != feedback:
                    raise ValueError("feedback idempotency key reused with different content")
                return False
            conn.execute("INSERT INTO feedback(id,decision_id,payload) VALUES (?,?,?)",
                         (feedback.id, feedback.decision_id, feedback.model_dump_json()))
        return True

    def feedback(self) -> list[Feedback]:
        with self.connection() as conn:
            rows = conn.execute("SELECT payload FROM feedback WHERE id NOT IN "
                                "(SELECT id FROM revoked_feedback) ORDER BY seq").fetchall()
        return [Feedback.model_validate_json(row[0]) for row in rows]

    @staticmethod
    def _collection_release_ids(conn) -> set[str]:
        return {row[0] for row in conn.execute("""SELECT DISTINCT json_extract(f.payload,'$.release_id')
            FROM feedback f WHERE f.id NOT IN (SELECT id FROM revoked_feedback)
            AND json_extract(f.payload,'$.kind') IN ('known','save_for_later','listened_like','listened_mixed','listened_dislike')
            AND NOT EXISTS (SELECT 1 FROM hidden_collection h
                WHERE h.release_id=json_extract(f.payload,'$.release_id') AND h.through_seq>=f.seq)""")}

    def collection_release_ids(self) -> set[str]:
        with self.connection() as conn:
            return self._collection_release_ids(conn)

    def remove_from_collection(self, release_id: str | None = None, *, forget: bool = False) -> bool:
        """Hide albums or revoke their feedback, retaining consumed idempotency keys.

        Effective profile, exports and future training all use feedback(), so a
        reset cannot silently keep old learning labels. Hiding retains taste but
        excludes even saved-for-later albums from future recommendations.
        """
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute("SELECT payload FROM feedback WHERE id NOT IN "
                                "(SELECT id FROM revoked_feedback)").fetchall()
            events = [Feedback.model_validate_json(row[0]) for row in rows]
            targets = self._collection_release_ids(conn)
            if release_id is not None:
                targets &= {release_id}
            if not targets:
                return False
            boundary = conn.execute("SELECT coalesce(max(seq),0) FROM feedback").fetchone()[0]
            conn.executemany("""INSERT INTO hidden_collection VALUES (?,?,?)
                ON CONFLICT(release_id) DO UPDATE SET through_seq=excluded.through_seq,
                exclude_from_recs=excluded.exclude_from_recs""",
                [(rid, boundary, int(not forget)) for rid in targets])
            if not forget:
                return True
            conn.executemany("INSERT OR IGNORE INTO revoked_feedback(id) VALUES (?)",
                             [(event.id,) for event in events if event.release_id in targets])
            row = conn.execute("SELECT payload FROM settings WHERE key='profile'").fetchone()
            if row:
                profile = UserProfile.model_validate_json(row[0])
                profile.known_release_ids = [rid for rid in profile.known_release_ids if rid not in targets]
                for mapping in (profile.liked_release_ids_by_genre, profile.disliked_release_ids_by_genre):
                    for scope, ids in mapping.items():
                        mapping[scope] = [rid for rid in ids if rid not in targets]
                conn.execute("UPDATE settings SET payload=? WHERE key='profile'", (profile.model_dump_json(),))
        return True

    def decisions(self) -> list[Decision]:
        with self.connection() as conn:
            rows = conn.execute("SELECT payload FROM decisions ORDER BY rowid").fetchall()
        return [Decision.model_validate_json(row[0]) for row in rows]

    def get_catalog(self, hashed: str) -> Catalog:
        with self.connection() as conn:
            row = conn.execute("SELECT payload FROM catalogs WHERE hash=?", (hashed,)).fetchone()
        if row is None:
            raise ValueError("catalog snapshot not found")
        return Catalog.model_validate_json(row[0])

    def set_base_profile(self, profile: UserProfile) -> None:
        with self.connection() as conn:
            conn.execute("INSERT INTO settings VALUES ('profile',?) "
                         "ON CONFLICT(key) DO UPDATE SET payload=excluded.payload",
                         (profile.model_dump_json(),))

    def profile(self, default: UserProfile | None = None) -> UserProfile:
        with self.connection() as conn:
            row = conn.execute("SELECT payload FROM settings WHERE key='profile'").fetchone()
        profile = (UserProfile.model_validate_json(row[0]) if row else
                   (default or UserProfile()).model_copy(deep=True))
        known = set(profile.known_release_ids)
        with self.connection() as conn:
            scopes = dict(conn.execute(
                "SELECT id, json_extract(payload,'$.request.genre_scope_id') FROM decisions"))
            known.update(row[0] for row in conn.execute(
                "SELECT release_id FROM hidden_collection WHERE exclude_from_recs=1"))
        latest: dict[tuple[str, str], str] = {}
        events = self.feedback()
        for event in events:
            scope = scopes[event.decision_id]
            if event.kind in {"known", "listened_like", "listened_mixed", "listened_dislike"}:
                known.add(event.release_id)
            if event.familiar_before_recommendation is True:
                known.add(event.release_id)
            if event.kind in {"listened_like", "listened_mixed", "listened_dislike"}:
                latest[scope, event.release_id] = event.kind
        for (scope, rid), kind in latest.items():
            likes = set(profile.liked_release_ids_by_genre.get(scope, []))
            dislikes = set(profile.disliked_release_ids_by_genre.get(scope, []))
            likes.discard(rid)
            dislikes.discard(rid)
            if kind == "listened_like":
                likes.add(rid)
            elif kind == "listened_dislike":
                dislikes.add(rid)
            profile.liked_release_ids_by_genre[scope] = sorted(likes)
            profile.disliked_release_ids_by_genre[scope] = sorted(dislikes)
        profile.known_release_ids = sorted(known)
        profile.version = digest(profile.model_dump_json() + "|".join(e.id for e in events))[:16]
        return profile

    def export(self) -> dict:
        with self.connection() as conn:
            catalogs = [json.loads(r[0]) for r in conn.execute("SELECT payload FROM catalogs")]
            impressions = [dict(zip(("decision_id", "release_id", "shown_at"), row))
                           for row in conn.execute("SELECT * FROM impressions")]
            row = conn.execute("SELECT payload FROM settings WHERE key='profile'").fetchone()
        return {
            "schema_version": 1, "catalogs": catalogs,
            "decisions": [d.model_dump(mode="json") for d in self.decisions()],
            "feedback": [f.model_dump(mode="json") for f in self.feedback()],
            "base_profile": json.loads(row[0]) if row else UserProfile().model_dump(mode="json"),
            "impressions": impressions,
        }

    def backup(self, target: str | Path) -> None:
        target = Path(target)
        if target.exists():
            raise ValueError("backup target already exists")
        target.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as source:
            dest = sqlite3.connect(target)
            try:
                source.backup(dest)
            finally:
                dest.close()
