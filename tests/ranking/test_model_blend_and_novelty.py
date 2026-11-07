"""Learned model is a bounded correction to the baseline; uninformative tags are not 'far'."""

from datetime import date

import pytest

from discovery.contracts import Artist, Catalog, Genre, Release, RequestIntent, UserProfile
from discovery.ranking.baseline import (
    FEATURE_VERSION,
    MODEL_WEIGHT,
    RANKER_VERSION,
    exploration_candidates,
    rank,
    score_candidates,
)

TODAY = date(2026, 10, 28)


def catalog() -> Catalog:
    genres = [Genre(id="hip", name="Hip hop"), Genre(id="boom", name="Boom bap", parent_ids=["hip"]),
              Genre(id="trap", name="Trap", parent_ids=["hip"]), Genre(id="drill", name="Drill", parent_ids=["hip"])]
    layout = {"liked": ["boom"], "near": ["boom"], "mid": ["boom", "trap"], "far": ["drill"],
              "scope-only": ["hip"]}
    artists = [Artist(id=f"a-{key}", name=key) for key in layout]
    releases = [Release(id=key, title=key, artist_ids=[f"a-{key}"], genre_ids=tags, year=2020)
                for key, tags in layout.items()]
    return Catalog(snapshot_id="blend", is_fixture=True, genres=genres, artists=artists, releases=releases)


def artifact(intercept: float, coefficient: float = 0.0) -> dict:
    return {"schema_version": 1, "kind": "logistic_regression", "taxonomy_version": "1",
            "feature_version": FEATURE_VERSION, "dataset_hash": "h", "trained_genre_ids": ["hip"],
            "validated_genre_ids": [], "is_fixture": False,
            "coefficients": {"similarity": coefficient}, "intercept": intercept}


PROFILE = UserProfile(liked_release_ids_by_genre={"hip": ["liked"]})


def scored(mode="explore", model=None):
    return score_candidates(catalog(), RequestIntent(genre_scope_id="hip", mode=mode), PROFILE, TODAY,
                            artifact=model, _experimental=model is not None)


def test_ranker_version_is_bumped():
    assert RANKER_VERSION.startswith("baseline-v6")


@pytest.mark.parametrize("mode", ["close", "explore", "surprise"])
def test_neutral_model_keeps_baseline_order_and_scores(mode):
    plain = scored(mode)
    blended = scored(mode, artifact(0.0))
    assert [i.release_id for i in blended.items] == [i.release_id for i in plain.items]
    for left, right in zip(plain.items, blended.items):
        assert right.score == pytest.approx(left.score)
        assert right.components["model_probability"] == pytest.approx(.5)
        assert right.components["baseline_score"] == pytest.approx(left.score)


def test_model_is_additive_bounded_correction():
    plain = {i.release_id: i for i in scored("surprise").items}
    for intercept in (4.0, -4.0):
        for item in scored("surprise", artifact(intercept)).items:
            probability = item.components["model_probability"]
            assert item.score == pytest.approx(plain[item.release_id].score
                                               + MODEL_WEIGHT * (probability - .5))
            assert abs(item.score - plain[item.release_id].score) <= MODEL_WEIGHT * .5
            # Baseline components (quality, similarity, penalties) survive the blend.
            assert item.components["similarity"] == plain[item.release_id].components["similarity"]


def test_surprise_keeps_novelty_weight_even_with_confident_model():
    # The model loves similarity, yet surprise must not collapse into "most similar first".
    model = artifact(0.0, coefficient=3.0)
    surprise = {i.release_id: i for i in scored("surprise", model).items}
    close = {i.release_id: i for i in scored("close", model).items}
    assert surprise["far"].score > surprise["near"].score
    assert close["near"].score > close["far"].score


def test_scope_only_releases_are_not_far():
    result = {i.release_id: i for i in scored("surprise").items}
    for rid in ("scope-only",):
        assert result[rid].components["novelty"] is None
        assert "genre_tags_not_specific" in result[rid].uncertainty_flags
    assert result["far"].components["novelty"] == 1.0
    for mode in ("explore", "surprise"):
        pool = {i.release_id for i in exploration_candidates(
            scored(mode), RequestIntent(genre_scope_id="hip", mode=mode))}
        assert "far" in pool
        assert not pool & {"scope-only"}


def test_rank_handles_uninformative_candidates_without_surfacing_them_as_far():
    cat = catalog()
    result = rank(cat, RequestIntent(genre_scope_id="hip", mode="surprise", count=5), PROFILE, TODAY)
    assert result.items
    assert all(i.components["novelty"] is None or i.components["novelty"] > .5
               or i.release_id in {"near", "mid"} for i in result.items)


def test_demo_catalog_has_no_uninformative_far_picks():
    from discovery.catalog import load_catalog

    cat = load_catalog("data/demo/catalog.json")
    scope = next(g.id for g in cat.genres if not g.parent_ids)
    liked = next(r.id for r in cat.releases if set(r.genre_ids) & cat.scope_ids(scope))
    profile = UserProfile(liked_release_ids_by_genre={scope: [liked]})
    result = score_candidates(cat, RequestIntent(genre_scope_id=scope, mode="surprise"), profile, TODAY)
    far = exploration_candidates(result, RequestIntent(genre_scope_id=scope, mode="surprise"))
    allowed = cat.scope_ids(scope)
    release_map = {r.id: r for r in cat.releases}
    assert all((set(release_map[i.release_id].genre_ids) & allowed) - {scope} for i in far)
