"""Deterministic, auditable entity matching; heuristic acceptance is not verification.

Names use Unicode NFC, casefold and whitespace normalization only. Punctuation,
edition suffixes and full artist credits remain meaningful. No fuzzy matching,
network requests, chart-year substitution or synthetic MusicBrainz IDs.
"""

import hashlib
import json
import re
import unicodedata
from collections import Counter, defaultdict
from typing import Literal, TypedDict
from urllib.parse import urlsplit

from discovery.contracts import Artist, Catalog, Release

MATCHING_VERSION = "conservative-identity-v1"


class PairResult(TypedDict):
    decision: Literal["match", "reject", "abstain"]
    rule: str
    evidence: dict


class MatchLink(TypedDict):
    source_row_id: str
    release_id: str
    status: Literal["accepted", "unmatched", "pending"]
    rule: str
    evidence: dict
    candidate_ids: list[str]


class NewEntity(TypedDict):
    id: str
    source_row_ids: list[str]
    representative_row_id: str


class MatchingResult(TypedDict):
    version: str
    links: list[MatchLink]
    entities: list[NewEntity]
    summary: dict


def normalized_name(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).casefold().split())


def _year(row: dict) -> int | None:
    value = row.get("year")
    if value is not None and (type(value) is not int or not 1800 <= value <= 2200):
        raise ValueError("year must be a known integer release year or null")
    return value


def _signature(row: dict) -> tuple[str, str, int | None]:
    return normalized_name(row["artist_credit"]), normalized_name(row["title"]), _year(row)


def _source_key(source: str, url: str | None, identifier=None) -> tuple[str, str] | None:
    """RYM path or AOTY numeric album ID; never infer identities from slugs."""
    from_url = None
    if url:
        parts = urlsplit(url)
        expected = {"rym": {"rateyourmusic.com", "www.rateyourmusic.com"},
                    "aoty": {"albumoftheyear.org", "www.albumoftheyear.org"}}
        if (parts.scheme != "https" or parts.hostname not in expected[source]
                or parts.username or parts.password or parts.port not in (None, 443)
                or parts.query):
            raise ValueError("Invalid source release URL")
        if source == "aoty":
            match = re.fullmatch(r"/album/(\d+)[^/]*\.php", parts.path)
            if not match:
                raise ValueError("Expected AOTY album URL")
            from_url = match[1]
        else:
            if not re.fullmatch(r"/release/(?:album|ep|mixtape)/[^/]+/[^/]+/?", parts.path):
                raise ValueError("Expected RYM release URL")
            from_url = parts.path.rstrip("/")
    if identifier is not None:
        if not isinstance(identifier, str) or not identifier:
            raise ValueError("Source ID must be a nonempty string")
        if source == "aoty" and not re.fullmatch(r"\d+", identifier):
            raise ValueError("AOTY source ID must be numeric")
        # RYM's opaque numeric release ID cannot be compared with a URL slug.
        if source == "rym" and not identifier.startswith("/release/"):
            return (source, "id:" + identifier) if not from_url else (source, from_url)
        identifier = identifier.rstrip("/")
        if from_url is not None and identifier != from_url:
            raise ValueError("Source ID conflicts with source URL")
        from_url = identifier
    return (source, from_url) if from_url else None


def _release_source_keys(release: Release) -> set[tuple[str, str]]:
    keys = set()
    for url in release.links.values():
        host = urlsplit(url).hostname
        source = ("rym" if host in {"rateyourmusic.com", "www.rateyourmusic.com"} else
                  "aoty" if host in {"albumoftheyear.org", "www.albumoftheyear.org"} else None)
        if source:
            try:
                key = _source_key(source, url)
            except ValueError:
                continue  # Artist/search URLs are not identity evidence.
            if key:
                keys.add(key)
    return keys


def _credit(release: Release, artists: dict[str, Artist]) -> str:
    return normalized_name(" / ".join(artists[aid].name for aid in release.artist_ids))


def classify_pair(row: dict, release: Release, artists: dict[str, Artist], *,
                  artist_aliases: dict[str, str] | None = None) -> PairResult:
    """Pair-level decision; dataset-level uniqueness is enforced by match_releases.

    Explicit aliases are name -> existing artist ID, not automatic name guesses.
    Multi-artist credits match the complete ordered 'A / B' credit only.
    """
    artist, title, year = _signature(row)
    aliases = {normalized_name(name): aid for name, aid in (artist_aliases or {}).items()}
    if any(aid not in artists for aid in aliases.values()):
        raise ValueError("Alias refers to an unknown artist ID")
    artist_equal = artist == _credit(release, artists)
    alias_id = aliases.get(artist)
    if alias_id and release.artist_ids == [alias_id]:
        artist_equal = True
    title_equal = title == normalized_name(release.title)
    key = _source_key(row["source"], row.get("source_url"), row.get("source_release_id"))
    other_keys = {x for x in _release_source_keys(release) if x[0] == row["source"]}
    exact_external = key is not None and key in other_keys
    year_conflict = year is not None and release.year is not None and year != release.year
    type_conflict = bool(row.get("release_type") and
                         normalized_name(row["release_type"]) != normalized_name(release.release_type))
    evidence = {"artist_credit_equal": artist_equal, "title_equal": title_equal,
                "source_year": year, "candidate_year": release.year,
                "exact_external_identity": exact_external,
                "alias_artist_id": alias_id if alias_id and artist_equal else None,
                "source_release_type": row.get("release_type"),
                "candidate_release_type": release.release_type,
                "normalization": "NFC_casefold_whitespace_only",
                "independently_verified": False}
    if exact_external:
        if year_conflict or type_conflict or not artist_equal or not title_equal:
            return {"decision": "abstain", "rule": "external_identity_metadata_conflict", "evidence": evidence}
        return {"decision": "match", "rule": "exact_external_identity_v1", "evidence": evidence}
    if key and other_keys:
        return {"decision": "reject", "rule": "external_identity_disagrees", "evidence": evidence}
    if not title_equal or not artist_equal:
        return {"decision": "reject", "rule": "artist_or_title_disagrees", "evidence": evidence}
    if year_conflict or type_conflict:
        return {"decision": "abstain", "rule": "release_year_or_type_conflict", "evidence": evidence}
    if year is None or release.year is None:
        return {"decision": "abstain", "rule": "release_year_unknown", "evidence": evidence}
    return {"decision": "match", "rule": "exact_credit_title_known_year_v1", "evidence": evidence}


def _local_id(value) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "community:" + hashlib.sha256(encoded.encode()).hexdigest()[:24]


def match_releases(mb_catalog: Catalog, rows: list[dict], *,
                   artist_aliases: dict[str, str] | None = None) -> MatchingResult:
    """Return a total source-row mapping and non-MB entities without mutating inputs.

    `pending` entities are review-only. `unmatched` entities are usable independent
    source releases. Accepted tuple rules are conservative heuristics, not proof
    of platform identity. Local IDs are content-derived, stable under row order;
    identity metadata corrections can require an explicit future migration.
    """
    if mb_catalog.is_fixture:
        raise ValueError("Cannot match real community rows to a fixture catalogue")
    artist_aliases = artist_aliases or {}
    artists = {a.id: a for a in mb_catalog.artists}
    if any(aid not in artists for aid in artist_aliases.values()):
        raise ValueError("Alias refers to an unknown artist ID")
    by_id = {r.id: r for r in mb_catalog.releases}
    by_credit_title, by_external = defaultdict(set), defaultdict(set)
    for release in mb_catalog.releases:
        title = normalized_name(release.title)
        by_credit_title[_credit(release, artists), title].add(release.id)
        if len(release.artist_ids) == 1:
            for name, aid in artist_aliases.items():
                if release.artist_ids == [aid]:
                    by_credit_title[normalized_name(name), title].add(release.id)
        for key in _release_source_keys(release):
            by_external[key].add(release.id)

    row_ids = [row.get("row_id") for row in rows]
    if any(not isinstance(rid, str) or not rid for rid in row_ids) or len(set(row_ids)) != len(rows):
        raise ValueError("Source row IDs must be nonempty and unique")
    source_groups = defaultdict(list)
    for row in sorted(rows, key=lambda x: x["row_id"]):
        if row.get("source") not in {"rym", "aoty"} or row.get("is_fixture") is not False:
            raise ValueError("Expected non-fixture historical source rows")
        if not all(isinstance(row.get(field), str) and row[field].strip()
                   for field in ("artist_credit", "title")):
            raise ValueError("Missing artist credit or title")
        _year(row)
        key = _source_key(row["source"], row.get("source_url"), row.get("source_release_id"))
        source_groups[key or (row["source"], "row:" + row["row_id"])].append(row)

    units = []
    for source_key, group in sorted(source_groups.items()):
        signatures = [_signature(row) for row in group]
        credits_titles = {sig[:2] for sig in signatures}
        years = {sig[2] for sig in signatures if sig[2] is not None}
        if len(credits_titles) > 1 or len(years) > 1:
            for row in group:
                units.append({"rows": [row], "representative": row, "signature": _signature(row),
                              "blocked": "duplicate_source_identity_conflict"})
            continue
        # A known year from the same explicit source entity can represent it;
        # the original row's unknown date remains untouched in source storage.
        representative = min(group, key=lambda r: (_year(r) is None, r["row_id"]))
        units.append({"rows": group, "representative": representative,
                      "signature": _signature(representative), "blocked": None})

    by_signature, by_names = defaultdict(list), defaultdict(list)
    for index, unit in enumerate(units):
        by_signature[unit["signature"]].append(index)
        by_names[unit["signature"][:2]].append(index)
    for index, unit in enumerate(units):
        signature = unit["signature"]
        siblings = by_signature[signature]
        source_counts = Counter(units[i]["representative"]["source"] for i in siblings)
        unique_signature = signature[2] is not None and max(source_counts.values()) == 1
        if unique_signature and not any(units[i]["blocked"] for i in siblings):
            unit["local_id"] = _local_id({"credit_title_year": signature})
        else:
            row = unit["representative"]
            source_key = _source_key(row["source"], row.get("source_url"), row.get("source_release_id"))
            unit["local_id"] = _local_id({"source_key": source_key} if source_key else
                                          {"source": row["source"], "credit_title_year": signature})
        row = unit["representative"]
        source_key = _source_key(row["source"], row.get("source_url"), row.get("source_release_id"))
        candidate_ids = set(by_credit_title.get(signature[:2], set()))
        if source_key:
            candidate_ids.update(by_external.get(source_key, set()))
        decisions = {rid: classify_pair(row, by_id[rid], artists, artist_aliases=artist_aliases)
                     for rid in sorted(candidate_ids)}
        matched = [rid for rid, result in decisions.items() if result["decision"] == "match"]
        explicit = [rid for rid, result in decisions.items()
                    if result["evidence"]["exact_external_identity"]]
        evidence = {"pair_decisions": decisions,
                    "source_entity_rows": [r["row_id"] for r in unit["rows"]],
                    "identity_key": {"artist_credit": signature[0], "title": signature[1],
                                     "year": signature[2]},
                    "source_identity": list(source_key) if source_key else None,
                    "same_identity_source_rows": sorted(r["row_id"] for i in siblings
                                                         for r in units[i]["rows"]),
                    "same_source_entity_counts": dict(sorted(source_counts.items())),
                    "independently_verified": False}
        if unit["blocked"]:
            status, rule, rid = "pending", unit["blocked"], unit["local_id"]
        elif max(source_counts.values()) > 1:
            status, rule, rid = "pending", "same_source_identity_ambiguous", unit["local_id"]
        elif explicit:
            if len(explicit) == 1 and decisions[explicit[0]]["decision"] == "match":
                status, rule, rid = "accepted", "exact_external_identity_v1", explicit[0]
            else:
                status, rule, rid = "pending", "external_identity_unresolved", unit["local_id"]
        elif len(matched) == 1:
            status, rule, rid = "accepted", decisions[matched[0]]["rule"], matched[0]
        elif len(matched) > 1:
            status, rule, rid = "pending", "multiple_musicbrainz_candidates", unit["local_id"]
        elif candidate_ids:
            status, rule, rid = "pending", "musicbrainz_identity_unresolved", unit["local_id"]
        elif unique_signature and len(siblings) > 1:
            status, rule, rid = "accepted", "cross_source_unique_credit_title_known_year_v1", unit["local_id"]
        elif any(i != index for i in by_names[signature[:2]]):
            status, rule, rid = "pending", "cross_source_year_unknown_or_conflicting", unit["local_id"]
        else:
            status, rule, rid = "unmatched", "independent_source_release_v1", unit["local_id"]
        unit.update(status=status, rule=rule, release_id=rid,
                    evidence=evidence, candidate_ids=sorted(candidate_ids))

    # Never let a cross-source tuple borrow an ID from a conflicting sibling.
    for signature, indexes in by_signature.items():
        if any(units[i]["status"] == "pending" for i in indexes):
            for index in indexes:
                unit = units[index]
                if unit["rule"] == "cross_source_unique_credit_title_known_year_v1":
                    unit.update(status="pending", rule="cross_source_peer_unresolved")
    links, entity_rows = [], defaultdict(list)
    representative_rows = {}
    for unit in units:
        if unit["status"] == "pending":
            # A review bucket must not silently merge two conflicting entities.
            unit["release_id"] = _local_id({"pending_source_rows": [row["row_id"] for row in unit["rows"]]})
        for row in unit["rows"]:
            links.append(MatchLink(source_row_id=row["row_id"], release_id=unit["release_id"],
                                   status=unit["status"], rule=unit["rule"],
                                   evidence=unit["evidence"], candidate_ids=unit["candidate_ids"]))
        if unit["release_id"] not in by_id:
            entity_rows[unit["release_id"]].extend(row["row_id"] for row in unit["rows"])
            chosen = representative_rows.get(unit["release_id"])
            candidate = unit["representative"]
            if chosen is None or (_year(candidate) is None, candidate["row_id"]) < (
                    _year(chosen) is None, chosen["row_id"]):
                representative_rows[unit["release_id"]] = candidate
    entities = [NewEntity(id=rid, source_row_ids=sorted(ids),
                          representative_row_id=representative_rows[rid]["row_id"])
                for rid, ids in sorted(entity_rows.items())]
    links.sort(key=lambda row: row["source_row_id"])
    return MatchingResult(version=MATCHING_VERSION, links=links, entities=entities,
                          summary={"source_rows": len(rows), "new_entities": len(entities),
                                   "statuses": dict(Counter(row["status"] for row in links)),
                                   "rules": dict(Counter(row["rule"] for row in links))})
