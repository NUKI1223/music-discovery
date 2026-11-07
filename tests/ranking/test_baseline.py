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
