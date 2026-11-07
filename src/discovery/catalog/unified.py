"""Build a reviewed-rule, multi-source catalogue without laundering historical ratings."""

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from discovery.catalog import load_catalog
from discovery.catalog.community import source_evidence
from discovery.catalog.matching import match_releases, normalized_name
from discovery.catalog.taxonomy import compile_taxonomy
from discovery.contracts import Artist, Catalog, HistoricalObservation, Release, SourceMetadata
from discovery.storage import catalog_digest


def _key(text):
    return normalized_name(text)


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def load_snapshot(directory: Path) -> tuple[list[dict], dict, str]:
    manifest_bytes = (directory / "manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    if (manifest.get("dataset_kind") != "historical_community_metadata"
        or manifest.get("is_fixture") is not False or manifest.get("personal_labels") != 0
        or manifest.get("source") not in {"rym", "aoty"}):
        raise ValueError("Expected a real historical community snapshot")
    filename = manifest.get("records_file")
    if filename != "releases.jsonl":
        raise ValueError("Unsupported snapshot records path")
    raw = (directory / filename).read_bytes()
    if _sha(raw) != manifest.get("records_sha256"):
        raise ValueError("Community snapshot hash mismatch")
    rows = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
    if len(rows) != manifest.get("accepted_rows"):
        raise ValueError("Community snapshot row count mismatch")
    for row in rows:
        if (row.get("source") != manifest["source"] or row.get("is_fixture") is not False
            or row.get("personal_label") is not None or row.get("observed_at") is not None
            or row.get("canonical_release_id") is not None
            or row.get("count_semantics_verified") is not False
            or row.get("input_sha256") != manifest.get("input_sha256")):
            raise ValueError("Community row conflicts with its historical provenance")
    return rows, manifest, _sha(manifest_bytes)


def combine(base: Catalog, snapshots: list[tuple[list[dict], dict, str]], *,
            artist_aliases: dict[str, str] | None = None) -> tuple[Catalog, dict, dict]:
    if base.is_fixture:
        raise ValueError("Cannot enrich a synthetic catalogue with real source snapshots")
    rows = [row for records, _, _ in snapshots for row in records]
    by_row = {row["row_id"]: row for row in rows}
    if len(by_row) != len(rows):
        raise ValueError("Duplicate source row IDs")
    origins = {row["row_id"]: (manifest, provenance_id)
               for records, manifest, provenance_id in snapshots for row in records}
    taxonomy = compile_taxonomy(base, rows)
    matches = match_releases(base, rows, artist_aliases=artist_aliases)
    releases = {r.id: r.model_copy(deep=True) for r in base.releases}
    for link in matches["links"]:
        if link["release_id"] in releases and (
            link["status"] != "accepted"
            or link["rule"] == "cross_source_unique_credit_title_known_year_v1"
        ):
            raise ValueError("Generated release ID collides with an existing catalogue entity")
    artists = {a.id: a.model_copy(deep=True) for a in base.artists}
    base_credits = defaultdict(set)
    for artist in base.artists:
        base_credits[_key(artist.name)].add((artist.id,))
    for release in base.releases:
        credit = " / ".join(artists[aid].name for aid in release.artist_ids)
        base_credits[_key(credit)].add(tuple(release.artist_ids))
    explicit_aliases = {_key(name): (aid,) for name, aid in (artist_aliases or {}).items()}
    approved_credits = defaultdict(set)
    for link in matches["links"]:
        if link["status"] == "accepted" and link["release_id"] in releases:
            approved_credits[_key(by_row[link["source_row_id"]]["artist_credit"])].add(
                tuple(releases[link["release_id"]].artist_ids))
    for name, aid in (artist_aliases or {}).items():
        if aid not in artists:
            raise ValueError("Artist alias points to an absent catalogue artist")
        approved_credits[_key(name)].add((aid,))

    def artist_ids(credit):
        if _key(credit) in explicit_aliases:
            return list(explicit_aliases[_key(credit)])
        known = approved_credits.get(_key(credit), set())
        if len(known) == 1 and len(known | base_credits.get(_key(credit), set())) == 1:
            return list(next(iter(known)))
        aid = "credit:" + _sha(_key(credit).encode())[:24]
        if aid not in artists:
            artists[aid] = Artist(id=aid, name=credit)
        return [aid]

    pending_ids = {link["release_id"] for link in matches["links"] if link["status"] == "pending"}
    for entity in matches["entities"]:
        if entity["id"] in pending_ids:
            continue
        if entity["id"] in releases:
            raise ValueError("New entity would overwrite an existing release ID")
        row = by_row[entity["representative_row_id"]]
        releases[entity["id"]] = Release(
            id=entity["id"], title=row["title"], artist_ids=artist_ids(row["artist_credit"]),
            year=row["year"], release_type=row.get("release_type") or "unknown",
        )
    included_rows = []
    for link in matches["links"]:
        if link["status"] == "pending":
            continue
        row = by_row[link["source_row_id"]]
        release = releases[link["release_id"]]
        mapping = taxonomy.map_labels(row["genres"], row["secondary_genres"])
        # Secondary labels remain searchable evidence, not primary eligibility assertions.
        release.genre_ids = sorted(set(release.genre_ids) | set(mapping.primary_ids))
        evidence = [*mapping.evidence, *source_evidence(row.get("mapping_evidence", [])),
                    *source_evidence(row.get("source_evidence", []))]
        evidence = list({json.dumps(item, sort_keys=True, ensure_ascii=False): item
                         for item in evidence}.values())
        metadata = SourceMetadata(
            source=row["source"], source_row_id=row["row_id"], title=row["title"],
            artist_credit=row["artist_credit"], release_date_text=row["release_date_text"],
            primary_labels=mapping.primary_labels, secondary_labels=mapping.secondary_labels,
            primary_ids=mapping.primary_ids, secondary_ids=mapping.secondary_ids,
            descriptors=row["descriptors"], vibes=row.get("vibes", []), unmapped=mapping.unmapped,
            mapping_evidence=evidence,
        )
        if metadata not in release.source_metadata:
            release.source_metadata.append(metadata)
        manifest, provenance_id = origins[row["row_id"]]
        acquired = manifest["acquisition_provenance"].get("retrieved_at")
        observation = HistoricalObservation(
            source=row["source"], source_row_id=row["row_id"], provenance_id=provenance_id,
            url=row["source_url"], retrieved_at=datetime.fromisoformat(acquired) if acquired else None,
            rating=row["rating"], rating_scale=row["rating_scale"], rating_count=row["rating_count"],
            rating_count_text=row["rating_count_text"],
        )
        if observation not in release.historical_observations:
            release.historical_observations.append(observation)
        if row["source_url"]:
            release.links.setdefault(row["source"], row["source_url"])
        included_rows.append(row["row_id"])
    for release in releases.values():
        for aid in release.artist_ids:
            artists[aid].genre_ids = sorted(set(artists[aid].genre_ids) | set(release.genre_ids))
    catalogue = Catalog(snapshot_id="pending", taxonomy_version=taxonomy.version,
                        genres=taxonomy.genres, artists=sorted(artists.values(), key=lambda a: a.id),
                        releases=sorted(releases.values(), key=lambda r: r.id))
    catalogue.snapshot_id = "community-unified-" + catalog_digest(catalogue)[:16]
    report = {
        "schema_version": 1, "dataset_kind": "unified_music_catalog", "is_fixture": False,
        "catalog_path": "catalog.json", "catalog_snapshot_id": catalogue.snapshot_id,
        "catalog_sha256": catalog_digest(catalogue), "taxonomy_version": catalogue.taxonomy_version,
        "base_snapshot_id": base.snapshot_id, "base_catalog_sha256": catalog_digest(base),
        "source_manifests": {m["source"]: {"sha256": pid, "manifest": m} for _, m, pid in snapshots},
        "source_manifest_entries": [{"sha256": pid, "manifest": m} for _, m, pid in snapshots],
        "counts": {"base_releases": len(base.releases), "source_rows": len(rows),
                   "included_source_rows": len(included_rows),
                   "pending_source_rows": len(rows) - len(included_rows),
                   "catalog_releases": len(catalogue.releases), "artists": len(catalogue.artists),
                   "genres": len(catalogue.genres),
                   "without_primary_genre": sum(not r.genre_ids for r in catalogue.releases),
                   "historical_observations": sum(len(r.historical_observations) for r in catalogue.releases)},
        "matching_summary": matches["summary"],
        "mapping_status_counts": dict(Counter(link["status"] for link in matches["links"])),
        "historical_ratings_used_for_current_popularity": False,
        "personal_labels": 0,
        "identity_note": "Accepted metadata-rule links are documented in matching.json; they are not independent source MBID claims.",
        "limitations": ["Historical community capture timestamps and count semantics remain unknown.",
                        "Pending identity links are retained outside serving until review.",
                        "Unmapped genre labels remain in source_metadata and taxonomy report.",
                        "Chart selection is not representative of underground releases."],
    }
    return catalogue, {"manifest": report, "matching": matches, "taxonomy": taxonomy.mapping_report}, by_row


def build(base_path: Path, dataset_dirs: list[Path], output_dir: Path, *, aliases_path: Path | None = None):
    if output_dir.exists():
        raise FileExistsError("Unified snapshot output must be a new directory")
    aliases = json.loads(aliases_path.read_text()) if aliases_path else {}
    catalogue, reports, rows = combine(load_catalog(base_path), [load_snapshot(p) for p in dataset_dirs],
                                        artist_aliases=aliases)
    pending = [rows[link["source_row_id"]] for link in reports["matching"]["links"]
               if link["status"] == "pending"]
    output_dir.mkdir(parents=True, exist_ok=False)
    for name, payload in reports.items():
        (output_dir / f"{name}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                                                encoding="utf-8")
    (output_dir / "pending.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False) + "\n"
                                                      for row in pending), encoding="utf-8")
    # Publish the serving file only after its provenance reports have been written.
    from discovery.catalog import save_catalog

    save_catalog(catalogue, output_dir / "catalog.json")
    return reports["manifest"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, default=Path("data/catalogs/musicbrainz-expanded.json"))
    parser.add_argument("--dataset", type=Path, action="append", required=True)
    parser.add_argument("--artist-aliases", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = build(args.base, args.dataset, args.output_dir, aliases_path=args.artist_aliases)
    except (ValueError, OSError) as exc:
        parser.exit(2, f"Error: {exc}\n")
    print(json.dumps({"counts": result["counts"], "matching": result["mapping_status_counts"],
                      "snapshot": result["catalog_snapshot_id"]}, indent=2))


if __name__ == "__main__":
    main()
