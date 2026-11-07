"""Read-only catalog comparison for Vibe enrichment; never opens user databases."""

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from discovery.catalog import load_catalog
from discovery.contracts import Catalog
from discovery.storage import catalog_digest


def fingerprint(value) -> str:
    payload = value.model_dump(mode="json") if hasattr(value, "model_dump") else value
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def multiset(values) -> Counter:
    return Counter(fingerprint(value) for value in values)


def evidence_kind(evidence: dict) -> str | None:
    """Recognize recorded acquisition evidence; do not assert independent truth of tags."""
    try:
        index_url = urlsplit(evidence.get("index_page_url", ""))
        detail_url = urlsplit(evidence.get("url", ""))
    except (TypeError, ValueError):
        return None
    hosts = {"www.albumoftheyear.org", "albumoftheyear.org"}
    if (evidence.get("field") == "vibes" and isinstance(evidence.get("vibe"), str)
            and index_url.scheme == "https" and index_url.hostname in hosts
            and index_url.path.startswith("/all/releases/vibe/")):
        return "index"
    if (evidence.get("kind") == "aoty_album_readable_metadata" and detail_url.scheme == "https"
            and detail_url.hostname in hosts and detail_url.path.startswith("/album/")):
        return "detail"
    return None


def pair_evidence(catalog: Catalog) -> dict[tuple[str, str], set[str]]:
    pairs = defaultdict(set)
    for release in catalog.releases:
        for metadata in release.source_metadata:
            labels = {label.strip().casefold() for label in metadata.vibes if label.strip()}
            for label in labels:
                pairs[release.id, label]  # Retain labels with unclassified evidence too.
            for evidence in metadata.mapping_evidence:
                kind = evidence_kind(evidence) if metadata.source == "aoty" else None
                if kind == "detail":
                    for label in labels:
                        pairs[release.id, label].add("detail")
                elif kind == "index":
                    label = evidence["vibe"].strip().casefold()
                    if label in labels:
                        pairs[release.id, label].add("index")
    return dict(pairs)


def evidence_counts(pairs: dict, selected: set | None = None) -> dict:
    selected = set(pairs) if selected is None else selected
    groups = Counter()
    for key in selected:
        kinds = pairs[key]
        groups["both" if kinds == {"index", "detail"} else next(iter(kinds)) if kinds else "unclassified"] += 1
    return {kind: groups[kind] for kind in ("index", "detail", "both", "unclassified")}


def coverage(catalog: Catalog, pairs: dict, scope: str | None = None) -> dict:
    if scope is None:
        release_ids = {release.id for release in catalog.releases}
    elif scope not in {genre.id for genre in catalog.genres}:
        release_ids = set()
    else:
        allowed = catalog.descendant_ids(scope)
        release_ids = {release.id for release in catalog.releases if allowed.intersection(release.genre_ids)}
    selected = {pair for pair in pairs if pair[0] in release_ids}
    annotated = {pair[0] for pair in selected}
    return {
        "releases": len(release_ids), "releases_with_vibes": len(annotated),
        "releases_without_vibes": len(release_ids - annotated),
        "coverage_fraction": len(annotated) / len(release_ids) if release_ids else None,
        "distinct_release_vibe_pairs": len(selected),
        "distinct_labels": sorted({pair[1] for pair in selected}),
        "distinct_label_count": len({pair[1] for pair in selected}),
        "pairs_by_evidence": evidence_counts(pairs, selected),
    }


def integrity(base: Catalog, catalog: Catalog) -> dict:
    failures = []
    checks = Counter()
    before = {"release": {row.id: row for row in base.releases},
              "artist": {row.id: row for row in base.artists}, "genre": {row.id: row for row in base.genres}}
    after = {"release": {row.id: row for row in catalog.releases},
             "artist": {row.id: row for row in catalog.artists}, "genre": {row.id: row for row in catalog.genres}}

    def require(ok, category, identifier=None):
        checks[category] += 1
        if not ok:
            failures.append({"check": category, "id": identifier})

    require(base.is_fixture == catalog.is_fixture, "fixture_status_preserved")
    for kind, rows in before.items():
        for identifier, original in rows.items():
            current = after[kind].get(identifier)
            require(current is not None, f"{kind}_id_preserved", identifier)
            if current is None:
                continue
            if kind == "genre":
                require(original.name == current.name and original.parent_ids == current.parent_ids,
                        "genre_identity_preserved", identifier)
                require(set(original.aliases) <= set(current.aliases), "genre_aliases_preserved", identifier)
            elif kind == "artist":
                require(original.name == current.name, "artist_identity_preserved", identifier)
                require(set(original.genre_ids) <= set(current.genre_ids), "artist_genres_preserved", identifier)
            else:
                require((original.title, original.artist_ids, original.year, original.release_type) ==
                        (current.title, current.artist_ids, current.year, current.release_type),
                        "release_identity_preserved", identifier)
                require(set(original.genre_ids) <= set(current.genre_ids), "release_genres_preserved", identifier)
                require(all(current.links.get(key) == value for key, value in original.links.items()),
                        "source_links_preserved", identifier)
                for field in ("observations", "historical_observations", "source_metadata"):
                    require(not (multiset(getattr(original, field)) - multiset(getattr(current, field))),
                            f"old_{field}_multiset_preserved", identifier)
    return {"passed": not failures, "checked": dict(checks), "failure_count": len(failures), "failures": failures}


def audit_catalogs(base: Catalog, catalog: Catalog) -> dict:
    previous, current = pair_evidence(base), pair_evidence(catalog)
    old_pairs, new_pairs = set(previous), set(current)
    old_annotated = {rid for rid, _ in old_pairs}
    new_annotated = {rid for rid, _ in new_pairs}
    old_ids = {release.id for release in base.releases}
    current_ids = {release.id for release in catalog.releases}
    old_metadata = {release.id: multiset(release.source_metadata) for release in base.releases}
    metadata_kinds = Counter()
    new_observations = Counter()
    old_releases = {release.id: release for release in base.releases}
    for release in catalog.releases:
        remaining = multiset(release.source_metadata) - old_metadata.get(release.id, Counter())
        for metadata in release.source_metadata:
            key = fingerprint(metadata)
            if remaining[key] <= 0:
                continue
            remaining[key] -= 1
            kinds = {kind for item in metadata.mapping_evidence if (kind := evidence_kind(item))}
            metadata_kinds["both" if kinds == {"index", "detail"} else next(iter(kinds)) if kinds else "unclassified"] += 1
        original = old_releases.get(release.id)
        for field in ("observations", "historical_observations"):
            added = multiset(getattr(release, field)) - multiset(getattr(original, field) if original else [])
            new_observations[field] += sum(added.values())
            new_observations[field + "_with_rating_or_count"] += sum(
                count for encoded, count in added.items()
                if any(json.loads(encoded).get(key) is not None for key in ("rating", "rating_count")))
    genres = {genre.id: genre for genre in [*base.genres, *catalog.genres]}
    scopes = {genre.id for genre in [*base.genres, *catalog.genres] if not genre.parent_ids}
    if "rage" in genres:
        scopes.add("rage")
    report = {
        "schema_version": 1, "audit_version": "vibe-coverage-v1", "timestamp": datetime.now(UTC).isoformat(),
        "offline": True, "user_database_opened": False, "cost_usd": 0,
        "base": {"snapshot_id": base.snapshot_id, "canonical_sha256": catalog_digest(base),
                 "artists": len(base.artists), "genres": len(base.genres), **coverage(base, previous)},
        "catalog": {"snapshot_id": catalog.snapshot_id, "canonical_sha256": catalog_digest(catalog),
                    "artists": len(catalog.artists), "genres": len(catalog.genres), **coverage(catalog, current)},
        "integrity": integrity(base, catalog),
        "delta": {
            "added_release_ids": sorted(current_ids - old_ids), "removed_release_ids": sorted(old_ids - current_ids),
            "newly_annotated_releases": len(new_annotated - old_annotated),
            "newly_annotated_existing_releases": len((new_annotated - old_annotated) & old_ids),
            "newly_annotated_release_ids": sorted(new_annotated - old_annotated),
            "new_release_vibe_pairs": len(new_pairs - old_pairs),
            "removed_release_vibe_pairs": len(old_pairs - new_pairs),
            "new_pairs_by_evidence": evidence_counts(current, new_pairs - old_pairs),
            "new_labels": sorted({label for _, label in new_pairs} - {label for _, label in old_pairs}),
            "new_metadata_rows_by_evidence": dict(metadata_kinds), "added_observations": dict(new_observations),
        },
        "per_root_genre_and_rage": [{"id": scope, "name": genres[scope].name,
                                     "base": coverage(base, previous, scope), "catalog": coverage(catalog, current, scope)}
                                    for scope in sorted(scopes)],
        "limitations": [
            "Genre groups include descendants and overlap; their totals must not be added.",
            "Vibe labels normalized by strip/casefold and deduplicated per canonical release.",
            "Index/detail counts describe recorded source evidence, not independent audio validation.",
            "This comparison does not fetch source pages, inspect user databases or measure musical quality.",
        ],
    }
    if old_pairs - new_pairs:
        report["integrity"]["passed"] = False
        report["integrity"]["failure_count"] += 1
        report["integrity"]["failures"].append({"check": "old_vibe_pairs_preserved", "count": len(old_pairs - new_pairs)})
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Output already exists; use a new report path")
    try:
        base, catalog = load_catalog(args.base), load_catalog(args.catalog)
        report = audit_catalogs(base, catalog)
        report["input_files"] = {name: {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                                 for name, path in (("base", args.base), ("catalog", args.catalog))}
    except (OSError, ValueError) as exc:
        report = {"audit_version": "vibe-coverage-v1", "integrity": {"passed": False,
                  "failure_count": 1, "failures": [{"check": "load_valid_catalogs", "reason": str(exc)}]}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"passed": report["integrity"]["passed"], "failures": report["integrity"]["failure_count"],
                      "releases_with_vibes": report.get("catalog", {}).get("releases_with_vibes"),
                      "newly_annotated_releases": report.get("delta", {}).get("newly_annotated_releases"),
                      "new_pairs": report.get("delta", {}).get("new_release_vibe_pairs"),
                      "output": str(args.output)}, ensure_ascii=False))
    return 0 if report["integrity"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
