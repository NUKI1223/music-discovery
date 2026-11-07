"""Literal source descriptions, including AOTY Vibe; never measured audio attributes."""

from datetime import datetime
from math import log2

from discovery.catalog.web.common import validate_url
from discovery.contracts import Release

SOUND_POLICY_VERSION = "source-vibe-order-v1"


def ordered_vibe_positions(release: Release) -> dict[str, int]:
    """Use only the newest attributable album-page capture, never index order.

    Positions describe the captured visible list, not vote counts or measured
    intensity. Missing/hidden labels have unknown positions. Do not merge lists
    from different captures into an invented order.
    """
    captures = []
    for metadata in release.source_metadata:
        if metadata.source != "aoty" or not metadata.vibes:
            continue
        for evidence in metadata.mapping_evidence:
            if evidence.get("kind") != "aoty_album_readable_metadata":
                continue
            try:
                validate_url(evidence.get("url", ""), "aoty")
                timestamp = datetime.fromisoformat(evidence.get("retrieved_at", ""))
                if timestamp.utcoffset() is None:
                    continue
            except (ValueError, TypeError, AttributeError):
                continue
            if not evidence.get("text_sha256") or evidence.get("vibe_completeness") not in {
                "visible_subset", "visible_block", "complete",
            }:
                continue
            labels = list(dict.fromkeys(label.strip().casefold() for label in metadata.vibes if label.strip()))
            captures.append((timestamp, metadata.source_row_id, labels))
    if not captures:
        return {}
    labels = max(captures, key=lambda capture: (capture[0], capture[1]))[2]
    return {label: index for index, label in enumerate(labels, 1)}


def descriptor_weights(release: Release) -> tuple[dict[str, float], dict[str, int]]:
    """Bounded, genre-independent display-order heuristic; never a taste label.

    AOTY presence alone gets .5; an observed position adds .5/log2(rank+1).
    Thus first=1, second≈.815, fifteenth=.625. Hidden/index-only labels retain
    the presence weight. Repeated index hits cannot override a page position.
    RYM descriptors retain their existing presence-only weight (1).
    """
    positions = ordered_vibe_positions(release)
    weights = {}
    for metadata in release.source_metadata:
        for label in metadata.descriptors:
            if label.strip():
                weights[label.strip().casefold()] = 1.0
        for label in metadata.vibes:
            label = label.strip().casefold()
            if not label:
                continue
            position = positions.get(label) if metadata.source == "aoty" else None
            weight = .5 + .5 / log2(position + 1) if position else .5
            weights[label] = max(weights.get(label, 0), weight)
    return weights, positions


def descriptor_groups(release: Release) -> dict[tuple[str, str], set[str]]:
    """Keep source and source field for explanations; normalize only spelling/case."""
    groups: dict[tuple[str, str], set[str]] = {}
    for metadata in release.source_metadata:
        for field, labels in (("descriptors", metadata.descriptors), ("vibes", metadata.vibes)):
            values = {label.strip().casefold() for label in labels if label.strip()}
            if values:
                groups.setdefault((metadata.source, field), set()).update(values)
    return groups


def source_descriptors(release: Release) -> set[str]:
    """Deduplicate across source rows/fields: repeated labels do not increase score."""
    return set().union(*descriptor_groups(release).values())


def matched_descriptions(release: Release, requested: list[str]) -> list[tuple[str, str, list[str]]]:
    wanted = set(requested)
    return [(source, field, sorted(values & wanted))
            for (source, field), values in sorted(descriptor_groups(release).items()) if values & wanted]
