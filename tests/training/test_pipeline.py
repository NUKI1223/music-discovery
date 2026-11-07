from datetime import datetime, timedelta, timezone

import pytest

from discovery.training import TrainingRecord, predict, train


def rows(n=100, genre="hip"):
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    return [TrainingRecord(f"{genre}{i}", genre, (start+timedelta(days=i)).isoformat(),
                           (start+timedelta(days=i, hours=1)).isoformat(), i % 2,
                           {"affinity": float(i % 2), "mode": "explore"}, f"snap-{i}", True)
            for i in range(n)]


def test_empty_dataset_is_not_fake_personalization():
    report = train([], cutoff="2026-01-01T00:00:00Z")
    assert report.status == "insufficient_data"
    assert report.artifact is None


def test_reproducible_safe_artifact_and_unvalidated_scope_fallback():
    records = rows()
    one = train(records, cutoff="2026-01-01T00:00:00Z")
    two = train(records, cutoff="2026-01-01T00:00:00Z")
    assert one.to_dict() == two.to_dict()
    assert one.status == "fixture_only"
    assert one.artifact["validated_genre_ids"] == []
    assert predict(one.artifact, records[-1]) is None
    assert predict(one.artifact, records[-1], allow_experimental=True) > .5
    assert one.per_genre["hip"]["test"]["model"]["brier"] < one.per_genre["hip"]["test"]["constant_baseline"]["brier"]


def test_cross_genre_duplicate_release_rejected_before_split():
    data = rows()
    data.append(TrainingRecord("hip0", "jazz", data[0].decision_at, data[0].available_at, 1, {}, "x", True))
    with pytest.raises(ValueError, match="Canonical release"):
        train(data, cutoff="2026-01-01T00:00:00Z")


def test_late_label_and_future_category_never_enter_training():
    data = rows()
    original = data[2]
    data[2] = TrainingRecord(original.release_id, original.genre_scope_id, original.decision_at,
                             "2025-12-01T00:00:00Z", 0, {"late_only": 1}, "late", True)
    data[-1] = TrainingRecord(data[-1].release_id, "hip", data[-1].decision_at,
                             data[-1].available_at, 1, {"future_only": 1}, "future", True)
    report = train(data, cutoff="2026-01-01T00:00:00Z")
    assert "late_only" not in report.artifact["coefficients"]
    assert "future_only" not in report.artifact["coefficients"]


def test_shared_model_second_genre_and_new_genre_fallback():
    report = train(rows()+rows(100, "jazz"), cutoff="2026-01-01T00:00:00Z")
    assert report.artifact["trained_genre_ids"] == ["hip", "jazz"]
    assert set(report.per_genre) == {"hip", "jazz"}
    assert predict(report.artifact, rows(1, "metal")[0], allow_experimental=True) is None


def test_missing_feedback_is_not_a_label():
    with pytest.raises(ValueError, match="explicit"):
        TrainingRecord("r", "hip", "2025-01-01", "2025-01-02", None, {}, "snap")


@pytest.mark.parametrize("field,value", [("taxonomy_version", "different-taxonomy"),
                                         ("feature_version", "different-features")])
def test_record_version_mismatch_is_not_silently_trained_or_dropped(field, value):
    from dataclasses import replace

    data = rows()
    data[-1] = replace(data[-1], **{field: value})
    with pytest.raises(ValueError, match="version mismatch"):
        train(data, cutoff="2026-01-01T00:00:00Z")
