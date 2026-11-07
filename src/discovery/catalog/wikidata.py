"""Small bounded Wikidata CC0 enrichment, joined only by explicit MusicBrainz IDs.

The source's genre QIDs remain independent features. This adapter never edits the
catalog taxonomy, infers personal labels, or treats metadata as popularity.
"""
from __future__ import annotations

import argparse
import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field

from discovery.contracts import Catalog
from discovery.storage import catalog_digest

LICENSE_URL = "https://www.wikidata.org/wiki/Wikidata:Licensing"
QUERY_URL = "https://query.wikidata.org/sparql"
ENTITY_URL = "https://www.wikidata.org/w/api.php"
USER_AGENT = "MusicDiscoveryLearning/0.1 (personal educational metadata experiment)"
QID = re.compile(r"Q[1-9][0-9]*\Z")


class SourceClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    property_id: str
    entity_id: str
    statement_id: str
    value: str | int | float | dict
    rank: str
    reference_count: int


class WikidataRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    release_id: str
    release_group_mbid: str
    wikidata_id: str
    title: str
    artist_ids: list[str]
    genre_qids: list[str]
    genre_labels: dict[str, str]
    features: dict[str, float]
    mapping_status: str = "exact_release_group_and_artist"
    revision_id: int
    modified_at: str | None
    source_url: str
    claims: list[SourceClaim]


class WikidataDataset(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: int = 1
    source: str = "wikidata"
    license: str = "CC0-1.0"
    license_url: str = LICENSE_URL
    fetched_at: str
    catalog_snapshot_id: str
    catalog_sha256: str
    taxonomy_version: str
    is_fixture: bool
    records: list[WikidataRecord] = Field(default_factory=list)
    quarantine: list[dict] = Field(default_factory=list)
    coverage: dict = Field(default_factory=dict)


def _uuid(value) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return str(UUID(value))
    except ValueError:
        return None


def _release_mbid(release) -> str | None:
    direct = _uuid(release.id)
    if direct:
        return direct
    # Internal release IDs may instead carry a verified MusicBrainz entity URL.
    for value in release.links.values():
        parsed = urlparse(value)
        if parsed.hostname == "musicbrainz.org" and parsed.path.startswith("/release-group/"):
            candidate = _uuid(parsed.path.rstrip("/").rsplit("/", 1)[-1])
            if candidate:
                return candidate
    return None


def _claims(entity: dict, prop: str) -> list[SourceClaim]:
    values = []
    for claim in entity.get("claims", {}).get(prop, []):
        snak = claim.get("mainsnak", {})
        if claim.get("rank") == "deprecated" or snak.get("snaktype") != "value":
            continue
        if "datavalue" not in snak:
            continue
        value = snak["datavalue"].get("value")
        if value is None:
            continue
        values.append(SourceClaim(property_id=prop, entity_id=entity["id"],
                                  statement_id=claim.get("id", ""), value=value,
                                  rank=claim.get("rank", "normal"),
                                  reference_count=len(claim.get("references", []))))
    # Wikidata's truthy view prefers preferred-rank assertions when present.
    preferred = [c for c in values if c.rank == "preferred"]
    return preferred or values


def _entity_value(claim: SourceClaim) -> str | None:
    value = claim.value
    if isinstance(value, dict) and isinstance(value.get("id"), str) and QID.fullmatch(value["id"]):
        return value["id"]
    return None


def normalize(catalog: Catalog, entities: dict[str, dict], *, fetched_at: str,
              queried_release_ids: list[str] | None = None) -> WikidataDataset:
    """Build a sidecar; absent/conflicting identities never join through titles."""
    indexed: dict[str, list[dict]] = {}
    for entity in entities.values():
        if entity.get("missing") is not None or not QID.fullmatch(entity.get("id", "")):
            continue
        for claim in _claims(entity, "P436"):
            mbid = _uuid(claim.value)
            if mbid:
                indexed.setdefault(mbid, []).append(entity)
    output = WikidataDataset(fetched_at=fetched_at, catalog_snapshot_id=catalog.snapshot_id,
                             catalog_sha256=catalog_digest(catalog), taxonomy_version=catalog.taxonomy_version,
                             is_fixture=catalog.is_fixture)
    queried = set(queried_release_ids) if queried_release_ids is not None else {r.id for r in catalog.releases}
    exact_release_matches = 0
    for release in catalog.releases:
        if release.id not in queried:
            continue
        mbid = _release_mbid(release)
        if mbid is None:
            output.quarantine.append({"release_id": release.id, "reason": "missing_release_group_mbid"})
            continue
        candidates = {e["id"]: e for e in indexed.get(mbid, [])}
        if not candidates:
            continue
        exact_release_matches += 1
        if len(candidates) != 1:
            output.quarantine.append({"release_id": release.id, "reason": "multiple_wikidata_items_for_mbid",
                                      "wikidata_ids": sorted(candidates)})
            continue
        entity = next(iter(candidates.values()))
        group_claims = _claims(entity, "P436")
        group_mbids = {_uuid(c.value) for c in group_claims} - {None}
        if group_mbids != {mbid}:
            output.quarantine.append({"release_id": release.id, "wikidata_id": entity["id"],
                                      "reason": "ambiguous_release_group_ids", "release_group_mbids": sorted(group_mbids)})
            continue
        performer_claims = _claims(entity, "P175")
        performer_qids = {_entity_value(c) for c in performer_claims} - {None}
        expected_artists = {_uuid(a) for a in release.artist_ids} - {None}
        artist_claims = []
        artist_sets = []
        for qid in sorted(performer_qids):
            assertions = _claims(entities.get(qid, {"id": qid}), "P434")
            artist_claims.extend(assertions)
            artist_sets.append({_uuid(c.value) for c in assertions} - {None})
        observed_artists = set().union(*artist_sets) if artist_sets else set()
        if not expected_artists or len(expected_artists) != len(release.artist_ids) or not artist_sets or any(not s for s in artist_sets):
            reason = "artist_identity_unverified"
        elif not expected_artists <= observed_artists or any(not s & expected_artists for s in artist_sets):
            reason = "artist_identity_conflict"
        else:
            reason = None
        if reason:
            output.quarantine.append({"release_id": release.id, "wikidata_id": entity["id"], "reason": reason,
                                      "expected_artist_ids": sorted(expected_artists), "observed_artist_mbids": sorted(observed_artists)})
            continue
        genres = _claims(entity, "P136")
        genre_qids = sorted({_entity_value(c) for c in genres} - {None})
        labels = {qid: entities.get(qid, {}).get("labels", {}).get("en", {}).get("value", qid) for qid in genre_qids}
        output.records.append(WikidataRecord(
            release_id=release.id, release_group_mbid=mbid, wikidata_id=entity["id"],
            title=entity.get("labels", {}).get("en", {}).get("value", entity["id"]),
            artist_ids=sorted(release.artist_ids), genre_qids=genre_qids, genre_labels=labels,
            features={f"wikidata_genre:{qid}": 1.0 for qid in genre_qids},
            revision_id=entity.get("lastrevid", 0), modified_at=entity.get("modified"),
            source_url=f"https://www.wikidata.org/wiki/{entity['id']}",
            claims=[*group_claims, *performer_claims, *artist_claims, *genres, *_claims(entity, "P577")],
        ))
    output.records.sort(key=lambda r: r.release_id)
    output.coverage = {"catalog_releases": len(catalog.releases), "considered_releases": len(queried),
                       "queried_releases": len(queried) if queried_release_ids is not None else None,
                       "exact_release_matches": exact_release_matches, "verified_records": len(output.records),
                       "records_with_genres": sum(bool(r.genre_qids) for r in output.records),
                       "genre_assertions": sum(len(r.genre_qids) for r in output.records),
                       "quarantined": len(output.quarantine),
                       "no_personal_labels": True, "no_popularity_signal": True}
    return output


class WikidataClient:
    """Public endpoints only; each batch, response, total transfer and run are bounded."""

    def __init__(self, http: httpx.Client | None = None, *, delay: float = 1.05,
                 max_requests: int = 24, max_total_bytes: int = 8_000_000,
                 max_response_bytes: int = 2_000_000):
        self.http = http or httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=35,
                                        follow_redirects=False)
        self.owns_client = http is None
        self.delay, self.max_requests = delay, max_requests
        self.max_total_bytes, self.max_response_bytes = max_total_bytes, max_response_bytes
        self.requests = 0
        self.total_bytes = 0

    def close(self):
        if self.owns_client:
            self.http.close()

    def get(self, endpoint: str, params: dict) -> dict:
        if endpoint not in {QUERY_URL, ENTITY_URL}:
            raise ValueError("Only official Wikidata endpoints are allowed")
        if self.requests >= self.max_requests:
            raise ValueError("Wikidata request budget exhausted")
        if self.requests and self.delay:
            time.sleep(self.delay)
        self.requests += 1
        chunks, size = [], 0
        with self.http.stream("GET", endpoint, params=params, headers={"User-Agent": USER_AGENT},
                              follow_redirects=False) as response:
            response.raise_for_status()
            for chunk in response.iter_bytes():
                size += len(chunk)
                self.total_bytes += len(chunk)
                if size > self.max_response_bytes or self.total_bytes > self.max_total_bytes:
                    raise ValueError("Wikidata response/transfer budget exceeded")
                chunks.append(chunk)
        payload = json.loads(b"".join(chunks))
        if "error" in payload:
            raise ValueError("Wikidata returned an API error; no automatic retry")
        return payload


def fetch(catalog: Catalog, output_dir: Path, *, max_releases: int = 100,
          client: WikidataClient | None = None) -> WikidataDataset:
    if not 1 <= max_releases <= 200:
        raise ValueError("Use a bounded sample of 1..200 releases")
    raw_dir = output_dir / "raw"
    if (output_dir / "dataset.json").exists() or raw_dir.exists():
        raise ValueError("Output already exists; choose a fresh snapshot directory")
    raw_dir.mkdir(parents=True)
    api = client or WikidataClient()
    selected = sorted((r for r in catalog.releases if _release_mbid(r)), key=lambda r: r.id)[:max_releases]
    album_ids = set()
    entities = {}
    fetched_at = datetime.now(UTC).isoformat()

    def save(name: str, payload: dict):
        (raw_dir / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2)+"\n")

    def get_entities(ids: list[str], prefix: str):
        for start in range(0, len(ids), 20):
            batch = ids[start:start+20]
            payload = api.get(ENTITY_URL, {"action": "wbgetentities", "ids": "|".join(batch),
                                          "props": "claims|labels|info", "languages": "en",
                                          "format": "json", "maxlag": 5})
            save(f"{prefix}-{start//20:03d}.json", payload)
            entities.update(payload.get("entities", {}))

    try:
        for start in range(0, len(selected), 40):
            batch = selected[start:start+40]
            values = " ".join(json.dumps(_release_mbid(r)) for r in batch)
            query = f"SELECT ?album ?mbid WHERE {{ VALUES ?mbid {{ {values} }} ?album wdt:P436 ?mbid . }} LIMIT {len(batch)*4}"
            (raw_dir / f"lookup-{start//40:03d}.rq").write_text(query+"\n")
            payload = api.get(QUERY_URL, {"query": query, "format": "json"})
            save(f"lookup-{start//40:03d}.json", payload)
            rows = payload.get("results", {}).get("bindings", [])
            if len(rows) >= len(batch)*4:
                raise ValueError("Lookup may be truncated; use smaller targeted batches")
            for row in rows:
                qid = row.get("album", {}).get("value", "").rsplit("/", 1)[-1]
                if QID.fullmatch(qid):
                    album_ids.add(qid)
        if len(album_ids) > 200:
            raise ValueError("Album entity budget exceeded")
        get_entities(sorted(album_ids), "albums")
        dependencies = set()
        for entity in list(entities.values()):
            for prop in ("P136", "P175"):
                dependencies.update(qid for c in _claims(entity, prop) if (qid := _entity_value(c)))
        dependencies -= entities.keys()
        if len(dependencies) > 200:
            raise ValueError("Dependency entity budget exceeded")
        get_entities(sorted(dependencies), "dependencies")
        dataset = normalize(catalog, entities, fetched_at=fetched_at, queried_release_ids=[r.id for r in selected])
        dataset.coverage.update(http_requests=api.requests, transfer_bytes=api.total_bytes,
                                selection_policy="first MBID-identified releases sorted by catalog ID; not a representative popularity sample")
        (output_dir / "dataset.json").write_text(dataset.model_dump_json(indent=2)+"\n")
        (output_dir / "fetch-manifest.json").write_text(json.dumps({
            "source": "wikidata", "license": "CC0-1.0", "license_url": LICENSE_URL,
            "fetched_at": fetched_at, "catalog_sha256": catalog_digest(catalog),
            "queried_release_ids": [r.id for r in selected], "http_requests": api.requests,
            "transfer_bytes": api.total_bytes, "status": "complete",
        }, indent=2)+"\n")
        return dataset
    except (httpx.HTTPError, ValueError, OSError) as exc:
        manifest = {"source": "wikidata", "status": "failed", "error_type": type(exc).__name__,
                    "fetched_at": fetched_at, "http_requests": api.requests,
                    "transfer_bytes": api.total_bytes, "catalog_sha256": catalog_digest(catalog)}
        if isinstance(exc, httpx.HTTPStatusError):
            manifest["http_status"] = exc.response.status_code
        (output_dir / "fetch-manifest.json").write_text(json.dumps(manifest, indent=2)+"\n")
        raise
    finally:
        if client is None:
            api.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--max-releases", type=int, default=100)
    parser.add_argument("--raw-dir", type=Path, help="Replay existing entity JSON without network")
    parser.add_argument("--fetched-at", help="Required source retrieval time for offline replay")
    args = parser.parse_args()
    catalog = Catalog.model_validate_json(args.catalog.read_text())
    if args.raw_dir:
        if not args.fetched_at:
            parser.error("--raw-dir requires --fetched-at")
        entities = {}
        for path in sorted(args.raw_dir.glob("*.json")):
            entities.update(json.loads(path.read_text()).get("entities", {}))
        queried_mbids = set()
        for path in args.raw_dir.glob("*.rq"):
            queried_mbids.update(re.findall(r'"([0-9a-fA-F-]{36})"', path.read_text()))
        queried_ids = ([r.id for r in catalog.releases if _release_mbid(r) in queried_mbids]
                       if queried_mbids else None)
        dataset = normalize(catalog, entities, fetched_at=args.fetched_at, queried_release_ids=queried_ids)
        target = args.output_dir / "dataset.json"
        if target.exists():
            parser.error("Output exists; choose a fresh snapshot directory")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(dataset.model_dump_json(indent=2)+"\n")
    else:
        try:
            dataset = fetch(catalog, args.output_dir, max_releases=args.max_releases)
        except (httpx.HTTPError, ValueError, OSError) as exc:
            status = f" HTTP {exc.response.status_code}" if isinstance(exc, httpx.HTTPStatusError) else ""
            parser.exit(2, f"Wikidata refresh failed: {type(exc).__name__}{status}; no dataset published.\n")
    print(json.dumps(dataset.coverage, indent=2))


if __name__ == "__main__":
    main()
