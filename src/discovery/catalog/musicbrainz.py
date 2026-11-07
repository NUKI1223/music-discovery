"""Explicit MusicBrainz API acquisition and conservative, traceable normalization.

Importing this module never makes network requests. Core data is CC0; tags,
genre associations and ratings are CC BY-NC-SA 3.0. See docs/sources.md.
"""

import argparse
import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from uuid import UUID

import httpx

from discovery.contracts import Artist, Catalog, Genre, Observation, Release

from .io import save_catalog

ACCESS_BASIS = (
    "MusicBrainz official API; noncommercial prototype. Core metadata CC0; "
    "tags/genre associations/ratings CC BY-NC-SA 3.0, attribution MusicBrainz contributors. "
    "https://musicbrainz.org/doc/About/Data_License"
)

# This is data configuration, not genre-specific recommendation logic. Unknown
# provider tags stay in the raw snapshot instead of becoming invented assertions.
GENRE_MAP = {
    "hip hop": "hip-hop", "hip-hop": "hip-hop", "rap": "hip-hop",
    "experimental hip hop": "experimental", "abstract hip hop": "abstract",
    "industrial hip hop": "industrial-hip-hop", "hardcore hip hop": "hardcore",
    "jazz rap": "jazz-rap", "jazz hip hop": "jazz-rap", "boom bap": "boom-bap",
    "trap": "trap", "jazz": "jazz", "modal jazz": "modal-jazz",
    "rhythm and blues": "rnb", "contemporary r&b": "rnb", "r&b": "rnb",
    "electronic": "electronic", "rock": "rock", "instrumental hip hop": "instrumental-hip-hop",
}


def taxonomy() -> list[Genre]:
    roots = [("hip-hop", "Hip-hop"), ("jazz", "Jazz"), ("rnb", "R&B"),
             ("electronic", "Electronic"), ("rock", "Rock")]
    children = [
        ("experimental", "Experimental hip-hop", ["hip-hop"]),
        ("abstract", "Abstract hip-hop", ["hip-hop"]),
        ("industrial-hip-hop", "Industrial hip-hop", ["hip-hop"]),
        ("hardcore", "Hardcore hip-hop", ["hip-hop"]),
        ("jazz-rap", "Jazz rap", ["hip-hop", "jazz"]),
        ("boom-bap", "Boom bap", ["hip-hop"]), ("trap", "Trap", ["hip-hop"]),
        ("instrumental-hip-hop", "Instrumental hip-hop", ["hip-hop"]),
        ("modal-jazz", "Modal jazz", ["jazz"]),
    ]
    return [Genre(id=key, name=name) for key, name in roots] + [
        Genre(id=key, name=name, parent_ids=parents) for key, name, parents in children
    ]


class MusicBrainzClient:
    """One-process, serialized API reader, including bounded transient retry.

    Multiple importer processes must not run concurrently; the official API limit
    applies to the whole application. ``transport`` supports offline contract tests.
    """

    _lock = Lock()
    _last_request = 0.0

    def __init__(self, *, transport: httpx.BaseTransport | None = None,
                 cache_dir: Path | None = None):
        self.client = httpx.Client(
            base_url="https://musicbrainz.org/ws/2/", timeout=20,
            headers={"User-Agent": "MusicDiscovery/0.1 (local noncommercial learning prototype)",
                     "Accept": "application/json"}, transport=transport,
        )
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.last_capture: dict = {}
        self.network_requests = 0

    def close(self) -> None:
        self.client.close()

    def search_release_groups(self, query: str, *, limit: int = 25, offset: int = 0) -> dict:
        if not 1 <= limit <= 100 or offset < 0 or not query.strip():
            raise ValueError("nonempty query, limit 1..100 and nonnegative offset required")
        return self._get("release-group/", {"query": query, "fmt": "json", "limit": limit,
                                           "offset": offset}, "release-groups")

    def search_artists(self, name: str, *, limit: int = 5) -> dict:
        if not name.strip() or not 1 <= limit <= 100:
            raise ValueError("nonempty artist name and limit 1..100 required")
        phrase = name.replace("\\", "\\\\").replace('"', '\\"')
        return self._get("artist/", {"query": f'artist:"{phrase}" OR alias:"{phrase}"',
                                     "fmt": "json", "limit": limit},
                         "artists")

    def lookup_release_group(self, release_group_id: str) -> dict:
        canonical_id = str(UUID(release_group_id))
        return self._get(f"release-group/{canonical_id}",
                         {"inc": "genres+tags+ratings+artist-credits", "fmt": "json"}, "id")

    def _get(self, endpoint: str, params: dict, expected: str) -> dict:
        key = hashlib.sha256(json.dumps([endpoint, params], sort_keys=True).encode()).hexdigest()
        path = self.cache_dir / f"{key}.json" if self.cache_dir else None
        if path and path.exists():
            cached = json.loads(path.read_text(encoding="utf-8"))
            if (cached.get("endpoint") != endpoint or cached.get("params") != params
                or not cached.get("captured_at") or expected not in cached.get("response", {})):
                raise ValueError(f"invalid source cache record: {path}")
            captured_at = datetime.fromisoformat(cached["captured_at"])
            if captured_at.tzinfo is None:
                raise ValueError("source cache capture time must be timezone-aware")
            self.last_capture = {k: v for k, v in cached.items() if k != "response"}
            self.last_capture["cache_path"] = str(path)
            return cached["response"]
        for attempt in range(3):
            with self._lock:
                delay = 1.1 - (time.monotonic() - type(self)._last_request)
                if delay > 0:
                    time.sleep(delay)
                type(self)._last_request = time.monotonic()
                self.network_requests += 1
                response = self.client.get(endpoint, params=params)
            if response.status_code not in {429, 502, 503, 504} or attempt == 2:
                response.raise_for_status()
                payload = response.json()
                if (expected not in payload or expected in {"release-groups", "artists"}
                    and not isinstance(payload[expected], list)):
                    raise ValueError("unexpected MusicBrainz API response")
                captured = {
                    "endpoint": endpoint, "params": params, "url": str(response.request.url),
                    "captured_at": datetime.now(UTC).isoformat(), "response": payload,
                }
                if path:
                    temporary = path.with_suffix(".tmp")
                    temporary.write_text(json.dumps(captured, ensure_ascii=False, indent=2) + "\n",
                                         encoding="utf-8")
                    temporary.replace(path)
                self.last_capture = {k: v for k, v in captured.items() if k != "response"}
                if path:
                    self.last_capture["cache_path"] = str(path)
                return payload
            retry_after = response.headers.get("Retry-After", "")
            if retry_after:
                try:
                    seconds = float(retry_after)
                except ValueError:
                    response.raise_for_status()
                if seconds > 10 or seconds < 0:
                    response.raise_for_status()
                time.sleep(seconds)
            else:
                time.sleep(1.1 * (attempt + 1))
        raise AssertionError("unreachable")


def normalize_pages(
    pages: list[dict], *, observed_at: datetime,
    detail_by_id: dict | None = None,
    observed_at_by_id: dict[str, datetime] | None = None,
) -> tuple[Catalog, dict]:
    if observed_at.tzinfo is None:
        raise ValueError("observed_at must include a timezone")
    details = detail_by_id or {}
    artists: dict[str, Artist] = {}
    releases: dict[str, Release] = {}
    report: dict = {"skipped": [], "duplicate_ids": [], "genre_evidence": {}, "unknown_tags": {}}
    for page in pages:
        if not isinstance(page.get("release-groups"), list):
            raise ValueError("expected MusicBrainz release-group search page")
        for raw in page["release-groups"]:
            if not isinstance(raw, dict):
                report["skipped"].append({"id": "", "reason": "record_is_not_an_object"})
                continue
            rid = raw.get("id", "")
            if rid in releases:
                report["duplicate_ids"].append(rid)
                continue
            if not rid or not raw.get("title") or raw.get("primary-type") not in {"Album", "EP"}:
                report["skipped"].append({"id": rid, "reason": "unsupported_type_or_missing_identity"})
                continue
            editions = raw.get("releases", [])
            if editions and all(edition.get("status") == "Bootleg" for edition in editions):
                report["skipped"].append({"id": rid, "reason": "only_bootleg_editions"})
                continue
            secondary = set(raw.get("secondary-types", []))
            if secondary & {"Interview", "Compilation", "Live", "Remix", "DJ-mix"}:
                report["skipped"].append({"id": rid, "reason": "out_of_pilot_release_scope"})
                continue
            artist_ids = []
            for credit in raw.get("artist-credit", []):
                artist = credit.get("artist", {}) if isinstance(credit, dict) else {}
                aid, name = artist.get("id"), artist.get("name")
                if aid and name:
                    artists.setdefault(aid, Artist(id=aid, name=name))
                    artist_ids.append(aid)
            if not artist_ids:
                report["skipped"].append({"id": rid, "reason": "no_canonical_artist_credit"})
                continue
            detail = details.get(rid, {})
            if detail and detail.get("id") != rid:
                raise ValueError("rating detail ID does not match its canonical release group")
            # A lookup may omit genre associations while indexed tags still carry
            # useful provenance. Merge explicit tags only; never infer from query text.
            # An explicit nonpositive latest vote overrides that indexed tag.
            tag_votes = {}
            for records in (raw.get("tags", []), detail.get("tags", []), detail.get("genres", [])):
                for tag in records:
                    if isinstance(tag, dict) and isinstance(tag.get("name"), str):
                        tag_votes[tag["name"].casefold()] = tag.get("count", 0)
            tags = sorted(tag for tag, votes in tag_votes.items() if votes > 0)
            genre_ids = sorted({GENRE_MAP[tag] for tag in tags if tag in GENRE_MAP})
            withheld = {}
            # "trap" is also an EDM genre. It alone cannot establish hip-hop
            # membership; keep the provider tag as evidence but withhold that edge.
            hiphop_support = {"hip-hop", "experimental", "abstract", "industrial-hip-hop",
                              "hardcore", "jazz-rap", "boom-bap", "instrumental-hip-hop"}
            if "trap" in genre_ids and not set(genre_ids) & hiphop_support:
                genre_ids.remove("trap")
                withheld["trap"] = "ambiguous_trap_without_independent_hiphop_tag"
            report["genre_evidence"][rid] = {
                "provider": "musicbrainz", "raw_tags": tags,
                "mapped_genre_ids": genre_ids, "method": "exact_alias_mapping_of_release_tags",
                "mapping_version": "mb-tags-v2", "independent_manual_review": False,
                "withheld_genre_ids": withheld,
                "tag_sources": {"search_tags": raw.get("tags", []),
                                "lookup_tags": detail.get("tags", []),
                                "lookup_genres": detail.get("genres", [])},
            }
            report["unknown_tags"][rid] = sorted(set(tags) - GENRE_MAP.keys())
            # Artist priors remain unavailable unless actual artist-level observations
            # are imported separately; release assertions are not promoted to artist facts.
            first_date = raw.get("first-release-date", "")
            year = int(first_date[:4]) if first_date[:4].isdigit() else None
            if year is not None and not 1800 <= year <= 2200:
                year = None
            rating = detail.get("rating", {}) or {}
            # Search score is match relevance; search count is editions. Neither is a vote.
            count = rating.get("votes-count")
            value = rating.get("value")
            observation = Observation(
                source="musicbrainz", url=f"https://musicbrainz.org/release-group/{rid}",
                observed_at=(observed_at_by_id or {}).get(rid, observed_at),
                rating=value, rating_scale=5 if value is not None else None,
                rating_count=count, access_basis=ACCESS_BASIS,
            )
            releases[rid] = Release(
                id=rid, title=raw["title"], artist_ids=list(dict.fromkeys(artist_ids)),
                genre_ids=genre_ids, year=year,
                release_type="mixtape" if "Mixtape/Street" in secondary else raw["primary-type"].lower(),
                observations=[observation], links={"musicbrainz": observation.url},
            )
    content = json.dumps({"pages": pages, "details": details, "observed_at": observed_at.isoformat(),
                          "observed_at_by_id": {key: value.isoformat() for key, value in
                                                (observed_at_by_id or {}).items()}},
                         sort_keys=True, ensure_ascii=False).encode()
    digest = hashlib.sha256(content).hexdigest()[:16]
    catalog = Catalog(
        snapshot_id=f"musicbrainz-sample-{digest}", taxonomy_version="mb-tags-v2",
        is_fixture=False, genres=taxonomy(), artists=list(artists.values()), releases=list(releases.values()),
    )
    report["release_count"] = len(releases)
    report["source"] = "https://musicbrainz.org/"
    report["access_basis"] = ACCESS_BASIS
    report["observed_at"] = observed_at.isoformat()
    return catalog, report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    fetch = subparsers.add_parser("fetch", help="Explicit network request; never run concurrent importers")
    fetch.add_argument("--query", required=True)
    fetch.add_argument("--limit", type=int, default=25)
    fetch.add_argument("--offset", type=int, default=0)
    fetch.add_argument("--output", required=True, type=Path)
    normalize = subparsers.add_parser("normalize", help="Offline normalization of saved API responses")
    normalize.add_argument("--input", required=True, type=Path, nargs="+")
    normalize.add_argument("--details", type=Path)
    normalize.add_argument("--observed-at", required=True, type=datetime.fromisoformat)
    normalize.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.command == "fetch":
        client = MusicBrainzClient()
        try:
            result = client.search_release_groups(args.query, limit=args.limit, offset=args.offset)
        finally:
            client.close()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    else:
        pages = [json.loads(path.read_text(encoding="utf-8")) for path in args.input]
        details = json.loads(args.details.read_text()) if args.details else None
        catalog, report = normalize_pages(pages, observed_at=args.observed_at, detail_by_id=details)
        save_catalog(catalog, args.output)
        args.output.with_suffix(".import-report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
