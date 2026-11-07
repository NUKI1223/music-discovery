"""Resumable, explicitly invoked collection of a bounded MusicBrainz catalogue."""

import argparse
import fcntl
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import httpx

from .audit import audit_catalog
from .io import save_catalog
from .musicbrainz import ACCESS_BASIS, MusicBrainzClient, normalize_pages


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def balanced_candidates(pages: list[dict], favorite_ids: set[str], limit: int) -> list[dict]:
    """Keep seed discographies, then round-robin query strata; no popularity sort."""
    stamp = datetime.now(UTC)
    allowed, _ = normalize_pages(pages, observed_at=stamp)
    allowed_ids = {r.id for r in allowed.releases}
    chosen: dict[str, dict] = {}
    for page in pages:
        for row in page["release-groups"]:
            if row.get("id") not in allowed_ids:
                continue
            credits = {credit.get("artist", {}).get("id") for credit in row.get("artist-credit", [])
                       if isinstance(credit, dict)}
            if credits & favorite_ids:
                chosen.setdefault(row["id"], row)
    buckets = [[row for row in page["release-groups"] if row.get("id") in allowed_ids] for page in pages]
    for index in range(max((len(bucket) for bucket in buckets), default=0)):
        for bucket in buckets:
            if len(chosen) >= limit:
                return list(chosen.values())[:limit]
            if index < len(bucket):
                chosen.setdefault(bucket[index]["id"], bucket[index])
    return list(chosen.values())[:limit]


def collect(config_path: Path, *, offline: bool = False) -> dict:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    workspace = Path(config["workspace"])
    workspace.mkdir(parents=True, exist_ok=True)
    output = Path(config["output"])
    manifest_path = Path(config["manifest"])
    state_path = workspace / "state.json"
    # Protect the shared cache/collector from accidental duplicate invocation.
    lock_handle = (workspace / "collector.lock").open("a")
    try:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_handle.close()
        raise ValueError("another collector already owns this workspace") from None
    client = MusicBrainzClient(cache_dir=workspace / "raw")
    pages, queries, favorite_ids, favorites, failures = [], [], set(), {}, []
    captures_by_id: dict[str, datetime] = {}
    capture_rows: list[dict] = []
    state = {"started_at": datetime.now(UTC).isoformat(), "phase": "sources", "lookups_done": 0,
             "network_requests_this_run": 0, "errors": []}
    if state_path.exists():
        previous = json.loads(state_path.read_text())
        state["first_started_at"] = previous.get("first_started_at", previous.get("started_at"))

    def checkpoint(**values):
        state.update(values)
        state["network_requests_this_run"] = client.network_requests
        state["updated_at"] = datetime.now(UTC).isoformat()
        _write_json(state_path, state)

    try:
        if offline:
            client.client = _offline_client(client.client)
            checkpoint(phase="offline-rebuild")
        else:
            checkpoint(phase="sources")
        for artist_index, name in enumerate(config["seed_artist_names"], 1):
            result = client.search_artists(name)
            capture_rows.append(dict(client.last_capture))
            checkpoint(artist_searches_done=artist_index)
            exact = [artist for artist in result["artists"] if name.casefold() in {
                artist["name"].casefold(), *(a["name"].casefold() for a in artist.get("aliases", []))
            }]
            if len(exact) != 1:
                failures.append({"artist": name, "error": "ambiguous_or_absent_exact_artist_match"})
                continue
            artist = exact[0]
            favorite_ids.add(artist["id"])
            favorites[name] = {"id": artist["id"], "canonical_name": artist["name"]}
            queries.append({"label": f"favorite:{name}", "query":
                            f'arid:{artist["id"]} AND (primarytype:album OR primarytype:ep)',
                            "limit": 100, "offset": 0})
        queries.extend(config["queries"])
        checkpoint(favorites=favorites, query_count=len(queries), errors=failures)
        for index, query in enumerate(queries, 1):
            page = client.search_release_groups(query["query"], limit=query.get("limit", 50),
                                                offset=query.get("offset", 0))
            pages.append(page)
            capture = dict(client.last_capture)
            capture["stratum"] = query["label"]
            capture_rows.append(capture)
            for row in page["release-groups"]:
                captures_by_id.setdefault(row["id"], datetime.fromisoformat(capture["captured_at"]))
            checkpoint(queries_done=index, raw_candidate_count=sum(len(p["release-groups"]) for p in pages))
            print(json.dumps({"phase": "search", "done": index, "total": len(queries),
                              "stratum": query["label"], "rows": len(page["release-groups"])}), flush=True)

        selected = balanced_candidates(pages, favorite_ids, config.get("max_releases", 500))
        detail_by_id = {}
        observation_times = dict(captures_by_id)
        checkpoint(phase="lookups", selected_release_count=len(selected))
        consecutive_failures = 0
        for index, row in enumerate(selected, 1):
            try:
                details = client.lookup_release_group(row["id"])
                detail_by_id[row["id"]] = details
                capture = dict(client.last_capture)
                capture_rows.append(capture)
                observation_times[row["id"]] = datetime.fromisoformat(capture["captured_at"])
                consecutive_failures = 0
            except (httpx.HTTPError, ValueError) as exc:
                failures.append({"release_id": row["id"], "error": str(exc)})
                consecutive_failures += 1
                if consecutive_failures >= 5:
                    checkpoint(phase="interrupted", errors=failures)
                    raise RuntimeError("five consecutive lookup failures; rerun to resume cached requests") from exc
            checkpoint(lookups_done=index, successful_lookups=len(detail_by_id), errors=failures)
            if index % 20 == 0 or index == len(selected):
                print(json.dumps({"phase": "lookup", "done": index, "total": len(selected),
                                  "successful": len(detail_by_id),
                                  "requests_this_run": client.network_requests}), flush=True)

        observed_at = max(observation_times.values()) if observation_times else datetime.now(UTC)
        catalog, import_report = normalize_pages(
            [{"release-groups": selected}], observed_at=observed_at, detail_by_id=detail_by_id,
            observed_at_by_id=observation_times,
        )
        catalog.snapshot_id = catalog.snapshot_id.replace("sample", "expanded", 1)
        manifest = {
            "catalog_path": os.path.relpath(output.resolve(), manifest_path.parent.resolve()),
            "source": "https://musicbrainz.org/", "access_basis": ACCESS_BASIS,
            "is_fixture": False, "taxonomy_version": catalog.taxonomy_version,
            "acquisition": {"configuration": str(config_path), "favorites": favorites,
                            "capture_records": capture_rows, "failures": failures,
                            "strategy": "seed discographies then round-robin genre/year queries; no popularity sort",
                            "not_representative": True, "max_requests_per_second": 1,
                            "limitations": ["community metadata coverage is uneven",
                                            "search index relevance and missing tags bias sampling",
                                            "vote counts measure this platform only"]},
            "independent_review": {"confirmed_genre_release_ids": [],
                                   "access_verified_sources": ["musicbrainz"]},
            "requirements_by_genre": {"hip-hop": {"min_releases": 300, "min_families": 6,
                                                   "min_releases_per_family": 30}},
        }
        save_catalog(catalog, output)
        _write_json(output.with_suffix(".import-report.json"), import_report)
        _write_json(manifest_path, manifest)
        audit = audit_catalog(catalog, as_of=datetime.now(UTC).date(),
                              review=manifest["independent_review"],
                              requirements_by_genre=manifest["requirements_by_genre"])
        _write_json(output.with_suffix(".audit.json"), audit)
        checkpoint(phase="complete", release_count=len(catalog.releases),
                   artist_count=len(catalog.artists), snapshot_id=catalog.snapshot_id,
                   ratings_with_value=sum(o.rating is not None for r in catalog.releases for o in r.observations),
                   known_vote_counts=sum(o.rating_count is not None for r in catalog.releases for o in r.observations),
                   g0_status=audit["g0_status"])
        print(json.dumps(state), flush=True)
        return state
    finally:
        client.close()
        lock_handle.close()


def _offline_client(client):
    if not getattr(client, "_discovery_offline", False):
        client.close()
        def reject(request):
            raise RuntimeError(f"offline cache miss: {request.url}")
        client = httpx.Client(base_url="https://musicbrainz.org/ws/2/", transport=httpx.MockTransport(reject))
        client._discovery_offline = True
    return client


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    collect(args.config, offline=args.offline)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
