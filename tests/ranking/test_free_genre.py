from datetime import date

import pytest

from discovery.contracts import (
    Artist,
    ArtistPopularityLabel,
    Catalog,
    Genre,
    Release,
    RequestIntent,
    SourceMetadata,
    UserProfile,
)
from discovery.ranking import rank, score_candidates
from discovery.storage import catalog_digest


@pytest.fixture
def catalog():
    roots = ["rock", "jazz", "pop", "folk", "electronic"]
    genres = [Genre(id=g, name=g.title()) for g in roots]
    genres += [Genre(id=f"{g}-{n}", name=f"{g} style {n}", parent_ids=[g]) for g in roots for n in range(2)]
    artists = [Artist(id=f"a-{g}-{n}", name=f"Artist {g} {n}") for g in roots for n in range(2)]
    return Catalog(snapshot_id="free-fixture", is_fixture=True, genres=genres, artists=artists,
        releases=[Release(id=f"{g}-{n}", title=f"Album {g} {n}", artist_ids=[f"a-{g}-{n}"],
                          genre_ids=[f"{g}-{n}"], year=2025,
                          source_metadata=[SourceMetadata(source="aoty", source_row_id=f"row-{g}-{n}",
                              title=f"Album {g} {n}", artist_credit=f"Artist {g} {n}", vibes=["soft"])])
                  for g in roots for n in range(2)])


def test_free_scope_is_virtual_and_diversifies_equal_candidates(catalog):
    before = catalog_digest(catalog)
    result = rank(catalog, RequestIntent(genre_scope_id="all"), UserProfile())
    assert len(result.candidate_ids) == 10
    assert {i.release_id.rsplit("-", 1)[0] for i in result.items} == {"rock", "jazz", "pop", "folk", "electronic"}
    assert catalog_digest(catalog) == before
    assert "all" not in {g.id for g in catalog.genres}
    assert all("all_genres_selection" in i.reason_codes for i in result.items)


def test_all_taste_and_exclusions_survive_cross_genre_selection(catalog):
    profile = UserProfile(seed_artist_ids_by_genre={"rock": ["a-rock-0"]},
        known_release_ids=["pop-0"], liked_release_ids_by_genre={"jazz": ["jazz-0"]},
        disliked_release_ids_by_genre={"folk": ["folk-0"]}, avoid_tags_by_genre={"rock": ["electronic"]},
        request_only_artist_ids=["a-rock-1"], limit_widely_known_artists=True,
        max_widely_known_artists=1, artist_popularity_labels={
            a.id: ArtistPopularityLabel(classification="widely_known" if a.id in {"a-pop-1", "a-jazz-1"} else "less_known", evidence="Synthetic explicit label")
            for a in catalog.artists})
    result = rank(catalog, RequestIntent(genre_scope_id="all"), profile)
    assert result.profile_basis == "rated_releases"
    assert not {"pop-0", "jazz-0", "folk-0", "rock-1"} & set(result.candidate_ids)
    assert sum(i.release_id in {"pop-1", "jazz-1"} for i in result.items) <= 1
    scored = score_candidates(catalog, RequestIntent(genre_scope_id="all"), profile)
    assert all(i.components["avoidance"] == 1 for i in scored.items if i.release_id.startswith("electronic"))
    # Explicit ratings given in free mode are respected in a later scoped search.
    local = rank(catalog, RequestIntent(genre_scope_id="rock"),
                 UserProfile(liked_release_ids_by_genre={"all": ["rock-0"]}))
    assert "rock-0" not in local.candidate_ids


def test_all_uses_existing_artist_taste_without_copying_profile(catalog):
    profile = UserProfile(seed_artist_ids_by_genre={"rock": ["a-rock-0"], "jazz": ["a-jazz-0"]})
    before = profile.model_dump()
    result = score_candidates(catalog, RequestIntent(genre_scope_id="all"), profile)
    assert result.profile_basis == "artist_prior"
    assert profile.model_dump() == before


def test_all_respects_locks_descriptors_and_unknown_popularity(catalog):
    request = RequestIntent(genre_scope_id="all", locked_subgenre_ids=["rock", "jazz"], preferred_descriptors=["soft"])
    result = rank(catalog, request, UserProfile(), date(2026, 11, 4))
    assert len(result.candidate_ids) == 4
    assert all(i.components["sound_adjustment"] > 0 for i in result.items)
    assert rank(catalog, request.model_copy(update={"obscurity": "require"}), UserProfile()).items == []
    with pytest.raises(ValueError):
        RequestIntent(genre_scope_id="all", locked_subgenre_ids=["all"]).validate_catalog(catalog)
    with pytest.raises(ValueError):
        RequestIntent(genre_scope_id="made-up").validate_catalog(catalog)


def test_unclassified_releases_can_be_explored_without_claiming_a_genre(catalog):
    catalog.releases[0].genre_ids = []
    assert catalog.releases[0].id in rank(catalog, RequestIntent(genre_scope_id="all"), UserProfile()).candidate_ids
    assert catalog.releases[0].id not in rank(catalog, RequestIntent(genre_scope_id="rock"), UserProfile()).candidate_ids
    scored = score_candidates(catalog, RequestIntent(genre_scope_id="all"), UserProfile())
    unknown = next(i for i in scored.items if i.release_id == catalog.releases[0].id)
    assert unknown.components["missing_genre_penalty"] == .05
    assert "genre_unknown" in unknown.uncertainty_flags


def test_free_scope_never_pools_unrelated_genres_into_rarity_cohort():
    from discovery.contracts import Observation

    genres = [Genre(id=g, name=g) for g in ["jazz", "rock"]]
    artists = [Artist(id=str(i), name=f"Artist {i}") for i in range(30)]
    releases = [Release(id=str(i), title=f"Album {i}", artist_ids=[str(i)], year=2025,
        genre_ids=["jazz" if i < 15 else "rock"], observations=[Observation(
            source="test", observed_at="2026-11-04T00:00:00Z", rating_count=i + 1,
            access_basis="synthetic cohort test")]) for i in range(30)]
    catalog = Catalog(snapshot_id="cohorts", genres=genres, artists=artists, releases=releases)
    result = score_candidates(catalog, RequestIntent(genre_scope_id="all"), UserProfile(), date(2026, 11, 4))
    assert len(result.items) == 30
    assert all(i.components["rarity"] is None for i in result.items)
    assert rank(catalog, RequestIntent(genre_scope_id="all", obscurity="require"), UserProfile(), date(2026, 11, 4)).items == []
