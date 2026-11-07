"""Explicit, versioned source-label mapping; no fuzzy or substring classification."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from discovery.contracts import Catalog, Genre

DEFAULT_CONFIG = Path(__file__).resolve().parents[3] / "configs/community-taxonomy.json"


def normalize_label(label: str) -> str:
    """Normalize typography only; semantic equivalents require configured aliases."""
    value = unicodedata.normalize("NFKC", label).casefold()
    for separator in ("-", "‐", "‑", "–", "—"):
        value = value.replace(separator, " ")
    return " ".join(value.split())


@dataclass
class LabelMapping:
    primary_labels: list[str]
    secondary_labels: list[str]
    primary_ids: list[str] = field(default_factory=list)
    secondary_ids: list[str] = field(default_factory=list)
    unmapped: list[dict] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)


@dataclass
class CompiledTaxonomy:
    genres: list[Genre]
    version: str
    mapping_report: dict
    _aliases: dict[str, str] = field(repr=False)
    _contextual: dict[str, dict] = field(repr=False)
    _descendants: dict[str, set[str]] = field(repr=False)

    def map_labels(self, primary: list[str], secondary: list[str]) -> LabelMapping:
        for labels in (primary, secondary):
            if not isinstance(labels, (list, tuple)) or any(not isinstance(item, str) for item in labels):
                raise ValueError("Expected a list of source genre strings")
        result = LabelMapping(primary_labels=list(primary), secondary_labels=list(secondary))
        # Only unconditional evidence establishes context; ambiguous terms cannot
        # bootstrap one another into a genre scope. Roles remain separate below.
        direct_ids = {self._aliases[normalize_label(label)] for label in [*primary, *secondary]
                      if normalize_label(label) in self._aliases}
        for role, labels, target in (("primary", primary, result.primary_ids),
                                     ("secondary", secondary, result.secondary_ids)):
            for label in labels:
                key = normalize_label(label)
                gid = self._aliases.get(key)
                evidence = {"label": label, "role": role, "normalized_label": key}
                if key in self._contextual:
                    rule = self._contextual[key]
                    context = sorted(direct_ids & self._descendants[rule["requires_scope"]])
                    if context:
                        gid = rule["genre_id"]
                        evidence.update(rule="contextual_alias", context=context)
                    else:
                        result.unmapped.append({"label": label, "role": role,
                                                "reason": "ambiguous_without_context"})
                        continue
                elif gid:
                    evidence["rule"] = "exact_alias"
                else:
                    result.unmapped.append({"label": label, "role": role,
                                            "reason": "unmapped_label"})
                    continue
                if gid not in target:
                    target.append(gid)
                result.evidence.append({**evidence, "genre_id": gid})
        return result


def compile_taxonomy(base_catalog: Catalog, rows: Iterable[dict], *,
                     config_path: Path | None = None) -> CompiledTaxonomy:
    """Preserve base IDs/names/parents; extend via reviewed JSON and report coverage.

    Rows are normalized community records with genres (or primary_genres) and
    secondary_genres arrays. They affect coverage only, never mapping policy.
    """
    config = json.loads((config_path or DEFAULT_CONFIG).read_text(encoding="utf-8"))
    if not isinstance(config, dict) or not isinstance(config.get("version"), str):
        raise ValueError("Taxonomy configuration requires an explicit version")
    configured = [Genre.model_validate(value) for value in config.get("genres", [])]
    if len({genre.id for genre in configured}) != len(configured):
        raise ValueError("Duplicate configured genre IDs")
    genres = {genre.id: genre.model_copy(deep=True) for genre in base_catalog.genres}
    aliases = {}

    def add_alias(label: str, gid: str) -> None:
        key = normalize_label(label)
        if not key:
            raise ValueError("Empty genre alias")
        if key in aliases and aliases[key] != gid:
            raise ValueError(f"Conflicting genre alias: {label}")
        aliases[key] = gid

    for genre in configured:
        if genre.id in genres:
            existing = genres[genre.id]
            if set(existing.parent_ids) != set(genre.parent_ids):
                raise ValueError(f"Configured parents would change existing genre: {genre.id}")
            existing.aliases = sorted(set(existing.aliases + genre.aliases + [genre.name]) - {existing.name})
        else:
            genres[genre.id] = genre.model_copy(deep=True)
            # Match the normalization applied to an existing configured genre,
            # so enriching this output again does not change its taxonomy hash.
            genres[genre.id].aliases = sorted(set(genre.aliases) - {genre.name})
    ordered = sorted(genres.values(), key=lambda item: item.id)
    graph = Catalog(snapshot_id="taxonomy-validation", genres=ordered)
    for genre in ordered:
        for label in [genre.name, *genre.aliases]:
            add_alias(label, genre.id)
    contextual = {}
    for rule in config.get("contextual_aliases", []):
        if set(rule) != {"label", "genre_id", "requires_scope"}:
            raise ValueError("Contextual alias requires label, genre_id and requires_scope")
        if rule["genre_id"] not in genres or rule["requires_scope"] not in genres:
            raise ValueError("Contextual alias references an unknown genre")
        key = normalize_label(rule["label"])
        if key in contextual:
            raise ValueError("Duplicate contextual alias")
        if key in aliases and aliases[key] != rule["genre_id"]:
            raise ValueError("Contextual alias conflicts with a different genre")
        aliases.pop(key, None)
        contextual[key] = dict(rule)
    # A reduced custom configuration must not accidentally re-enable the base
    # display name Trap as unconditional evidence of hip-hop membership.
    if "trap" in aliases:
        gid = aliases.pop("trap")
        if "hip-hop" in genres and gid in graph.descendant_ids("hip-hop"):
            contextual["trap"] = {"label": "Trap", "genre_id": gid, "requires_scope": "hip-hop"}
    # Base internal IDs are deliberately not automatic aliases: 'experimental'
    # is the legacy hip-hop ID while the label Experimental denotes a wider root.
    payload = {"config": config, "genres": [genre.model_dump(mode="json") for genre in ordered],
               "effective_contextual_aliases": contextual}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    taxonomy = CompiledTaxonomy(ordered, f"{config['version']}+{digest[:12]}", {}, aliases,
                               contextual, {gid: graph.descendant_ids(gid) for gid in genres})
    overall = Counter(rows=0, rows_with_primary_mapping=0, rows_with_any_mapping=0,
                      label_occurrences=0, mapped_label_occurrences=0)
    missing = Counter()
    by_source = {}
    for row in rows:
        primary = row.get("genres", row.get("primary_genres", []))
        secondary = row.get("secondary_genres", [])
        mapped = taxonomy.map_labels(primary, secondary)
        source = row.get("source", "unknown")
        if not isinstance(source, str):
            raise ValueError("Source must be a string")
        source_stats = by_source.setdefault(source, Counter())
        stats = {"rows": 1, "rows_with_primary_mapping": int(bool(mapped.primary_ids)),
                 "rows_with_any_mapping": int(bool(mapped.primary_ids or mapped.secondary_ids)),
                 "label_occurrences": len(primary)+len(secondary),
                 "mapped_label_occurrences": len(mapped.evidence)}
        overall.update(stats)
        source_stats.update(stats)
        missing.update(item["label"] for item in mapped.unmapped)
    taxonomy.mapping_report = {
        "taxonomy_version": taxonomy.version, "base_taxonomy_version": base_catalog.taxonomy_version,
        "config_sha256": hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest(),
        "genre_count": len(ordered), "preserved_base_genre_ids": sorted(g.id for g in base_catalog.genres),
        **dict(overall), "by_source": {source: dict(values) for source, values in sorted(by_source.items())},
        "unmapped_label_counts": dict(sorted(missing.items(), key=lambda item: (-item[1], item[0]))),
        "policy": "Exact configured names/aliases; original labels and primary/secondary roles retained; no fuzzy inference.",
        "warnings": ["Taxonomy is an explicit editorial hierarchy, not a claim of universally agreed music classification.",
                     "Unmapped source labels are retained; mapping coverage does not establish source accuracy."]}
    return taxonomy
