from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest

from discovery.catalog import load_catalog
from discovery.catalog.musicbrainz import MusicBrainzClient, normalize_pages

OBSERVED = datetime(2026, 10, 28, tzinfo=UTC)


def page():
    return {"count": 55, "release-groups": [{
        "id": "album-a", "title": "Album A", "primary-type": "Album",
        "first-release-date": "2010-10-10", "score": 100, "count": 99,
        "artist-credit": [{"artist": {"id": "artist-a", "name": "Artist A"}}],
        "tags": [{"name": "jazz rap", "count": 2}, {"name": "hip hop", "count": 0}],
        "releases": [{"id": "edition-1", "status": "Official"},
                     {"id": "edition-2", "status": "Official"}],
    }]}


def test_search_relevance_and_editions_are_not_ratings():
    catalog, report = normalize_pages([page(), page()], observed_at=OBSERVED)
    assert len(catalog.releases) == 1
    assert report["duplicate_ids"] == ["album-a"]
    obs = catalog.releases[0].observations[0]
    assert obs.rating is None and obs.rating_count is None
    assert catalog.releases[0].genre_ids == ["jazz-rap"]
    assert catalog.artists[0].genre_ids == []


def test_only_explicit_lookup_rating_and_original_scale_are_kept():
    catalog, _ = normalize_pages([page()], observed_at=OBSERVED, detail_by_id={
        "album-a": {"id": "album-a", "rating": {"value": 4.4, "votes-count": 18}},
    })
    obs = catalog.releases[0].observations[0]
    assert (obs.rating, obs.rating_scale, obs.rating_count) == (4.4, 5, 18)
    assert "CC BY-NC-SA 3.0" in obs.access_basis


def test_mismatched_detail_does_not_assign_another_albums_rating():
    with pytest.raises(ValueError, match="detail ID"):
        normalize_pages([page()], observed_at=OBSERVED, detail_by_id={
            "album-a": {"id": "another-album", "rating": {"value": 4.4, "votes-count": 18}},
        })


def test_missing_tags_remain_missing_and_ids_are_not_title_matched():
    payload = page()
    payload["release-groups"][0].pop("tags")
    catalog, _ = normalize_pages([payload], observed_at=OBSERVED)
    assert catalog.releases[0].genre_ids == []
    assert catalog.releases[0].id == "album-a"


def test_bootlegs_and_invalid_rows_are_reported_not_recommended():
    payload = page()
    payload["release-groups"][0]["releases"] = [{"status": "Bootleg"}]
    payload["release-groups"].append(None)
    catalog, report = normalize_pages([payload], observed_at=OBSERVED)
    assert not catalog.releases
    assert {row["reason"] for row in report["skipped"]} == {
        "only_bootleg_editions", "record_is_not_an_object",
    }


def test_no_naive_capture_dates():
    with pytest.raises(ValueError, match="timezone"):
        normalize_pages([page()], observed_at=datetime(2026, 10, 28))


def test_client_rate_limit_and_retry_are_serial_and_bounded(monkeypatch):
    from discovery.catalog import musicbrainz

    state = {"now": 100.0, "calls": []}

    def sleep(delay):
        state["now"] += delay

    def transport(request):
        assert request.url.host == "musicbrainz.org"
        assert "MusicDiscovery/0.1" in request.headers["User-Agent"]
        state["calls"].append(state["now"])
        if len(state["calls"]) == 1:
            return httpx.Response(503)
        return httpx.Response(200, json=page())

    monkeypatch.setattr(musicbrainz, "time", SimpleNamespace(
        monotonic=lambda: state["now"], sleep=sleep,
    ))
    monkeypatch.setattr(MusicBrainzClient, "_last_request", 0)
    client = MusicBrainzClient(transport=httpx.MockTransport(transport))
    try:
        client.search_release_groups("tag:jazz")
        client.search_release_groups("tag:jazz")
    finally:
        client.close()
    assert len(state["calls"]) == 3
    assert all(b - a >= 1.09 for a, b in zip(state["calls"], state["calls"][1:]))


def test_client_does_not_retry_permanent_errors(monkeypatch):
    calls = []

    def transport(request):
        calls.append(request)
        return httpx.Response(403)

    monkeypatch.setattr(MusicBrainzClient, "_last_request", 0)
    client = MusicBrainzClient(transport=httpx.MockTransport(transport))
    try:
        with pytest.raises(httpx.HTTPStatusError):
            client.search_release_groups("tag:jazz")
    finally:
        client.close()
    assert len(calls) == 1


def test_checked_in_real_sample_is_separate_and_canonical():
    catalog = load_catalog("data/samples/musicbrainz.json")
    assert not catalog.is_fixture
    assert len(catalog.releases) == 33
    assert len({r.id for r in catalog.releases}) == 33
    counts = [o.rating_count for r in catalog.releases for o in r.observations]
    assert sum(count is not None for count in counts) == 1
    assert next(count for count in counts if count is not None) == 18


def test_empty_lookup_genres_preserve_source_search_tags_with_provenance():
    catalog, report = normalize_pages([page()], observed_at=OBSERVED, detail_by_id={
        "album-a": {"id": "album-a", "genres": [], "rating": {"value": None, "votes-count": 0}},
    })
    release = catalog.releases[0]
    assert release.genre_ids == ["jazz-rap"]
    assert release.observations[0].rating_count == 0
    assert release.observations[0].rating is None
    assert report["genre_evidence"][release.id]["tag_sources"]["search_tags"]
    assert report["genre_evidence"][release.id]["mapping_version"] == "mb-tags-v2"


def test_latest_explicit_nonpositive_tag_overrides_indexed_vote():
    catalog, _ = normalize_pages([page()], observed_at=OBSERVED, detail_by_id={
        "album-a": {"id": "album-a", "genres": [{"name": "jazz rap", "count": -1}]},
    })
    assert catalog.releases[0].genre_ids == []


@pytest.mark.parametrize("tags,expected", [
    (["trap"], []), (["trap", "trap edm", "electronic"], ["electronic"]),
    (["trap", "hip hop"], ["hip-hop", "trap"]),
    (["trap", "experimental hip hop"], ["experimental", "trap"]),
])
def test_trap_needs_release_level_hiphop_support(tags, expected):
    payload = page()
    payload["release-groups"][0]["tags"] = [{"name": name, "count": 1} for name in tags]
    catalog, report = normalize_pages([payload], observed_at=OBSERVED)
    assert catalog.releases[0].genre_ids == expected
    if "trap" not in expected:
        assert report["genre_evidence"]["album-a"]["withheld_genre_ids"]["trap"]


def test_per_lookup_capture_time_overrides_collection_time():
    old = datetime(2024, 1, 2, tzinfo=UTC)
    catalog, _ = normalize_pages([page()], observed_at=OBSERVED,
                                 observed_at_by_id={"album-a": old})
    assert catalog.releases[0].observations[0].observed_at == old


def test_invalid_lookup_id_cannot_target_arbitrary_endpoint():
    client = MusicBrainzClient(transport=httpx.MockTransport(
        lambda request: pytest.fail("invalid ID made a request"),
    ))
    try:
        with pytest.raises(ValueError):
            client.lookup_release_group("../../artist/")
    finally:
        client.close()


def test_artist_lookup_searches_aliases_and_escapes_input(monkeypatch):
    seen = []
    monkeypatch.setattr(MusicBrainzClient, "_last_request", 0)

    def transport(request):
        seen.append(request.url.params["query"])
        return httpx.Response(200, json={"artists": []})

    client = MusicBrainzClient(transport=httpx.MockTransport(transport))
    try:
        client.search_artists('A "quoted" name')
    finally:
        client.close()
    assert seen == ['artist:"A \\"quoted\\" name" OR alias:"A \\"quoted\\" name"']
