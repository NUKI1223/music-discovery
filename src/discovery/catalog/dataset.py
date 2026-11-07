"""Export source metadata separately from personal preference training labels."""

import argparse
import hashlib
import json
from pathlib import Path
from urllib.parse import urlparse

from discovery.catalog import load_catalog
from discovery.contracts import Catalog
from discovery.storage import catalog_digest


def metadata_records(catalog: Catalog) -> list[dict]:
    """One canonical release per row; community ratings are observations, not labels."""
    artists = {artist.id: artist.name for artist in catalog.artists}
    return [{
        "release_id": release.id,
        "title": release.title,
        "artist_ids": release.artist_ids,
        "artist_names": [artists[aid] for aid in release.artist_ids],
        "genre_ids": sorted(release.genre_ids),
        "year": release.year,
        "release_type": release.release_type,
        "observations": [observation.model_dump(mode="json")
                         for observation in release.observations],
        "historical_observations": [o.model_dump(mode="json") for o in release.historical_observations],
        "source_metadata": [m.model_dump(mode="json") for m in release.source_metadata],
        "links": release.links,
        "taxonomy_version": catalog.taxonomy_version,
        "is_fixture": catalog.is_fixture,
    } for release in sorted(catalog.releases, key=lambda release: release.id)]


def export_metadata(catalog: Catalog, output_dir: Path, *, enrichment: dict | None = None) -> dict:
    """Create a new dataset directory; never overwrite a frozen export."""
    records = metadata_records(catalog)
    enrichment_manifest = None
    if enrichment is not None:
        from discovery.catalog.wikidata import QID, WikidataDataset

        enrichment = WikidataDataset.model_validate(enrichment).model_dump(mode="json")
        if (enrichment.get("schema_version") != 1 or enrichment.get("source") != "wikidata"
            or enrichment.get("license") != "CC0-1.0"
            or enrichment.get("catalog_snapshot_id") != catalog.snapshot_id
            or enrichment.get("catalog_sha256") != catalog_digest(catalog)
            or enrichment.get("taxonomy_version") != catalog.taxonomy_version
            or enrichment.get("is_fixture") is not False or catalog.is_fixture):
            raise ValueError("Enrichment provenance does not match this real catalogue")
        indexed = {}
        known = {row["release_id"]: row for row in records}
        for extra in enrichment.get("records", []):
            rid = extra["release_id"]
            reference_ids = {rid}
            if rid in known:
                for url in known[rid]["links"].values():
                    parsed = urlparse(url)
                    if parsed.hostname == "musicbrainz.org" and parsed.path.startswith("/release-group/"):
                        reference_ids.add(parsed.path.rstrip("/").rsplit("/", 1)[-1])
            if (rid not in known or rid in indexed
                or extra.get("release_group_mbid") not in reference_ids
                or set(extra.get("artist_ids", [])) != set(known[rid]["artist_ids"])
                or extra.get("mapping_status") != "exact_release_group_and_artist"):
                raise ValueError("Enrichment requires unique, exact release and artist matches")
            features = extra.get("features", {})
            genre_qids = set(extra["genre_qids"])
            claim_qids = {claim["value"].get("id") for claim in extra["claims"]
                          if claim["property_id"] == "P136"
                          and claim["entity_id"] == extra["wikidata_id"]
                          and claim["rank"] != "deprecated" and isinstance(claim["value"], dict)}
            if (any(not QID.fullmatch(qid) for qid in genre_qids)
                or genre_qids != claim_qids
                or features != {f"wikidata_genre:{qid}": 1.0 for qid in genre_qids}):
                raise ValueError("Enrichment features do not match source genre claims")
            indexed[rid] = extra
        for row in records:
            row["external_metadata"] = indexed.get(row["release_id"])
        enrichment_manifest = {
            "source": enrichment["source"], "license": enrichment["license"],
            "license_url": enrichment.get("license_url"),
            "matched_rows": len(indexed),
            "sidecar_sha256": hashlib.sha256(json.dumps(enrichment, sort_keys=True).encode()).hexdigest(),
            "note": "Additional source features, not personal labels or fresh popularity.",
        }
    content = "".join(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n" for row in records)
    observations = [o for release in catalog.releases for o in release.observations]
    manifest = {
        "dataset_kind": "catalog_metadata_not_personal_labels",
        "schema_version": 1,
        "catalog_snapshot_id": catalog.snapshot_id,
        "catalog_hash": catalog_digest(catalog),
        "taxonomy_version": catalog.taxonomy_version,
        "is_fixture": catalog.is_fixture,
        "rows": len(records),
        "personal_labels": 0,
        "enrichment": enrichment_manifest,
        "records_file": "releases.jsonl",
        "records_sha256": hashlib.sha256(content.encode()).hexdigest(),
        "sources": sorted({o.source for o in observations}),
        "access_basis": sorted({o.access_basis for o in observations}),
        "coverage": {
            "releases_with_genres": sum(bool(r.genre_ids) for r in catalog.releases),
            "releases_with_community_rating": sum(any(o.rating is not None for o in r.observations)
                                                 for r in catalog.releases),
            "releases_with_vote_count": sum(any(o.rating_count is not None for o in r.observations)
                                            for r in catalog.releases),
        },
        "limitations": [
            "Source selection bias remains; rows are not a representative music sample.",
            "Community ratings must not be used as this user's likes or dislikes.",
            "Observation timestamps and source scales are preserved; no freshness is inferred.",
            "This dataset is not an input to the personal-label supervised training CLI.",
        ],
    }
    output_dir.mkdir(parents=True, exist_ok=False)
    (output_dir / "releases.jsonl").write_text(content, encoding="utf-8")
    (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False)
                                             + "\n", encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--enrichment", type=Path, help="Optional exact-matched Wikidata sidecar")
    args = parser.parse_args()
    try:
        enrichment = json.loads(args.enrichment.read_text()) if args.enrichment else None
        report = export_metadata(load_catalog(args.catalog), args.output_dir, enrichment=enrichment)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(2, f"Error: {exc}\n")
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
