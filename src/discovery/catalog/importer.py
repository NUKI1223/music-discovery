"""Conservative ID-based imports. Ambiguous records stay in a conflict report."""

import hashlib
import json
from dataclasses import dataclass

from discovery.contracts import Catalog


@dataclass(frozen=True)
class ImportResult:
    catalog: Catalog
    added: dict[str, int]
    conflicts: list[dict[str, str]]


def merge_catalogs(existing: Catalog, incoming: Catalog) -> ImportResult:
    """Merge exact canonical IDs, never fuzzy-match titles or mix fixtures with evidence.

    Changed records are deliberately quarantined: resolving a mapping or refreshing
    an observation creates a reviewed new source snapshot rather than overwriting
    the old one. Re-importing identical records leaves the catalogue unchanged.
    """
    if existing.is_fixture != incoming.is_fixture:
        raise ValueError("synthetic and real catalogues must remain separate")
    if existing.taxonomy_version != incoming.taxonomy_version:
        raise ValueError("taxonomy versions differ; migrate explicitly before merging")
    payload = existing.model_dump(mode="json")
    added, conflicts = {}, []
    for collection in ("genres", "artists", "releases"):
        indexed = {record["id"]: record for record in payload[collection]}
        added[collection] = 0
        for record in incoming.model_dump(mode="json")[collection]:
            previous = indexed.get(record["id"])
            if previous is None:
                indexed[record["id"]] = record
                added[collection] += 1
            elif previous != record:
                conflicts.append({"collection": collection, "id": record["id"],
                                  "reason": "canonical_id_has_conflicting_record"})
        payload[collection] = list(indexed.values())
    if not any(added.values()):
        return ImportResult(existing.model_copy(deep=True), added, conflicts)
    content = {k: v for k, v in payload.items() if k != "snapshot_id"}
    digest = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()[:16]
    payload["snapshot_id"] = f"import-{digest}"
    # An incoming reference to a rejected conflicting taxonomy must not corrupt storage.
    merged = Catalog.model_validate(payload)
    return ImportResult(merged, added, conflicts)
