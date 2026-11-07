import json
from copy import deepcopy

import httpx
import pytest

from discovery.catalog.wikidata import ENTITY_URL, QUERY_URL, WikidataClient, fetch, normalize
from discovery.contracts import Artist, Catalog, Genre, Release
from discovery.storage import catalog_digest

RELEASE = "aaaaaaaa-1111-4111-8111-111111111111"
ARTIST = "bbbbbbbb-2222-4222-8222-222222222222"
OTHER = "cccccccc-3333-4333-8333-333333333333"


def assertion(entity, prop, value, rank="normal"):
    return {"id": f"{entity}${prop}", "rank": rank,
            "mainsnak": {"snaktype": "value", "datavalue": {"value": value}}, "references": []}


def fixture():
    catalog = Catalog(snapshot_id="synthetic-test", is_fixture=True,
                      genres=[Genre(id="hip", name="Hip hop")],
                      artists=[Artist(id=ARTIST, name="A")],
                      releases=[Release(id=RELEASE, title="An album", artist_ids=[ARTIST], genre_ids=["hip"])])
    entities = {
        "Q1": {"id": "Q1", "lastrevid": 12, "modified": "2026-10-28T00:00:00Z",
               "labels": {"en": {"value": "An album"}},
               "claims": {"P436": [assertion("Q1", "P436", RELEASE)],
                          "P175": [assertion("Q1", "P175", {"id": "Q2"})],
                          "P136": [assertion("Q1", "P136", {"id": "Q3"})]}},
        "Q2": {"id": "Q2", "claims": {"P434": [assertion("Q2", "P434", ARTIST)]}},
        "Q3": {"id": "Q3", "labels": {"en": {"value": "Experimental hip hop"}}},
    }
    return catalog, entities


def test_exact_group_and_artist_join_preserves_provenance_and_independent_features():
    catalog, entities = fixture()
    before = catalog.model_dump_json()
    dataset = normalize(catalog, entities, fetched_at="2026-10-28T00:00:00Z")
    assert dataset.catalog_sha256 == catalog_digest(catalog)
    assert dataset.is_fixture
    assert dataset.license == "CC0-1.0"
    assert dataset.records[0].release_id == RELEASE
    assert dataset.records[0].features == {"wikidata_genre:Q3": 1}
    assert {c.property_id for c in dataset.records[0].claims} == {"P436", "P175", "P434", "P136"}
    assert catalog.model_dump_json() == before
    assert "rating" not in type(dataset.records[0]).model_fields


def test_same_title_is_never_an_identity_match():
    catalog, entities = fixture()
    entities["Q1"]["claims"]["P436"][0]["mainsnak"]["datavalue"]["value"] = OTHER
    result = normalize(catalog, entities, fetched_at="2026-10-28T00:00:00Z")
    assert result.records == []
    assert result.coverage["exact_release_matches"] == 0


@pytest.mark.parametrize("case,reason", [("artist_conflict", "artist_identity_conflict"),
                                        ("artist_missing", "artist_identity_unverified"),
                                        ("multiple_groups", "ambiguous_release_group_ids"),
                                        ("duplicate_entities", "multiple_wikidata_items_for_mbid")])
def test_ambiguous_or_conflicting_identities_are_quarantined(case, reason):
    catalog, entities = fixture()
    if case == "artist_conflict":
        entities["Q2"]["claims"]["P434"] = [assertion("Q2", "P434", OTHER)]
    elif case == "artist_missing":
        entities.pop("Q2")
    elif case == "multiple_groups":
        entities["Q1"]["claims"]["P436"].append(assertion("Q1", "P436", OTHER))
    else:
        entities["Q4"] = deepcopy(entities["Q1"])
        entities["Q4"]["id"] = "Q4"
    result = normalize(catalog, entities, fetched_at="2026-10-28T00:00:00Z")
    assert result.records == []
    assert result.quarantine[0]["reason"] == reason


def test_deprecated_claim_is_not_a_current_feature():
    catalog, entities = fixture()
    entities["Q1"]["claims"]["P136"][0]["rank"] = "deprecated"
    result = normalize(catalog, entities, fetched_at="2026-10-28T00:00:00Z")
    assert len(result.records) == 1
    assert result.records[0].features == {}


def test_fetch_uses_bounded_public_endpoints_and_writes_replayable_snapshot(tmp_path):
    catalog, entities = fixture()
    requests = []
    def handler(request):
        requests.append(request)
        if str(request.url).startswith(QUERY_URL):
            return httpx.Response(200, json={"results": {"bindings": [
                {"album": {"value": "http://www.wikidata.org/entity/Q1"}, "mbid": {"value": RELEASE}}]}})
        ids = request.url.params["ids"].split("|")
        return httpx.Response(200, json={"entities": {qid: entities[qid] for qid in ids}})
    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = WikidataClient(http, delay=0)
    dataset = fetch(catalog, tmp_path / "new", client=client)
    assert len(requests) == 3
    assert all(r.url.host in {"query.wikidata.org", "www.wikidata.org"} for r in requests)
    assert dataset.coverage["verified_records"] == 1
    saved = json.loads((tmp_path / "new" / "dataset.json").read_text())
    assert saved["records"][0]["features"] == {"wikidata_genre:Q3": 1}
    with pytest.raises(ValueError, match="Output already"):
        fetch(catalog, tmp_path / "new", client=client)


def test_response_size_and_endpoint_allowlist_are_enforced():
    client = WikidataClient(httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=b"x"*200))), delay=0, max_response_bytes=100)
    with pytest.raises(ValueError, match="Only official"):
        client.get("https://example.com", {})
    with pytest.raises(ValueError, match="budget exceeded"):
        client.get(ENTITY_URL, {})


def test_denied_refresh_stops_and_records_failure_without_publishing(tmp_path):
    catalog, _ = fixture()
    client = WikidataClient(httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(403))), delay=0)
    destination = tmp_path / "denied"
    with pytest.raises(httpx.HTTPStatusError):
        fetch(catalog, destination, client=client)
    assert client.requests == 1
    assert not (destination / "dataset.json").exists()
    report = json.loads((destination / "fetch-manifest.json").read_text())
    assert report["status"] == "failed"
    assert report["http_status"] == 403


def test_replay_reports_only_previously_queried_release_coverage():
    catalog, entities = fixture()
    catalog.releases.append(Release(id=OTHER, title="Unqueried", artist_ids=[ARTIST], genre_ids=["hip"]))
    replay = normalize(catalog, entities, fetched_at="2026-10-28T00:00:00Z", queried_release_ids=[RELEASE])
    assert replay.coverage["catalog_releases"] == 2
    assert replay.coverage["queried_releases"] == 1
    assert replay.coverage["verified_records"] == 1
