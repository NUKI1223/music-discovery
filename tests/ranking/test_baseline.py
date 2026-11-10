from datetime import date, datetime, timezone

import pytest

from discovery.contracts import (
    Artist,
    Catalog,
    Genre,
    Observation,
    Release,
    RequestIntent,
    UserProfile,
)
from discovery.ranking import rank, score_candidates


def catalog() -> Catalog:
    genres = [Genre(id="hip", name="Hip hop"), Genre(id="experimental", name="Experimental", parent_ids=["hip"]),
              Genre(id="boom", name="Boom bap", parent_ids=["hip"]), Genre(id="jazz", name="Jazz")]
    artists = [Artist(id=f"a{i}", name=f"Artist {i}", genre_ids=["experimental" if i % 2 else "boom"]) for i in range(40)]
    releases = [Release(id=f"r{i}", title=f"Release {i}", artist_ids=[f"a{i}"],
                        genre_ids=["experimental" if i % 2 else "boom"], year=2020,
                        observations=[Observation(source="fixture", observed_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
                                                  rating=.8, rating_scale=1, rating_count=i+1,
                                                  access_basis="synthetic")]) for i in range(40)]
    releases.append(Release(id="jazz-only", title="Jazz", artist_ids=["a0"], genre_ids=["jazz"], year=2020))
    return Catalog(snapshot_id="test", is_fixture=True, genres=genres, artists=artists, releases=releases)


TODAY = date(2026, 10, 28)


def test_unknown_profile_and_genre_scope_are_honest():
    result = rank(catalog(), RequestIntent(genre_scope_id="jazz"),
                  UserProfile(seed_artist_ids_by_genre={"hip": ["a1"]}), TODAY)
    assert result.profile_basis == "unavailable"
    assert [r.release_id for r in result.items] == ["jazz-only"]
    assert result.items[0].components["novelty"] is None


def test_strict_rarity_uses_cohort_and_ignores_null_stale_future():
    cat = catalog()
    for i in (0, 1, 2):
        cat.releases[i].observations[0].rating_count = None
    cat.releases[3].observations[0].observed_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
    cat.releases[4].observations[0].observed_at = datetime(2027, 1, 1, tzinfo=timezone.utc)
    result = score_candidates(cat, RequestIntent(genre_scope_id="hip", obscurity="require"), UserProfile(), TODAY)
    assert result.items
    assert not {f"r{i}" for i in range(5)} & set(result.candidate_ids)
    assert all(r.components["rarity_eligible"] == 1 for r in result.items)
    assert all(r.components["cohort_size"] >= 30 for r in result.items)


def test_equal_counts_do_not_make_entire_catalog_rare():
    cat = catalog()
    for r in cat.releases:
        for o in r.observations:
            o.rating_count = 1
    result = rank(cat, RequestIntent(genre_scope_id="hip", obscurity="require"), UserProfile(), TODAY)
    assert result.items == []


def test_known_and_locked_subgenre_never_overridden_by_novelty():
    cat = catalog()
    result = rank(cat, RequestIntent(genre_scope_id="hip", mode="surprise", locked_subgenre_ids=["boom"]),
                  UserProfile(known_release_ids=["r0"], seed_artist_ids_by_genre={"hip": ["a1"]}), TODAY)
    assert "r0" not in result.candidate_ids
    assert all(cat.releases[int(r.release_id[1:])].genre_ids == ["boom"] for r in result.items)
    assert "jazz-only" not in result.candidate_ids


def test_scoped_avoid_tags_do_not_penalize_other_genre():
    request = RequestIntent(genre_scope_id="jazz")
    plain = rank(catalog(), request, UserProfile(), TODAY)
    scoped = rank(catalog(), request, UserProfile(avoid_tags_by_genre={"hip": ["jazz"]}), TODAY)
    assert plain.items == scoped.items


def test_modes_change_actual_slate_distance():
    cat = catalog()
    profile = UserProfile(seed_artist_ids_by_genre={"hip": ["a1"]})
    close = rank(cat, RequestIntent(genre_scope_id="hip", mode="close"), profile, TODAY)
    surprise = rank(cat, RequestIntent(genre_scope_id="hip", mode="surprise"), profile, TODAY)
    assert sum(r.components["novelty"] for r in surprise.items) > sum(r.components["novelty"] for r in close.items)
    assert len({cat.releases[int(r.release_id[1:])].artist_ids[0] for r in surprise.items}) == len(surprise.items)


def test_invalid_scope_does_not_fall_back_to_pilot():
    with pytest.raises(ValueError):
        rank(catalog(), RequestIntent(genre_scope_id="unknown"), UserProfile(), TODAY)


def _obs(count, source="fixture", when=datetime(2026, 10, 1, tzinfo=timezone.utc)):
    return Observation(source=source, observed_at=when, rating_count=count, access_basis="synthetic")


def _with_outside_release(*observations, artist="a0"):
    cat = catalog()
    cat.releases.append(Release(id="outside", title="Outside", artist_ids=[artist], genre_ids=["jazz"], year=2020,
                                observations=list(observations)))
    return cat


def _hip_popularity(cat, obscurity="prefer"):
    result = score_candidates(cat, RequestIntent(genre_scope_id="hip", obscurity=obscurity), UserProfile(), TODAY)
    return result, {i.release_id: i.components for i in result.items}


def test_artist_peak_makes_rare_looking_release_of_known_artist_not_rare():
    # r0 has 1 listener (the rarest of the cohort) but its artist has another release with 1000.
    cat = _with_outside_release(_obs(1000))
    cat.releases.append(Release(id="r0-big", title="Big", artist_ids=["a0"], genre_ids=["boom"], year=2020,
                                observations=[_obs(1000)]))
    result, comps = _hip_popularity(cat, "require")
    assert "r0" not in result.candidate_ids
    comps = _hip_popularity(cat)[1]["r0"]
    assert comps["rarity_eligible"] == 0.0
    assert comps["rating_count"] == 1 and comps["artist_peak_count"] == 1000
    plain = _hip_popularity(catalog())[1]["r0"]
    assert plain["rarity_eligible"] == 1.0 and plain["artist_peak_count"] == 1


def test_artist_peak_counts_release_outside_requested_scope():
    comps = _hip_popularity(_with_outside_release(_obs(500)))[1]["r0"]
    assert comps["rating_count"] == 1 and comps["artist_peak_count"] == 500 and comps["rarity_eligible"] == 0.0


@pytest.mark.parametrize("ignored", [_obs(500, when=datetime(2025, 1, 1, tzinfo=timezone.utc)),
                                     _obs(500, when=datetime(2027, 1, 1, tzinfo=timezone.utc)),
                                     _obs(None), _obs(500, source="other")])
def test_artist_peak_ignores_stale_future_null_and_other_source(ignored):
    comps = _hip_popularity(_with_outside_release(ignored))[1]["r0"]
    assert comps["artist_peak_count"] == 1 and comps["rarity_eligible"] == 1.0


def test_listenbrainz_is_preferred_source_over_musicbrainz():
    cat = catalog()
    for i, r in enumerate(cat.releases[:40]):
        r.observations = [_obs(i+1, source="musicbrainz"), _obs(i+1, source="listenbrainz")]
    comps = _hip_popularity(cat)[1]["r5"]
    assert comps["popularity_source"] == "listenbrainz" and comps["cohort_id"].startswith("listenbrainz:")


def test_single_release_artist_matches_plain_cohort_values():
    comps = _hip_popularity(catalog())[1]
    assert all(c["artist_peak_count"] == c["rating_count"] for c in comps.values() if c["rating_count"] is not None)
    assert comps["r9"]["rarity"] == pytest.approx(1-10/40) and comps["r9"]["cohort_size"] == 40


def _mb(count, when=datetime(2026, 10, 1, tzinfo=timezone.utc)):
    return _obs(count, source="musicbrainz", when=when)


def _popularity_of(cat):
    from discovery.ranking.baseline import _popularity
    return _popularity(cat, "hip", TODAY)


def test_stale_source_does_not_hide_fresh_source():
    cat = catalog()
    for i, r in enumerate(cat.releases[:40]):
        r.observations = [_mb(i+1)]
    stale = datetime(2025, 1, 1, tzinfo=timezone.utc)
    cat.releases[5].observations.append(_obs(6, source="listenbrainz", when=stale))
    value = _popularity_of(cat)["r5"]
    assert value["popularity_source"] == "musicbrainz" and value["rating_count"] == 6
    assert value["cohort_id"].startswith("musicbrainz:") and value["rarity"] is not None


def test_only_stale_sources_keep_old_alphabetical_choice_with_empty_values():
    cat = catalog()
    stale = datetime(2025, 1, 1, tzinfo=timezone.utc)
    cat.releases[5].observations = [_mb(6, when=stale), _obs(6, source="listenbrainz", when=stale)]
    value = _popularity_of(cat)["r5"]
    assert value["popularity_source"] == "listenbrainz"
    assert value["rarity"] is None and value["rating_count"] is None
    assert value["artist_peak_count"] is None and value["rarity_eligible"] == 0.0


def test_single_stale_source_gives_empty_values():
    cat = catalog()
    cat.releases[5].observations[0].observed_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
    value = _popularity_of(cat)["r5"]
    assert value["popularity_source"] == "fixture"
    assert value["rarity"] is None and value["rating_count"] is None
    assert value["artist_peak_count"] is None and value["cohort_id"] is None


def test_single_fresh_source_with_null_count_keeps_null_count():
    cat = catalog()
    cat.releases[5].observations[0].rating_count = None
    value = _popularity_of(cat)["r5"]
    assert value["popularity_source"] == "fixture" and value["rating_count"] is None
    assert value["artist_peak_count"] is None and value["rarity_eligible"] == 0.0


def test_own_zero_count_is_never_rarity_eligible():
    cat = catalog()
    for i, r in enumerate(cat.releases[:40]):
        r.observations = [_mb(1 if i == 9 else 0)]
    # r10..r39 have own 0 but a big release by the same artist, which lifts their effective count.
    for i in range(10, 40):
        cat.releases.append(Release(id=f"big{i}", title="Big", artist_ids=[f"a{i}"],
                                    genre_ids=["jazz"], year=2020, observations=[_mb(1000)]))
    popularity = _popularity_of(cat)
    assert popularity["r0"]["rarity"] is not None and popularity["r0"]["cohort_size"] >= 30
    assert popularity["r0"]["rating_count"] == 0 and popularity["r0"]["rarity_eligible"] == 0.0
    assert popularity["r9"]["rarity_eligible"] == 1.0  # control: positive count stays eligible


@pytest.mark.parametrize("name", ["Various Artists", " various ARTISTS ", "[unknown artist]",
                                  "[Traditional]"])
def test_collective_artists_do_not_share_a_peak(name):
    cat = _with_outside_release(_obs(1000))
    cat.artists.append(Artist(id="va", name=name, genre_ids=["boom"]))
    cat.releases[0].artist_ids = ["va"]
    cat.releases.append(Release(id="va-big", title="Comp", artist_ids=["va"], genre_ids=["jazz"],
                                year=2020, observations=[_obs(1000)]))
    value = _popularity_of(cat)["r0"]
    assert value["rating_count"] == 1 and value["artist_peak_count"] == 1
    assert value["rarity_eligible"] == 1.0


def test_collective_artist_does_not_block_real_coartist_peak():
    cat = _with_outside_release(_obs(1000))
    cat.artists.append(Artist(id="va", name="Various Artists", genre_ids=["boom"]))
    cat.releases[0].artist_ids = ["va", "a0"]
    value = _popularity_of(cat)["r0"]
    assert value["artist_peak_count"] == 1000 and value["rarity_eligible"] == 0.0
