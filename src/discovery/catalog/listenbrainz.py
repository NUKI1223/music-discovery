"""ListenBrainz listener counts as a dated popularity source for an existing catalogue.

Importing this module never makes network requests. ``match`` finds MusicBrainz
release-group IDs for releases that lack one (slow, resumable); ``listeners``
fetches listener counts for every known ID (fast; repeat within 90 days).
MusicBrainz core identifiers and ListenBrainz listen statistics are CC0.
See docs/listenbrainz-popularity.md.
"""

import argparse
import json
import re
import signal
import tempfile
import time
import unicodedata
import warnings
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote
from uuid import UUID

import httpx

from discovery.config import DEFAULT_CATALOG, PROJECT_ROOT
from discovery.contracts import Catalog, Observation, Release

from .io import load_catalog
from .musicbrainz import MusicBrainzClient

SOURCE = "listenbrainz"
IDS_NAME = "musicbrainz-ids.json"
OVERLAY_NAME = "listenbrainz.json"
MATCH_METHOD = "exact-normalized-title-artist-credit-year-v2"
MAX_AGE_DAYS = 90  # keep equal to the ranker's MAX_OBSERVATION_AGE_DAYS
ACCESS_BASIS = (
    "ListenBrainz public API (popularity/release-group); listen statistics are CC0. "
    "Counts are ListenBrainz users, not all listeners. https://listenbrainz.org/data/"
)
USER_AGENT = "MusicDiscovery/0.1 (local noncommercial learning prototype)"
TITLES_PER_QUERY = 8
MAX_QUERY_BYTES = 4000
MBIDS_PER_REQUEST = 500
CHECKPOINT_REQUESTS = 50
FAILURE_PAUSES = (5.0, 15.0, 60.0, 300.0)  # seconds after the 1st..4th consecutive failure
TYPE_RANK = {"Album": 0, "EP": 1}
_BRACKETED = re.compile(r"^(.+?)\s*\[(.+)\]$")


def normalize(text: str) -> str:
    """Case-, accent- and punctuation-insensitive key; symbol-only names keep their symbols."""
    folded = unicodedata.normalize("NFKD", text).casefold()
    # Only Latin-style accents (U+0300..U+036F) go: kana voicing marks change the word.
    stripped = "".join(c for c in folded if not 0x300 <= ord(c) <= 0x36F)
    plain = re.sub(r"[^\w]+", "", unicodedata.normalize("NFC", stripped))
    return plain or " ".join(text.casefold().split())


def artist_names(name: str) -> list[str]:
    """Catalogue spelling first; «米津玄師 [Kenshi Yonezu]» also yields both of its parts."""
    found = _BRACKETED.match(name.strip())
    return [name, found[1], found[2]] if found else [name]


def _mbid(value: object) -> str | None:
    try:
        return str(UUID(value)) if isinstance(value, str) and len(value) == 36 else None
    except ValueError:
        return None


def _phrase(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def build_query(artist: str, titles: list[str]) -> str:
    wanted = " OR ".join(f'releasegroup:"{_phrase(title)}"' for title in dict.fromkeys(titles))
    return f'artist:"{_phrase(artist)}" AND ({wanted})'


def _credit_names(row: dict) -> tuple[set[str], set[str]]:
    """Normalized whole-credit spellings, and the names of individual credited artists."""
    raw = row.get("artist-credit")
    credits = [
        c
        for c in (raw if isinstance(raw, list) else [])
        if isinstance(c, dict) and isinstance(c.get("artist") or {}, dict)
    ]
    canonical = [str((c.get("artist") or {}).get("name") or "") for c in credits]
    credited = [str(c.get("name") or name) for c, name in zip(credits, canonical, strict=True)]
    joined = "".join(
        name + str(c.get("joinphrase") or "") for c, name in zip(credits, credited, strict=True)
    )
    spellings = (joined, ", ".join(credited), ", ".join(canonical))
    whole = {normalize(value) for value in spellings if value.strip()}
    single = {normalize(name) for name in credited + canonical if name.strip()}
    return whole, single


def _year_fits(row: dict, year: int) -> bool:
    prefix = str(row.get("first-release-date") or "")[:4]
    return not (len(prefix) == 4 and prefix.isdecimal()) or abs(int(prefix) - year) <= 1


def choose_release_group(
    title: str, artist: str, year: int | None, rows: list
) -> tuple[str | None, str]:
    """Exact title and artist only. Several equally good candidates stay unmatched."""
    if not title.strip():
        return None, "absent"
    wanted_title = normalize(title)
    wanted_artists = {normalize(name) for name in artist_names(artist)}
    whole_matches, credit_matches = [], []
    for row in rows:
        if (
            not isinstance(row, dict)
            or _mbid(row.get("id")) is None
            or normalize(str(row.get("title") or "")) != wanted_title
        ):
            continue
        whole, single = _credit_names(row)
        if wanted_artists & whole:
            whole_matches.append(row)
        elif wanted_artists & single:
            credit_matches.append(row)
    if year is not None:
        # A known release year far from the candidate's rules it out; undated candidates stay.
        whole_matches = [row for row in whole_matches if _year_fits(row, year)]
        credit_matches = [row for row in credit_matches if _year_fits(row, year)]
    candidates = whole_matches or credit_matches
    if not candidates:
        return None, "absent"
    if year is not None:
        same_year = [
            row for row in candidates if str(row.get("first-release-date") or "")[:4] == str(year)
        ]
        candidates = same_year or candidates

    def rank(row: dict) -> tuple[int, bool]:
        primary = row.get("primary-type")
        return (
            TYPE_RANK.get(primary, 2) if isinstance(primary, str) else 2,
            bool(row.get("secondary-types")),
        )

    best = min(rank(row) for row in candidates)
    ids = {_mbid(row["id"]) for row in candidates if rank(row) == best}
    return (ids.pop(), "matched") if len(ids) == 1 else (None, "ambiguous")


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def load_ids(path: Path, catalog: Catalog) -> dict:
    """Matching state of one snapshot; a file of another snapshot is never reused."""
    if not path.exists():
        return {
            "snapshot_id": catalog.snapshot_id,
            "source": "https://musicbrainz.org/",
            "license": "CC0 (MusicBrainz core data)",
            "method": MATCH_METHOD,
            "matched": {},
            "unmatched": {},
        }
    state = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(state, dict)
        or state.get("snapshot_id") != catalog.snapshot_id
        or not isinstance(state.get("matched"), dict)
        or not isinstance(state.get("unmatched"), dict)
    ):
        raise ValueError(f"{path} does not belong to catalogue snapshot {catalog.snapshot_id}")
    if state.get("method") != MATCH_METHOD:
        raise ValueError(
            f"{path} was created by another matching method ({state.get('method')!r}, "
            f"current {MATCH_METHOD!r}); rename or delete it and run match again"
        )
    return state


def release_group_ids(catalog: Catalog, state: dict) -> dict[str, str]:
    """Release ID → release-group ID: matched ones plus releases whose own ID is one."""
    matched = state.get("matched", {})
    found = {}
    for release in catalog.releases:
        mbid = _mbid(release.id) or _mbid(matched.get(release.id))
        if mbid:
            found[release.id] = mbid
    return found


def _batches(artist: str, releases: list[Release]) -> list[list[Release]]:
    batches: list[list[Release]] = []
    for release in releases:
        current = batches[-1] if batches else None
        if (
            current is not None
            and len(current) < TITLES_PER_QUERY
            and len(quote(build_query(artist, [r.title for r in current] + [release.title])))
            <= MAX_QUERY_BYTES
        ):
            current.append(release)
        else:
            batches.append([release])
    return batches


def match_catalog(
    catalog: Catalog,
    ids_path: Path,
    client: MusicBrainzClient,
    *,
    max_requests: int | None = None,
    progress=None,
    sleep=time.sleep,
) -> dict:
    """Search MusicBrainz artist by artist; every outcome is checkpointed for resumption.

    A failed batch is left unrecorded and followed by a growing pause (FAILURE_PAUSES);
    the fifth consecutive failure stops the run.
    """
    state = load_ids(ids_path, catalog)
    matched, unmatched = state["matched"], state["unmatched"]
    names = {artist.id: artist.name for artist in catalog.artists}
    groups: dict[tuple[str, ...], list[Release]] = {}
    for release in catalog.releases:
        if (
            release.artist_ids
            and release.title.strip()
            and _mbid(release.id) is None
            and release.id not in matched
            and release.id not in unmatched
        ):
            groups.setdefault(tuple(release.artist_ids), []).append(release)
    started = client.network_requests
    saved_at = started
    failures = 0

    def used() -> int:
        return client.network_requests - started

    def save() -> None:
        state["updated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        _write_json(ids_path, state)

    def report(phase: str) -> dict:
        counts = {
            "phase": phase,
            "requests_this_run": used(),
            "matched": len(matched),
            "absent": sum(v == "absent" for v in unmatched.values()),
            "ambiguous": sum(v == "ambiguous" for v in unmatched.values()),
            "query_rejected": sum(v == "query_rejected" for v in unmatched.values()),
            "remaining": sum(
                r.id not in matched and r.id not in unmatched
                for group in groups.values()
                for r in group
            ),
        }
        if progress:
            progress(counts)
        return counts

    def search(artist: str, batch: list[Release]) -> tuple[list, bool] | None:
        """Rows and truncation flag; None when the query itself was rejected."""
        nonlocal failures
        # A bracketed transliteration is not part of the MusicBrainz name: search the first part.
        query = build_query(artist_names(artist)[-2:][0], [r.title for r in batch])
        try:
            payload = client.search_release_groups(query, limit=100)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in {400, 404, 414}:
                raise
            return None
        rows = payload.get("release-groups") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise ValueError("unexpected MusicBrainz search response")
        failures = 0
        count = payload.get("count")
        return rows, isinstance(count, int) and count > len(rows)

    def resolve(artist: str, batch: list[Release]) -> None:
        result = search(artist, batch)
        retry = []
        for release in batch:
            if result is None:
                if len(batch) > 1:
                    retry.append(release)
                else:
                    unmatched[release.id] = "query_rejected"
                continue
            rows, truncated = result
            mbid, outcome = choose_release_group(release.title, artist, release.year, rows)
            if mbid:
                matched[release.id] = mbid
            elif outcome == "absent" and truncated and len(batch) > 1:
                retry.append(release)  # a crowded page may have pushed this title out
            else:
                unmatched[release.id] = outcome
        for release in retry:
            resolve(artist, [release])

    try:
        for artist_ids, releases in groups.items():
            artist = ", ".join(names[artist_id] for artist_id in artist_ids)
            for batch in _batches(artist, releases):
                if max_requests is not None and used() >= max_requests:
                    return report("paused")
                try:
                    resolve(artist, batch)
                except (httpx.HTTPError, ValueError) as exc:
                    # Left unrecorded on purpose: the next run retries this batch.
                    failures += 1
                    if failures > len(FAILURE_PAUSES):
                        raise RuntimeError(
                            "five consecutive MusicBrainz failures after pauses; rerun to resume"
                        ) from exc
                    sleep(FAILURE_PAUSES[failures - 1])
                if client.network_requests - saved_at >= CHECKPOINT_REQUESTS:
                    saved_at = client.network_requests
                    save()
                    report("matching")
        return report("complete")
    finally:
        save()


class ListenBrainzClient:
    """Batched popularity reader. ``transport`` and ``sleep`` support offline tests."""

    def __init__(self, *, transport: httpx.BaseTransport | None = None, sleep=time.sleep):
        self.client = httpx.Client(
            base_url="https://api.listenbrainz.org/1/",
            timeout=60,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            transport=transport,
        )
        self.sleep = sleep
        self.network_requests = 0

    def close(self) -> None:
        self.client.close()

    def release_group_listeners(self, mbids: list[str]) -> dict[str, int]:
        """Release-group ID → distinct listeners. Absent, null and zero rows stay unknown."""
        if not 1 <= len(mbids) <= MBIDS_PER_REQUEST:
            raise ValueError(f"1..{MBIDS_PER_REQUEST} release-group IDs required")
        for attempt in range(4):
            self.network_requests += 1
            try:
                response = self.client.post(
                    "popularity/release-group", json={"release_group_mbids": mbids}
                )
            except httpx.TransportError:
                if attempt == 3:
                    raise
                self.sleep(2.0 * (attempt + 1))
                continue
            try:
                reset = min(max(float(response.headers.get("X-RateLimit-Reset-In", "")), 0.0), 60.0)
            except ValueError:
                reset = None
            if response.status_code in {429, 502, 503, 504} and attempt < 3:
                self.sleep(reset if reset is not None else 2.0 * (attempt + 1))
                continue
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                raise ValueError("unexpected ListenBrainz API response")
            if response.headers.get("X-RateLimit-Remaining") == "0" and reset is not None:
                self.sleep(reset)
            wanted = set(mbids)
            listeners = {}
            for row in payload:
                users = row.get("total_user_count") if isinstance(row, dict) else None
                if (
                    isinstance(users, int)
                    and not isinstance(users, bool)
                    and users > 0
                    and row.get("release_group_mbid") in wanted
                ):
                    listeners[row["release_group_mbid"]] = users
            return listeners
        raise AssertionError("unreachable")


def collect_listeners(
    catalog: Catalog, state: dict, client: ListenBrainzClient, *, progress=None
) -> dict:
    """One dated overlay for the snapshot. An unknown count is omitted, never zero."""
    by_release = release_group_ids(catalog, state)
    mbids = sorted(set(by_release.values()))
    listeners: dict[str, int] = {}
    for start in range(0, len(mbids), MBIDS_PER_REQUEST):
        listeners.update(client.release_group_listeners(mbids[start : start + MBIDS_PER_REQUEST]))
        if progress:
            progress(
                {
                    "phase": "listeners",
                    "requested": min(start + MBIDS_PER_REQUEST, len(mbids)),
                    "total": len(mbids),
                    "with_listeners": len(listeners),
                }
            )
    return {
        "snapshot_id": catalog.snapshot_id,
        "source": "https://listenbrainz.org/",
        "license": "CC0 (ListenBrainz listen statistics)",
        "observed_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "count_semantics": "distinct ListenBrainz users with at least one listen of the release group",
        "release_groups_requested": len(mbids),
        "listeners": {
            release_id: listeners[mbid]
            for release_id, mbid in by_release.items()
            if mbid in listeners
        },
    }


def apply_listeners(catalog: Catalog, overlay: dict) -> Catalog:
    """Copy of the catalogue with one ``listenbrainz`` observation per known release."""
    if overlay.get("snapshot_id") != catalog.snapshot_id:
        raise ValueError("listener overlay belongs to another catalogue snapshot")
    observed_at = datetime.fromisoformat(overlay["observed_at"])
    listeners = overlay["listeners"]
    if not isinstance(listeners, dict):
        raise ValueError("listener overlay has no listeners mapping")
    releases = []
    for release in catalog.releases:
        users = listeners.get(release.id)
        if not isinstance(users, int) or isinstance(users, bool) or users <= 0:
            releases.append(release)
            continue
        # No URL: cards turn observation URLs into links, and this is not an album page.
        observation = Observation(
            source=SOURCE, observed_at=observed_at, rating_count=users, access_basis=ACCESS_BASIS
        )
        releases.append(
            release.model_copy(update={"observations": [*release.observations, observation]})
        )
    return catalog.model_copy(update={"releases": releases})


def load_with_listeners(path: str | Path) -> Catalog:
    """``load_catalog`` plus the listener overlay stored next to it, when one exists."""
    path = Path(path)
    catalog = load_catalog(path)
    overlay_path = path.with_name(OVERLAY_NAME)
    if not overlay_path.exists():
        return catalog
    try:
        overlay = json.loads(overlay_path.read_text(encoding="utf-8"))
        if not isinstance(overlay, dict) or overlay.get("snapshot_id") != catalog.snapshot_id:
            warnings.warn(
                f"{overlay_path} belongs to another catalogue snapshot and is ignored",
                stacklevel=2,
            )
            return catalog
        enriched = apply_listeners(catalog, overlay)
        age_days = (datetime.now(UTC) - datetime.fromisoformat(overlay["observed_at"])).days
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        warnings.warn(
            f"{overlay_path} is unreadable or incomplete and is ignored: {exc!r}", stacklevel=2
        )
        return catalog
    if age_days > MAX_AGE_DAYS:
        warnings.warn(
            f"{overlay_path} is {age_days} days old; the ranker does not use data older than "
            f"{MAX_AGE_DAYS} days. Run: python -m discovery.catalog.listenbrainz listeners",
            stacklevel=2,
        )
    return enriched


def status(catalog: Catalog, ids_path: Path, overlay_path: Path) -> dict:
    state = load_ids(ids_path, catalog)
    known = release_group_ids(catalog, state)
    report = {
        "snapshot_id": catalog.snapshot_id,
        "releases": len(catalog.releases),
        "with_release_group_id": len(known),
        "unmatched": len(state["unmatched"]),
        "not_searched_yet": sum(
            r.id not in known and r.id not in state["unmatched"] for r in catalog.releases
        ),
        "listeners_collected": False,
    }
    if overlay_path.exists():
        try:
            overlay = json.loads(overlay_path.read_text(encoding="utf-8"))
            observed_at = datetime.fromisoformat(overlay["observed_at"])
            age_days = (datetime.now(UTC) - observed_at).days
            report.update(
                listeners_collected=True,
                observed_at=overlay["observed_at"],
                releases_with_listeners=len(overlay["listeners"]),
                age_days=age_days,
                stale=age_days > MAX_AGE_DAYS,
                overlay_matches_snapshot=overlay.get("snapshot_id") == catalog.snapshot_id,
            )
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            report.update(listeners_collected=False, overlay_error=repr(exc))
    return report


def _print(row: dict) -> None:
    print(json.dumps(row, ensure_ascii=False), flush=True)


def _interrupt(*_args) -> None:
    raise KeyboardInterrupt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["match", "listeners", "status"])
    parser.add_argument("--catalog", type=Path, default=Path(DEFAULT_CATALOG))
    parser.add_argument("--ids", type=Path, help=f"default: {IDS_NAME} next to the catalogue")
    parser.add_argument("--output", type=Path, help=f"default: {OVERLAY_NAME} next to the catalogue")
    parser.add_argument(
        "--max-requests", type=int, help="stop matching after this many MusicBrainz requests"
    )
    args = parser.parse_args()
    catalog_path = args.catalog if args.catalog.is_absolute() else PROJECT_ROOT / args.catalog
    ids_path = args.ids or catalog_path.with_name(IDS_NAME)
    overlay_path = args.output or catalog_path.with_name(OVERLAY_NAME)
    catalog = load_catalog(catalog_path)
    if args.command == "status":
        _print(status(catalog, ids_path, overlay_path))
        return 0
    if args.command == "listeners":
        client = ListenBrainzClient()
        try:
            overlay = collect_listeners(
                catalog, load_ids(ids_path, catalog), client, progress=_print
            )
        finally:
            client.close()
        _write_json(overlay_path, overlay)
        _print(
            {
                "phase": "complete",
                "output": str(overlay_path),
                "observed_at": overlay["observed_at"],
                "releases_with_listeners": len(overlay["listeners"]),
            }
        )
        return 0
    import fcntl  # Unix only; the site imports this module at start-up

    # MusicBrainz allows one request per second for the whole application.
    lock = (Path(tempfile.gettempdir()) / "music-discovery-musicbrainz.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise SystemExit("another MusicBrainz matching run is already active") from None
    for name in ("SIGTERM", "SIGHUP"):
        number = getattr(signal, name, None)
        if number is not None:
            signal.signal(number, _interrupt)
    client = MusicBrainzClient()
    try:
        match_catalog(catalog, ids_path, client, max_requests=args.max_requests, progress=_print)
    except KeyboardInterrupt:
        _print({"phase": "interrupted", "note": "progress saved; run the same command to resume"})
        return 130
    finally:
        client.close()
        lock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
