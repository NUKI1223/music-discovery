import json
from copy import deepcopy

import pytest

from discovery.contracts import Artist, Catalog, Genre, Release, RequestIntent, UserProfile
from discovery.ranking import rank
from discovery.ranking.baseline import FEATURE_VERSION
from discovery.training.serving import (
    artifact_hash,
    compatibility_reason,
    feature_values,
    load_artifact,
    load_optional_artifact,
    promote_artifact,
)


def model():
    # Artificial contract fixture only: never written as a user's trained artifact.
    return {"schema_version": 1, "kind": "logistic_regression", "taxonomy_version": "1",
            "feature_version": "genre-tags-v1", "dataset_hash": "test-hash",
            "trained_genre_ids": ["hip"], "validated_genre_ids": [], "is_fixture": False,
            "coefficients": {"avoidance": 10}, "intercept": -5}


def evidence(artifact):
    return {"artifact_hash": artifact_hash(artifact), "dataset_hash": artifact["dataset_hash"],
            "is_fixture": False, "protocol": "frozen_temporal_holdout", "decision": "promote",
            "genres": {"hip": {"n": 20, "positive": 10, "negative": 10, "holdout_snapshot_id": "test-holdout",
                               "genre_violations": 0, "model_log_loss": .4, "baseline_log_loss": .7,
                               "model_brier": .15, "baseline_brier": .25}}}


def test_json_loader_and_default_unvalidated_scope(tmp_path):
    artifact = model()
    path = tmp_path / "model.json"
    path.write_text(json.dumps({"artifact": artifact}))
    assert load_artifact(path) == artifact
    assert compatibility_reason(artifact, "hip", "1", "genre-tags-v1") == "genre_not_validated"
    path.write_text('{"intercept": NaN}')
    with pytest.raises(ValueError):
        load_artifact(path)


def test_optional_artifact_failure_keeps_baseline_available(tmp_path):
    assert load_optional_artifact(None) == (None, None)
    assert load_optional_artifact("") == (None, None)
    artifact, warning = load_optional_artifact(tmp_path / "missing.json")
    assert artifact is None and "baseline" in warning
    corrupt = tmp_path / "private-name.json"
    corrupt.write_text("secret-content-that-is-not-json")
    artifact, warning = load_optional_artifact(corrupt)
    assert artifact is None and "baseline" in warning
    assert "private-name" not in warning and "secret-content" not in warning
    valid = model()
    corrupt.write_text(json.dumps(valid))
    assert load_optional_artifact(corrupt) == (valid, None)


def test_promotion_requires_real_matching_nonregressing_evidence():
    artifact = model()
    report = evidence(artifact)
    fixture = dict(artifact, is_fixture=True)
    with pytest.raises(ValueError, match="Synthetic"):
        promote_artifact(fixture, report)
    for mutate in (lambda r: r.update(dataset_hash="other"),
                   lambda r: r["genres"]["hip"].update(n=3),
                   lambda r: r["genres"]["hip"].update(model_log_loss=.8)):
        bad = deepcopy(report)
        mutate(bad)
        with pytest.raises(ValueError):
            promote_artifact(artifact, bad)
    promoted = promote_artifact(artifact, report)
    assert artifact["validated_genre_ids"] == []
    assert promoted["validated_genre_ids"] == ["hip"]
    assert compatibility_reason(promoted, "hip", "1", "genre-tags-v1") is None
    assert compatibility_reason(promoted, "jazz", "1", "genre-tags-v1") == "genre_not_validated"
    assert compatibility_reason(promoted, "hip", "new", "genre-tags-v1") == "taxonomy_version_mismatch"
    assert compatibility_reason(promoted, "hip", "1", "new") == "feature_version_mismatch"
    tampered = deepcopy(promoted)
    tampered["coefficients"]["avoidance"] = 0
    assert compatibility_reason(tampered, "hip", "1", "genre-tags-v1") == "missing_or_stale_promotion_evidence"
    corrupted = deepcopy(promoted)
    corrupted["promotion_evidence"]["genres"] = None
    assert compatibility_reason(corrupted, "hip", "1", "genre-tags-v1") == "invalid_promotion_evidence"


def test_model_affects_scores_before_selector_and_keeps_hard_filters():
    cat = Catalog(snapshot_id="fixture", genres=[Genre(id="hip", name="Hip hop"),
                  Genre(id="tag", name="Some tag", parent_ids=["hip"]), Genre(id="jazz", name="Jazz")],
                  artists=[Artist(id="a", name="A"), Artist(id="b", name="B"), Artist(id="c", name="C")],
                  releases=[Release(id="plain", title="Plain", artist_ids=["a"], genre_ids=["hip"]),
                            Release(id="tagged", title="Tagged", artist_ids=["b"], genre_ids=["tag"]),
                            Release(id="outside", title="Outside", artist_ids=["c"], genre_ids=["jazz"])])
    request = RequestIntent(genre_scope_id="hip", count=1)
    profile = UserProfile(avoid_tags_by_genre={"hip": ["tag"]})
    artifact = model()
    artifact["feature_version"] = FEATURE_VERSION
    baseline = rank(cat, request, profile)
    assert baseline.items[0].release_id == "plain"
    unvalidated = rank(cat, request, profile, artifact=artifact)
    assert unvalidated.items == baseline.items
    assert any("genre_not_validated" in w for w in unvalidated.warnings)
    promoted = promote_artifact(artifact, evidence(artifact))
    learned = rank(cat, request, profile, artifact=promoted)
    # The model is a bounded correction: it cannot overturn a baseline avoidance penalty.
    assert learned.items[0].release_id == "plain"
    top = learned.items[0]
    assert "model_probability" in top.components
    assert top.score < top.components["baseline_score"] == baseline.items[0].score
    assert "outside" not in learned.candidate_ids
    inputs = feature_values(learned.items[0].components)
    assert "model_probability" not in inputs and "baseline_score" not in inputs
    assert "ranking_model_version" not in inputs
    strict = rank(cat, request.model_copy(update={"obscurity": "require"}), profile, artifact=promoted)
    assert strict.items == []


def test_legacy_model_is_not_applied_to_new_artist_frequency_features():
    cat = Catalog(snapshot_id="version-boundary", genres=[Genre(id="hip", name="Hip hop")],
                  artists=[Artist(id="a", name="A")],
                  releases=[Release(id="r", title="R", artist_ids=["a"], genre_ids=["hip"])])
    artifact = model()
    artifact = promote_artifact(artifact, evidence(artifact))
    current = rank(cat, RequestIntent(genre_scope_id="hip"), UserProfile(), artifact=artifact)
    assert any("feature_version_mismatch" in warning for warning in current.warnings)
    assert all("model_probability" not in item.components for item in current.items)
    legacy = rank(cat, RequestIntent(genre_scope_id="hip"), UserProfile(), artifact=artifact,
                  feature_version="genre-tags-v1")
    assert "model_probability" in legacy.items[0].components


def test_promotion_seal_covers_scopes_and_evidence():
    artifact = model()
    promoted = promote_artifact(artifact, evidence(artifact))
    assert compatibility_reason(promoted, "hip", "1", "genre-tags-v1") is None
    # Model identity is stable across promotion; the seal is what covers evidence.
    assert artifact_hash(promoted) == artifact_hash(artifact)
    edits = {
        "evidence_metrics": lambda a: a["promotion_evidence"]["genres"]["hip"].update(model_log_loss=.1),
        "evidence_holdout": lambda a: a["promotion_evidence"]["genres"]["hip"].update(holdout_snapshot_id="x"),
        "seal_removed": lambda a: a.pop("promotion_seal"),
        "scope_added": lambda a: (a["trained_genre_ids"].append("jazz"), a["validated_genre_ids"].append("jazz")),
    }
    for mutate in edits.values():
        forged = deepcopy(promoted)
        mutate(forged)
        assert compatibility_reason(forged, "hip", "1", "genre-tags-v1") is not None
    # Scope list edited without evidence: evidence no longer matches validated scopes.
    widened = deepcopy(promoted)
    widened["trained_genre_ids"].append("jazz")
    widened["validated_genre_ids"].append("jazz")
    assert compatibility_reason(widened, "jazz", "1", "genre-tags-v1") is not None


def test_sealed_promotion_without_seal_falls_back_to_baseline():
    artifact = model()
    promoted = promote_artifact(artifact, evidence(artifact))
    del promoted["promotion_seal"]
    assert compatibility_reason(promoted, "hip", "1", "genre-tags-v1") == "missing_or_stale_promotion_seal"


def test_holdout_overlapping_training_is_rejected():
    artifact = dict(model(), training_release_ids=["r1", "r2"])
    report = evidence(artifact)
    report["genres"]["hip"]["release_ids"] = ["r3"]
    assert promote_artifact(artifact, report)["validated_genre_ids"] == ["hip"]
    report["genres"]["hip"]["release_ids"] = ["r2", "r3"]
    with pytest.raises(ValueError, match="overlaps"):
        promote_artifact(artifact, report)
