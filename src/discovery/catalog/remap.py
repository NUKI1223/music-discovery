"""Reindex retained source genre labels into a new, immutable catalogue snapshot."""

from collections import Counter
from pathlib import Path

from discovery.catalog.taxonomy import compile_taxonomy
from discovery.contracts import Catalog
from discovery.storage import catalog_digest


def remap_source_genres(base: Catalog, *, config_path: Path | None = None) -> tuple[Catalog, dict]:
    """Only explicit primary labels add eligibility; preserve all original facts.

    Existing genre assertions (including MusicBrainz tags) are retained. This is
    an additive expansion, not a tool for retracting previously accepted genres.
    Old snapshots remain valid for replay. Only derived mapping fields in the
    new copy change; descriptors, Vibe order and capture evidence are untouched.
    """
    rows = ({"source": m.source, "genres": m.primary_labels,
             "secondary_genres": m.secondary_labels}
            for release in base.releases for m in release.source_metadata)
    taxonomy = compile_taxonomy(base, rows, config_path=config_path)
    result = base.model_copy(deep=True)
    result.genres = taxonomy.genres
    result.taxonomy_version = taxonomy.version
    added_counts = Counter()
    changed = []
    for release in result.releases:
        before = set(release.genre_ids)
        primary = set(before)
        for metadata in release.source_metadata:
            mapping = taxonomy.map_labels(metadata.primary_labels, metadata.secondary_labels)
            metadata.primary_ids = mapping.primary_ids
            metadata.secondary_ids = mapping.secondary_ids
            metadata.unmapped = mapping.unmapped
            # Replace only our previous exact/contextual label mapping evidence.
            # Attributable capture evidence (including Vibe positions) survives.
            retained = [e for e in metadata.mapping_evidence
                        if not (e.get("rule") in {"exact_alias", "contextual_alias"}
                                and e.get("role") in {"primary", "secondary"}
                                and "label" in e and "genre_id" in e)]
            metadata.mapping_evidence = [*mapping.evidence, *retained]
            primary.update(mapping.primary_ids)
        additions = primary - before
        if additions:
            release.genre_ids = sorted(primary)
            added_counts.update(additions)
            changed.append({"release_id": release.id, "added_primary_ids": sorted(additions),
                            "previously_unclassified": not before})
    artists = {artist.id: artist for artist in result.artists}
    genres_by_artist = {aid: set(artist.genre_ids) for aid, artist in artists.items()}
    for release in result.releases:
        for aid in release.artist_ids:
            genres_by_artist[aid].update(release.genre_ids)
    for aid, genres in genres_by_artist.items():
        if genres != set(artists[aid].genre_ids):
            artists[aid].genre_ids = sorted(genres)
    # Deterministic and idempotent: never include the previous snapshot ID in
    # the content identity of a new mapping revision.
    result.snapshot_id = "pending"
    result.snapshot_id = "community-remapped-" + catalog_digest(result)[:16]
    Catalog.model_validate(result.model_dump(mode="python"))
    report = {"base_snapshot_id": base.snapshot_id, "snapshot_id": result.snapshot_id,
              "base_taxonomy_version": base.taxonomy_version,
              "taxonomy_version": result.taxonomy_version,
              "releases_with_added_genres": len(changed),
              "newly_classified_releases": sum(x["previously_unclassified"] for x in changed),
              "added_primary_counts": dict(sorted(added_counts.items())),
              "changes": changed, "taxonomy": taxonomy.mapping_report}
    return result, report
