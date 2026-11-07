"""Build a new snapshot by remapping source labels and optionally adding reviewed data.

Never mutates the source catalogue, account databases or existing output dirs.
"""

import argparse
import json
from collections import Counter
from pathlib import Path

from discovery.catalog import load_catalog, save_catalog
from discovery.catalog.remap import remap_source_genres
from discovery.catalog.unified import combine, load_snapshot
from discovery.ranking.source_descriptors import ordered_vibe_positions
from discovery.storage import catalog_digest


def coverage(catalog):
    direct = Counter(gid for release in catalog.releases for gid in release.genre_ids)
    # Inverted sets avoid recounting multi-tagged releases in a parent scope.
    index = {g.id: set() for g in catalog.genres}
    for release in catalog.releases:
        for gid in release.genre_ids:
            index[gid].add(release.id)
    return {g.id: {"direct": direct[g.id], "including_subgenres": len(set().union(
        *(index[gid] for gid in catalog.descendant_ids(g.id))))} for g in catalog.genres}


def audit(base, catalog):
    genres, artists, releases = ({x.id: x for x in items} for items in
                                (catalog.genres, catalog.artists, catalog.releases))
    for g in base.genres:
        assert (g.name, g.parent_ids) == (genres[g.id].name, genres[g.id].parent_ids)
    for a in base.artists:
        assert a.model_dump(exclude={"genre_ids"}) == artists[a.id].model_dump(exclude={"genre_ids"})
        assert set(a.genre_ids) <= set(artists[a.id].genre_ids)
    excluded = {"primary_ids", "secondary_ids", "unmapped", "mapping_evidence"}
    checked = 0
    for old in base.releases:
        new = releases[old.id]
        assert old.model_dump(exclude={"genre_ids", "source_metadata", "historical_observations", "links"}) == new.model_dump(exclude={"genre_ids", "source_metadata", "historical_observations", "links"})
        assert set(old.genre_ids) <= set(new.genre_ids)
        assert all(new.links.get(k) == v for k, v in old.links.items())
        assert all(o in new.historical_observations for o in old.historical_observations)
        assert len(new.source_metadata) >= len(old.source_metadata)
        for before, after in zip(old.source_metadata, new.source_metadata, strict=False):
            assert before.model_dump(exclude=excluded) == after.model_dump(exclude=excluded)
            for evidence in before.mapping_evidence:
                if not (evidence.get("rule") in {"exact_alias", "contextual_alias"}
                        and evidence.get("role") in {"primary", "secondary"}
                        and "label" in evidence and "genre_id" in evidence):
                    assert evidence in after.mapping_evidence
            checked += 1
        assert ordered_vibe_positions(old) == ordered_vibe_positions(new)
    return {"all_previous_release_ids_preserved": True, "all_previous_artist_ids_preserved": True,
            "previous_genre_names_and_parents_preserved": True,
            "source_facts_preserved": True, "checked_previous_metadata_rows": checked,
            "old_vibe_order_unchanged": True, "previous_observations_preserved": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, action="append", default=[])
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.output_dir.exists():
        parser.error("Output already exists; snapshots are immutable")
    base = load_catalog(args.base)
    catalog, remapping = remap_source_genres(base)
    catalog, reports, rows = combine(catalog, [load_snapshot(p) for p in args.dataset])
    integrity = audit(base, catalog)
    before, after = coverage(base), coverage(catalog)
    added = sorted(set(after) - set(before))
    report = {"before_releases": len(base.releases), "after_releases": len(catalog.releases),
              "before_genres": len(base.genres), "after_genres": len(catalog.genres),
              "new_genres": added, "new_releases": len(catalog.releases) - len(base.releases),
              "releases_with_added_genres": remapping["releases_with_added_genres"],
              "newly_classified_releases": remapping["newly_classified_releases"],
              "new_genres_with_candidates": sum(after[gid]["including_subgenres"] > 0 for gid in added),
              "without_primary_genre": sum(not r.genre_ids for r in catalog.releases),
              "with_vibes": sum(any(m.vibes for m in r.source_metadata) for r in catalog.releases),
              "with_ordered_vibes": sum(bool(ordered_vibe_positions(r)) for r in catalog.releases),
              "coverage_before": before, "coverage_after": after, "integrity": integrity}
    manifest = reports["manifest"]
    manifest.update(base_snapshot_id=base.snapshot_id, base_catalog_sha256=catalog_digest(base),
                    remapping_report="remapping.json", coverage_report="coverage.json",
                    remapped_base_snapshot_id=remapping["snapshot_id"])
    manifest["counts"]["base_releases"] = len(base.releases)
    reports.update(remapping=remapping, coverage=report)
    args.output_dir.mkdir(parents=True)
    for name, payload in reports.items():
        (args.output_dir / f"{name}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    pending = [rows[link["source_row_id"]] for link in reports["matching"]["links"] if link["status"] == "pending"]
    (args.output_dir / "pending.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in pending))
    save_catalog(catalog, args.output_dir / "catalog.json")
    print(json.dumps({k: v for k, v in report.items() if not k.startswith("coverage_")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
