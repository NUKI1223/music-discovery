"""Independent fixtures: request intent must change order without inventing audio facts."""

import pytest

from discovery.contracts import (
    Artist,
    Catalog,
    Genre,
    Release,
    RequestIntent,
    SourceMetadata,
    UserProfile,
)
from discovery.ranking import rank, score_candidates


def fixture():
    genres = [Genre(id="hip-hop", name="Hip Hop")]
    genres += [Genre(id=g, name=g, parent_ids=["hip-hop"]) for g in ("near", "far", "edge")]
    artists = [Artist(id=f"a{i}", name=f"Performer {i}", genre_ids=["near"]) for i in range(8)]
    releases = [Release(id=f"r{i}", title=f"Album {i}", artist_ids=[f"a{i}"], genre_ids=["near"])
                for i in range(8)]
    for i, descriptors in enumerate((["mellow"], ["aggressive"], [])):
        releases[i].source_metadata = [SourceMetadata(source="rym", source_row_id=f"row{i}",
            title=releases[i].title, artist_credit=artists[i].name, descriptors=descriptors)]
    return Catalog(snapshot_id="request-context", is_fixture=True, genres=genres,
                   artists=artists, releases=releases)


def test_source_tags_change_order_unknown_is_neutral():
    cat = fixture()
    request = RequestIntent(genre_scope_id="hip-hop", preferred_descriptors=["aggressive"],
                            avoided_descriptors=["mellow"])
    items = {i.release_id: i for i in score_candidates(cat, request, UserProfile()).items}
    assert items["r1"].score > items["r2"].score > items["r0"].score
    assert items["r2"].components["sound_adjustment"] == 0
    assert "sound_descriptors_unavailable" in items["r2"].uncertainty_flags
    # More source rows carrying the same subjective tag must not inflate its weight.
    cat.releases[1].source_metadata *= 2
    assert score_candidates(cat, request, UserProfile()).items[0].score == items["r1"].score


def test_request_reference_overrides_general_likes_without_changing_profile():
    cat = fixture()
    cat.artists[0].genre_ids = ["far"]
    cat.releases[0].genre_ids = ["far"]
    cat.releases[1].genre_ids = ["far"]
    profile = UserProfile(liked_release_ids_by_genre={"hip-hop": ["r3"]})
    original = profile.model_dump()
    req = RequestIntent(genre_scope_id="hip-hop", reference_artist_ids=["a0"], excluded_artist_ids=["a0"])
    items = {i.release_id: i for i in score_candidates(cat, req, profile).items}
    assert "r0" not in items
    assert items["r1"].components["similarity"] == 1
    assert items["r4"].components["similarity"] == 0
    assert profile.model_dump() == original


def test_surprise_prefers_further_candidate_within_exploration_pool():
    cat = fixture()
    cat.artists[0].genre_ids = ["near"]
    cat.releases[1].genre_ids = ["near", "far", "edge"]  # distance 2/3
    cat.releases[2].genre_ids = ["far"]  # distance 1
    profile = UserProfile(seed_artist_ids_by_genre={"hip-hop": ["a0"]})
    explore = {i.release_id: i for i in score_candidates(cat, RequestIntent(genre_scope_id="hip-hop"), profile).items}
    surprise = {i.release_id: i for i in score_candidates(cat, RequestIntent(genre_scope_id="hip-hop", mode="surprise"), profile).items}
    assert explore["r1"].score > explore["r2"].score
    assert surprise["r2"].score > surprise["r1"].score


def test_soft_quota_does_not_shorten_slate_or_relax_hard_filters():
    cat = fixture()
    profile = UserProfile(seed_artist_ids_by_genre={"hip-hop": ["a0"]})
    req = RequestIntent(genre_scope_id="hip-hop", mode="surprise", locked_subgenre_ids=["near"], excluded_artist_ids=["a0"])
    result = rank(cat, req, profile)
    assert len(result.items) == 5
    assert "r0" not in {i.release_id for i in result.items}
    assert any("novelty balance unavailable" in warning for warning in result.warnings)
    cat.releases = cat.releases[:3]
    assert len(rank(cat, req, profile).items) == 2  # genuinely insufficient candidates
    strict = req.model_copy(update={"obscurity": "require"})
    assert not rank(cat, strict, profile).items  # no fresh rarity evidence


@pytest.mark.parametrize("changes", [
    {"reference_artist_ids": ["invented"]}, {"preferred_descriptors": ["rapping_only"]},
    {"preferred_descriptors": ["aggressive"], "avoided_descriptors": ["aggressive"]},
])
def test_invalid_wishes_rejected(changes):
    with pytest.raises(ValueError):
        RequestIntent(genre_scope_id="hip-hop", **changes).validate_catalog(fixture())


def test_no_metadata_does_not_promote_to_measured_popularity():
    result = rank(fixture(), RequestIntent(genre_scope_id="hip-hop", preferred_descriptors=["aggressive"]), UserProfile())
    assert all(i.components["rarity"] is None for i in result.items)
    assert all("popularity_unknown_or_insufficient" in i.uncertainty_flags for i in result.items)


def test_explore_prefers_new_artists_but_familiarity_is_not_a_ban_or_public_fame():
    cat = fixture()
    profile = UserProfile(seed_artist_ids_by_genre={"hip-hop": ["a0"]})
    close = {i.release_id: i for i in score_candidates(cat, RequestIntent(genre_scope_id="hip-hop", mode="close"), profile).items}
    assert close["r0"].score == close["r3"].score
    explore = {i.release_id: i for i in score_candidates(cat, RequestIntent(genre_scope_id="hip-hop"), profile).items}
    assert explore["r0"].score < explore["r3"].score
    assert explore["r0"].components["rarity"] is None
    assert "personally_familiar_artist" in explore["r0"].uncertainty_flags
    profile.seed_artist_ids_by_genre["hip-hop"] = [a.id for a in cat.artists]
    assert len(rank(cat, RequestIntent(genre_scope_id="hip-hop"), profile).items) == 5
