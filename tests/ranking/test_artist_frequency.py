"""An occasional style must not define an artist's whole discography."""

import pytest

from discovery.contracts import Artist, Catalog, Genre, Release, RequestIntent, UserProfile
from discovery.ranking import score_candidates


def catalog(scope):
    return Catalog(snapshot_id="independent-artist-frequency", is_fixture=True,
        genres=[Genre(id=scope, name=scope),
                Genre(id="core", name="Core", parent_ids=[scope]),
                Genre(id="edge", name="Edge", parent_ids=[scope])],
        artists=[Artist(id="reference", name="Reference", genre_ids=[scope, "core", "edge"]),
                 Artist(id="regular", name="Regular"), Artist(id="occasional", name="Occasional")],
        releases=[Release(id=f"reference-{i}", title=f"Reference {i}", artist_ids=["reference"],
                          genre_ids=["core" if i < 9 else "edge"]) for i in range(10)] +
                 [Release(id="regular", title="Regular", artist_ids=["regular"], genre_ids=["core"]),
                  Release(id="occasional", title="Occasional", artist_ids=["occasional"], genre_ids=["edge"])])


def scores(cat, **kwargs):
    request = RequestIntent(genre_scope_id=cat.genres[0].id, reference_artist_ids=["reference"],
                            excluded_artist_ids=["reference"])
    return {item.release_id: item for item in score_candidates(cat, request, UserProfile(), **kwargs).items}


@pytest.mark.parametrize("scope", ["hip-hop", "jazz", "electronic"])
def test_frequent_style_outranks_occasional_style_across_genres(scope):
    items = scores(catalog(scope))
    assert items["regular"].components["similarity"] > items["occasional"].components["similarity"]
    assert items["regular"].score > items["occasional"].score


def test_artist_union_and_broad_only_releases_do_not_dilute_specific_evidence():
    cat = catalog("jazz")
    before = scores(cat)
    cat.releases += [Release(id=f"unknown-{i}", title=f"Unknown {i}", artist_ids=["reference"],
                            genre_ids=["jazz"]) for i in range(15)]
    assert scores(cat) == before
    cat.releases = [r for r in cat.releases if r.id != "reference-9"]
    after = scores(cat)
    assert after["regular"].components["similarity"] == 1
    assert after["occasional"].components["similarity"] == 0


def test_legacy_features_remain_reproducible():
    items = scores(catalog("jazz"), feature_version="genre-tags-v1")
    assert items["regular"].components["similarity"] == items["occasional"].components["similarity"] == .5


def test_artist_tags_fallback_when_no_specific_release_evidence():
    cat = catalog("jazz")
    for r in cat.releases:
        if "reference" in r.artist_ids:
            r.genre_ids = ["jazz"]
    cat.artists[0].genre_ids = ["jazz", "core"]
    items = scores(cat)
    assert items["regular"].components["similarity"] == 1
    assert items["occasional"].components["similarity"] == 0


def test_positive_reference_reason_requires_actual_overlap():
    cat = catalog("jazz")
    cat.releases = [r for r in cat.releases if r.id != "reference-9"]
    items = scores(cat)
    assert "request_artist_reference" in items["regular"].reason_codes
    assert "request_artist_reference" not in items["occasional"].reason_codes


def test_liked_release_evidence_keeps_priority_over_artist_frequencies():
    cat = catalog("jazz")
    profile = UserProfile(seed_artist_ids_by_genre={"jazz": ["reference"]},
                          liked_release_ids_by_genre={"jazz": ["reference-9"]})
    items = {i.release_id: i for i in score_candidates(cat, RequestIntent(genre_scope_id="jazz"), profile).items}
    assert items["occasional"].components["similarity"] == 1
    assert items["regular"].components["similarity"] == 0


def test_one_many_style_excursion_does_not_outweigh_eight_core_releases():
    cat = catalog("electronic")
    cat.releases = [r for r in cat.releases if r.id != "reference-8"]
    edges = ["edge", *[f"edge-{i}" for i in range(5)]]
    cat.genres += [Genre(id=tag, name=tag, parent_ids=["electronic"]) for tag in edges[1:]]
    for r in cat.releases:
        if r.id in {"reference-9", "occasional"}:
            r.genre_ids = edges
    old = scores(cat, feature_version="genre-tags-v1")
    assert old["regular"].components["similarity"] < old["occasional"].components["similarity"]
    current = scores(cat)
    assert current["regular"].components["similarity"] > current["occasional"].components["similarity"]
    cat.releases.reverse()
    cat.artists.reverse()
    assert scores(cat) == current


def test_scope_only_artist_does_not_imply_similarity_to_all_scope_releases():
    cat = catalog("jazz")
    cat.artists[0].genre_ids = ["jazz"]
    for release in cat.releases:
        if "reference" in release.artist_ids:
            release.genre_ids = ["jazz"]
    items = scores(cat)
    assert all(item.components["novelty"] is None for item in items.values())
    assert all("unpersonalized" in item.uncertainty_flags for item in items.values())
