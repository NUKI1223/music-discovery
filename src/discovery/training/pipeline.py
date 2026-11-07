"""Temporal training with plain JSON artifacts and explicit genre readiness.

Inputs must contain features frozen at decision time. This module deliberately
does not reconstruct historical features using today's user profile/catalog.
"""

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from math import exp, isfinite, log
from typing import Any


def timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


@dataclass(frozen=True)
class TrainingRecord:
    release_id: str
    genre_scope_id: str
    decision_at: str
    available_at: str
    label: int
    features: dict[str, float | str]
    feature_snapshot_id: str
    is_fixture: bool = False
    taxonomy_version: str = "1"
    feature_version: str = "genre-tags-v1"

    def __post_init__(self) -> None:
        if self.label not in (0, 1):
            raise ValueError("Only explicit listened like/dislike labels are allowed")
        if timestamp(self.available_at) < timestamp(self.decision_at):
            raise ValueError("Label cannot be available before its decision")
        if not self.feature_snapshot_id or not self.genre_scope_id:
            raise ValueError("Feature snapshot and genre scope are required")
        if not self.taxonomy_version or not self.feature_version:
            raise ValueError("Taxonomy and feature versions are required")
        if any(isinstance(v, (int, float)) and not isfinite(v) for v in self.features.values()):
            raise ValueError("Feature values must be finite")


@dataclass
class TrainingReport:
    status: str
    reasons: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    per_genre: dict[str, Any] = field(default_factory=dict)
    artifact: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _features(record: TrainingRecord) -> dict[str, float | str]:
    return _context_features(record.genre_scope_id, record.features)


def _context_features(genre_scope_id: str, features: dict[str, float | str]) -> dict[str, float | str]:
    # Genre conditioning permits a shared model without assuming cross-genre taste.
    values: dict[str, float | str] = dict(features)
    values["genre_scope"] = genre_scope_id
    for name, value in features.items():
        values[f"genre:{genre_scope_id}:{name}"] = value
    return values


def _encode(values: dict[str, float | str]) -> dict[str, float]:
    return {f"{name}={value}" if isinstance(value, str) else name:
            1.0 if isinstance(value, str) else float(value)
            for name, value in values.items()}


def predict(artifact: dict[str, Any], record: TrainingRecord, *, allow_experimental: bool = False) -> float | None:
    """Return None when serving must use the baseline. Never auto-promote scopes."""
    return predict_features(artifact, record.genre_scope_id, record.features,
                            allow_experimental=allow_experimental)


def predict_features(artifact: dict[str, Any], genre_scope_id: str,
                     features: dict[str, float | str], *, allow_experimental: bool = False) -> float | None:
    permitted = artifact["trained_genre_ids"] if allow_experimental else artifact["validated_genre_ids"]
    if genre_scope_id not in permitted:
        return None
    values = _encode(_context_features(genre_scope_id, features))
    logit = float(artifact["intercept"])
    for name, weight in artifact["coefficients"].items():
        logit += weight * values.get(name, 0.0)
    return 1 / (1 + exp(-logit)) if logit >= 0 else exp(logit) / (1 + exp(logit))


def _metrics(labels: list[int], probabilities: list[float]) -> dict[str, float | int]:
    if not labels:
        return {"n": 0}
    bounded = [min(1-1e-12, max(1e-12, p)) for p in probabilities]
    return {
        "n": len(labels),
        "log_loss": -sum(y * log(p) + (1-y) * log(1-p) for y, p in zip(labels, bounded)) / len(labels),
        "brier": sum((y-p)**2 for y, p in zip(labels, bounded)) / len(labels),
    }


def train(records: list[TrainingRecord], *, cutoff: str, minimum_labels: int = 60,
          minimum_minority: int = 15, seed: int = 17,
          taxonomy_version: str = "1", feature_version: str = "genre-tags-v1") -> TrainingReport:
    """Train on the first 60%, validate on next 20%, test on final 20%.

    Delayed labels must be available by the end of their historical partition.
    A canonical release may occur once only; callers resolve append-only feedback
    corrections as of cutoff before constructing records. Synthetic runs are
    always fixture-only, never personal evidence.
    """
    if minimum_labels < 5 or minimum_minority < 1:
        raise ValueError("Training gates must remain meaningful")
    mismatches = [r.release_id for r in records
                  if r.taxonomy_version != taxonomy_version or r.feature_version != feature_version]
    if mismatches:
        raise ValueError(f"Taxonomy/feature version mismatch in {len(mismatches)} records; "
                         "migrate features explicitly or select a matching dataset/config")
    available = sorted((r for r in records if timestamp(r.available_at) <= timestamp(cutoff)),
                       key=lambda r: (timestamp(r.decision_at), r.release_id))
    if len({r.release_id for r in available}) != len(available):
        raise ValueError("Canonical release repeated: resolve feedback; do not leak across genre folds")
    if any(r.is_fixture for r in available) and not all(r.is_fixture for r in available):
        raise ValueError("Fixture and real labels cannot be mixed")
    counts = {"available": len(available), "unavailable": len(records)-len(available)}
    genre_counts: dict[str, dict[str, int]] = {}
    for r in available:
        genre_counts.setdefault(r.genre_scope_id, {"n": 0, "positive": 0, "negative": 0})
        genre_counts[r.genre_scope_id]["n"] += 1
        genre_counts[r.genre_scope_id]["positive" if r.label else "negative"] += 1
    eligible_genres = [g for g, c in genre_counts.items()
                       if c["n"] >= minimum_labels and min(c["positive"], c["negative"]) >= minimum_minority]
    if not eligible_genres:
        return TrainingReport("insufficient_data", ["No genre meets explicit-label and class-coverage gates"], counts, genre_counts)
    # Globally frozen time boundaries keep crossover releases and all genre slices aligned.
    n = len(available)
    train_end, valid_end = max(1, int(n*.6)), max(2, int(n*.8))
    train_cutoff = timestamp(available[train_end].decision_at)
    valid_cutoff = timestamp(available[min(valid_end, n-1)].decision_at)
    train_rows = [r for r in available[:train_end] if timestamp(r.available_at) <= train_cutoff]
    valid_rows = [r for r in available[train_end:valid_end] if timestamp(r.available_at) <= valid_cutoff]
    test_rows = available[valid_end:]
    train_rows = [r for r in train_rows if r.genre_scope_id in eligible_genres]
    counts.update(train=len(train_rows), validation=len(valid_rows), test=len(test_rows))
    if len({r.label for r in train_rows}) < 2:
        return TrainingReport("insufficient_data", ["Temporal train partition lacks both classes"], counts, genre_counts)
    import sklearn
    from sklearn.feature_extraction import DictVectorizer
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler

    encoder = DictVectorizer(sparse=True)
    x = encoder.fit_transform([_features(r) for r in train_rows])
    scaler = StandardScaler(with_mean=False)
    scaled_x = scaler.fit_transform(x)
    # Balance genre contribution; retaining all train history avoids newest-genre-only updates.
    group_n = {g: sum(r.genre_scope_id == g for r in train_rows) for g in eligible_genres}
    weights = [len(train_rows) / (len(eligible_genres)*group_n[r.genre_scope_id]) for r in train_rows]
    model = LogisticRegression(C=1.0, max_iter=1000, random_state=seed)
    model.fit(scaled_x, [r.label for r in train_rows], sample_weight=weights)
    artifact = {
        "schema_version": 1, "kind": "logistic_regression", "seed": seed,
        "trained_genre_ids": sorted({r.genre_scope_id for r in train_rows}),
        "validated_genre_ids": [], "taxonomy_version": taxonomy_version,
        "feature_version": feature_version, "cutoff": cutoff,
        "dataset_hash": sha256(json.dumps([asdict(r) for r in available], sort_keys=True).encode()).hexdigest(),
        "implementation_version": "logistic-v1", "sklearn_version": sklearn.__version__,
        "training_config": {"minimum_labels": minimum_labels, "minimum_minority": minimum_minority,
                            "C": 1.0, "max_iter": 1000, "genre_balancing": "inverse_frequency"},
        "training_release_ids": [r.release_id for r in train_rows],
        "training_counts_by_genre": {g: {"n": sum(r.genre_scope_id == g for r in train_rows),
                                          "positive": sum(r.label for r in train_rows if r.genre_scope_id == g)}
                                     for g in sorted({r.genre_scope_id for r in train_rows})},
        "feature_snapshot_ids": sorted({r.feature_snapshot_id for r in train_rows}),
        "intercept": float(model.intercept_[0]),
        # Fold the learned scale into weights so safe JSON inference uses raw inputs.
        "coefficients": dict(zip(encoder.get_feature_names_out().tolist(), (model.coef_[0]/scaler.scale_).tolist())),
        "training_scales": dict(zip(encoder.get_feature_names_out().tolist(), scaler.scale_.tolist())),
        "is_fixture": all(r.is_fixture for r in available),
    }
    reports: dict[str, Any] = {}
    for genre in genre_counts:
        genre_train = [r for r in train_rows if r.genre_scope_id == genre]
        baseline = sum(r.label for r in genre_train) / len(genre_train) if genre_train else .5
        report: dict[str, Any] = {"counts": genre_counts[genre], "serving": "baseline"}
        for partition, rows in (("validation", valid_rows), ("test", test_rows)):
            selected = [r for r in rows if r.genre_scope_id == genre]
            probabilities = [predict(artifact, r, allow_experimental=True) for r in selected]
            report[partition] = {
                "model": _metrics([r.label for r in selected], probabilities) if all(p is not None for p in probabilities) else {"n": 0, "reason": "untrained_genre"},
                "constant_baseline": _metrics([r.label for r in selected], [baseline]*len(selected)),
            }
        reports[genre] = report
    return TrainingReport("fixture_only" if artifact["is_fixture"] else "experimental",
                          ["Promotion requires frozen per-genre baseline comparisons and prospective evidence"],
                          counts, reports, artifact)
