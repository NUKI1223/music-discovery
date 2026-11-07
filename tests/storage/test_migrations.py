import json
import sqlite3
from unittest.mock import patch

import pytest

from discovery import storage
from discovery.storage import SCHEMA_VERSION, Store, catalog_digest

from .test_journal import committed, sample


def versions(path):
    with sqlite3.connect(path) as conn:
        return [r[0] for r in conn.execute("SELECT version FROM schema_version ORDER BY version")]


def test_fresh_database_is_wal_with_current_version_and_no_backup(tmp_path):
    path = tmp_path / "a.sqlite3"
    committed(Store(path))
    assert versions(path) == list(range(1, SCHEMA_VERSION + 1))
    assert sqlite3.connect(path).execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert not list(tmp_path.glob("*.bak"))
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT is_fixture FROM catalog_meta").fetchall() == [(1,)]


def test_unknown_future_version_is_rejected(tmp_path):
    path = tmp_path / "a.sqlite3"
    Store(path)
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO schema_version VALUES (?)", (SCHEMA_VERSION + 1,))
    storage._INITIALIZED.clear()
    with pytest.raises(RuntimeError, match="более новой"):
        Store(path)


def test_v1_database_migrates_with_backup_and_keeps_payloads(tmp_path):
    path = tmp_path / "a.sqlite3"
    catalog = sample()
    with sqlite3.connect(path) as conn:
        for statement in storage._SCHEMA_V1:
            conn.execute(statement)
        conn.execute("INSERT INTO schema_version VALUES (1)")
        conn.execute("INSERT INTO catalogs VALUES (?,?,?)",
                     (catalog_digest(catalog), "snapshot", catalog.model_dump_json()))
    store = Store(path)
    assert versions(path) == [1, 2]
    bak = tmp_path / "a.sqlite3.pre-v2.bak"
    assert bak.exists() and versions(bak) == [1]
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT is_fixture FROM catalog_meta").fetchall() == [(1,)]
        assert json.loads(conn.execute("SELECT payload FROM catalogs").fetchone()[0])["is_fixture"]
    assert store.get_catalog(catalog_digest(catalog)) == catalog


def test_existing_backup_is_not_overwritten(tmp_path):
    path = tmp_path / "a.sqlite3"
    with sqlite3.connect(path) as conn:
        for statement in storage._SCHEMA_V1:
            conn.execute(statement)
        conn.execute("INSERT INTO schema_version VALUES (1)")
    (tmp_path / "a.sqlite3.pre-v2.bak").write_text("keep")
    Store(path)
    assert (tmp_path / "a.sqlite3.pre-v2.bak").read_text() == "keep"


def test_init_runs_once_per_path_and_commit_uses_catalog_meta(tmp_path):
    path = tmp_path / "a.sqlite3"
    committed(Store(path))
    with patch.object(storage, "_migrate", side_effect=AssertionError("re-init")):
        store = Store(path)
        assert store.decisions()
    with sqlite3.connect(path) as conn:  # payload parsing must not be needed for the check
        conn.execute("UPDATE catalogs SET payload='not json'")
    real = sample().model_copy(update={"is_fixture": False})
    with pytest.raises(ValueError, match="separate databases"):
        Store(path).commit_decision("x", real, None, lambda: None)


def test_catalog_digest_is_cached_and_tracks_top_level_changes():
    catalog = sample()
    first = catalog_digest(catalog)
    with patch.object(type(catalog), "model_dump", side_effect=AssertionError("recomputed")):
        assert catalog_digest(catalog) == first
    catalog.snapshot_id = "other"
    assert catalog_digest(catalog) != first


def test_preexisting_v1_database_is_backed_up_then_switched_to_wal(tmp_path):
    import sqlite3

    from discovery import storage
    path = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(path)
    for statement in storage.MIGRATIONS[0]:
        conn.execute(statement)
    conn.execute("INSERT OR IGNORE INTO schema_version VALUES (1)")
    conn.commit()
    conn.close()
    storage.Store(path)
    backup = path.with_name(path.name + f".pre-v{storage.SCHEMA_VERSION}.bak")
    assert backup.exists()
    with sqlite3.connect(backup) as old:
        assert old.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal"
    with sqlite3.connect(path) as new:
        assert new.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
