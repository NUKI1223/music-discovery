from collections import Counter

import pytest

from discovery.contracts import Artist, Catalog, Genre, Release, RequestIntent, UserProfile
from discovery.experiments.bandit import (
    LinearRewardModel,
    build_slate,
    distribution,
    evaluate_ope,
    sample,
)
from discovery.ranking import rank


def test_probabilities_and_empirical_sampling():
    policy = distribution({"a": 1, "b": 2, "c": 2}, epsilon=.3)
    assert policy == pytest.approx({"a": .1, "b": .8, "c": .1})
    counts = Counter(sample(policy, seed) for seed in range(10000))
    assert counts["b"]/10000 == pytest.approx(.8, abs=.02)
    assert sample(policy, 19) == sample(policy, 19)
    assert distribution({"a": 2, "b": 1}, 0) == {"a": 1, "b": 0}


def test_bad_probability_input_rejected():
    with pytest.raises(ValueError):
        distribution({}, .1)
    with pytest.raises(ValueError):
        distribution({"a": 1}, -1)
    with pytest.raises(ValueError):
        sample({"a": 2}, 1)


def logged(reward, probability=.5):
    return {"policy_details": {"enabled": True, "context_id": "ctx", "probabilities": {"a": probability, "b": 1-probability},
                               "selected_release_id": "a", "chosen_probability": probability}, "reward": reward}


def test_ope_missing_feedback_and_support_are_explicit():
    observations = [logged(1), logged(0), logged(None)]
    report = evaluate_ope(observations, {"ctx": {"a": .5, "b": .5}}, minimum_ess=2)
    assert report["ips"] == .5
    assert report["snips"] == .5
    assert report["effective_sample_size"] == 2
    assert report["missing"] == 1
    assert report["promotion_allowed"] is False
    unsupported = evaluate_ope([logged(1, 1)], {"ctx": {"a": .5, "b": .5}})
    assert unsupported["status"] == "unsupported"
    changed_context = evaluate_ope(observations, {"different-prefix": {"a": .5, "b": .5}})
    assert changed_context["status"] == "unsupported"


def test_pending_and_duplicate_feedback_never_double_updates():
    model = LinearRewardModel()
    features = {"genre:hip:affinity": .8}
    initial = model.predict(features)
    assert not model.observe("pending", features, None)
    assert model.predict(features) == initial
    assert model.observe("event-1", features, 1)
    updated = model.predict(features)
    assert updated > initial
    assert not model.observe("event-1", features, 1)
    assert model.predict(features) == updated


def test_one_slot_sampling_preserves_actual_prefix_and_scope():
    genres = [Genre(id="hip", name="Hip hop"), Genre(id="exp", name="Experimental", parent_ids=["hip"]),
              Genre(id="boom", name="Boom bap", parent_ids=["hip"])]
    artists = [Artist(id=f"a{i}", name=f"Artist {i}", genre_ids=["exp" if i < 5 else "boom"]) for i in range(12)]
    releases = [Release(id=f"r{i}", title=f"Release {i}", artist_ids=[f"a{i}"],
                        genre_ids=["exp" if i < 5 else "boom"]) for i in range(12)]
    catalog = Catalog(snapshot_id="bandit-fixture", is_fixture=True, genres=genres, artists=artists, releases=releases)
    profile = UserProfile(seed_artist_ids_by_genre={"hip": ["a0"]})
    request = RequestIntent(genre_scope_id="hip", mode="explore")
    base = rank(catalog, request, profile)
    result, details = build_slate(catalog, request, profile, seed=19, epsilon=.2)
    assert details["enabled"]
    assert result.items[:4] == base.items[:4]
    assert result.items[4].release_id == details["selected_release_id"]
    assert details["chosen_probability"] == details["probabilities"][result.items[4].release_id]
    assert not set(details["candidate_ids"]) & set(details["prefix_release_ids"])
    assert details["context"]["genre_scope_id"] == "hip"
    assert (result, details) == build_slate(catalog, request, profile, seed=19, epsilon=.2)
    _, disabled = build_slate(catalog, request, UserProfile(), seed=1)
    assert not disabled["enabled"]
