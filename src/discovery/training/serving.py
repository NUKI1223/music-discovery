"""Safe JSON inference and explicit, evidence-gated scope promotion."""
import json
from copy import deepcopy
from hashlib import sha256
from math import isfinite
from pathlib import Path

from .pipeline import predict_features

# Only pre-selection input features enter training. Never feed a model's own
# probabilities, rank, version or selector score back as explanatory inputs.
FEATURE_FIELDS = frozenset({
    "similarity", "novelty", "avoidance", "negative_affinity", "novelty_fit",
    "rarity", "quality", "rating_count", "cohort_size", "rarity_eligible",
    "profile_confidence", "mode", "genre_scope", "popularity_source", "cohort_id",
})


PROMOTION_FIELDS = frozenset({"validated_genre_ids", "promotion_evidence", "promotion_seal"})


def feature_values(components: dict) -> dict[str, float | str]:
    return {key: value for key, value in components.items()
            if key in FEATURE_FIELDS and value is not None and isinstance(value, (int, float, str))}


def validate_artifact(artifact: dict) -> None:
    if not isinstance(artifact, dict) or artifact.get("schema_version") != 1 or artifact.get("kind") != "logistic_regression":
        raise ValueError("Unsupported model artifact")
    for field in ("taxonomy_version", "feature_version", "dataset_hash"):
        if not isinstance(artifact.get(field), str) or not artifact[field]:
            raise ValueError(f"Missing artifact {field}")
    for field in ("trained_genre_ids", "validated_genre_ids"):
        values = artifact.get(field)
        if not isinstance(values, list) or any(not isinstance(v, str) or not v for v in values) or len(set(values)) != len(values):
            raise ValueError(f"Invalid artifact {field}")
    if not set(artifact["validated_genre_ids"]) <= set(artifact["trained_genre_ids"]):
        raise ValueError("Validated scopes must have training data")
    if type(artifact.get("is_fixture")) is not bool:
        raise ValueError("Fixture provenance is required")
    coefficients = artifact.get("coefficients")
    if not isinstance(coefficients, dict) or not coefficients:
        raise ValueError("Model coefficients are required")
    if any(not isinstance(k, str) or not isinstance(v, (int, float)) or not isfinite(v)
           for k, v in coefficients.items()):
        raise ValueError("Model coefficients must be finite numbers")
    intercept = artifact.get("intercept")
    if not isinstance(intercept, (int, float)) or not isfinite(intercept):
        raise ValueError("Model intercept must be finite")


def artifact_hash(artifact: dict) -> str:
    validate_artifact(artifact)
    immutable = {k: v for k, v in artifact.items() if k not in PROMOTION_FIELDS}
    return sha256(json.dumps(immutable, sort_keys=True, allow_nan=False).encode()).hexdigest()


def promotion_seal(artifact: dict) -> str:
    """Hash binding the model identity to its validated scopes and evidence.

    artifact_hash stays the model identity (evidence refers to it, so it cannot
    cover the evidence itself). The seal covers both, so editing scopes or
    evidence after promotion is detected. It is integrity, not authenticity: a
    party who can rewrite the whole file can also recompute it.
    """
    payload = {"artifact_hash": artifact_hash(artifact),
               "validated_genre_ids": sorted(artifact["validated_genre_ids"]),
               "promotion_evidence": artifact.get("promotion_evidence")}
    return sha256(json.dumps(payload, sort_keys=True, allow_nan=False).encode()).hexdigest()


def load_artifact(path: str | Path) -> dict:
    file = Path(path)
    if file.stat().st_size > 10_000_000:
        raise ValueError("Model JSON exceeds size limit")
    def invalid_constant(value):
        raise ValueError(f"Invalid JSON number: {value}")
    payload = json.loads(file.read_text(), parse_constant=invalid_constant)
    # The training CLI writes a report bundle; standalone artifacts work too.
    artifact = payload.get("artifact") if isinstance(payload, dict) and "artifact" in payload else payload
    validate_artifact(artifact)
    artifact_hash(artifact)  # Reject non-finite numbers even in optional metadata.
    return artifact


def load_optional_artifact(path: str | Path | None) -> tuple[dict | None, str | None]:
    """Load an optional model without disabling the baseline on filesystem/JSON errors.

    Warnings deliberately omit file contents, paths and raw exception messages.
    Compatibility and promotion checks still happen against the request in rank().
    """
    if path is None or path == "":
        return None, None
    try:
        return load_artifact(path), None
    except (OSError, ValueError, TypeError) as exc:
        return None, f"Personal model unavailable ({type(exc).__name__}); using baseline."


def compatibility_reason(artifact: dict, genre_scope_id: str,
                         taxonomy_version: str, feature_version: str) -> str | None:
    try:
        validate_artifact(artifact)
    except (ValueError, TypeError) as exc:
        return f"invalid_artifact:{exc}"
    if artifact["is_fixture"]:
        return "fixture_artifact"
    if artifact["taxonomy_version"] != taxonomy_version:
        return "taxonomy_version_mismatch"
    if artifact["feature_version"] != feature_version:
        return "feature_version_mismatch"
    if genre_scope_id not in artifact["validated_genre_ids"]:
        return "genre_not_validated"
    promotion = artifact.get("promotion_evidence", {})
    if not isinstance(promotion, dict):
        return "invalid_promotion_evidence"
    if promotion.get("artifact_hash") != artifact_hash(artifact):
        return "missing_or_stale_promotion_evidence"
    if not isinstance(promotion.get("genres"), dict):
        return "invalid_promotion_evidence"
    if genre_scope_id not in promotion["genres"]:
        return "missing_genre_promotion_evidence"
    try:
        if sorted(artifact["validated_genre_ids"]) != sorted(promotion["genres"]):
            return "validated_scopes_differ_from_evidence"
        if artifact.get("promotion_seal") != promotion_seal(artifact):
            return "missing_or_stale_promotion_seal"
        promote_artifact(artifact, promotion)
    except (ValueError, TypeError, AttributeError):
        return "invalid_promotion_evidence"
    return None


def predict_components(artifact: dict, genre_scope_id: str, components: dict, *,
                       allow_experimental: bool = False) -> float:
    probability = predict_features(artifact, genre_scope_id, feature_values(components),
                                   allow_experimental=allow_experimental)
    if probability is None or not isfinite(probability):
        raise ValueError("Model cannot score the requested genre")
    return probability


def promote_artifact(artifact: dict, evidence: dict, *, minimum_holdout: int = 20,
                     minimum_class: int = 5, previous_artifact: dict | None = None) -> dict:
    """Return a new artifact; never writes files or auto-promotes during train.

    The caller supplies a frozen holdout report computed separately from training.
    This gate supports offline promotion, not a claim of real-user causal benefit.
    """
    validate_artifact(artifact)
    if not isinstance(evidence, dict):
        raise ValueError("Promotion evidence must be an object")
    if minimum_holdout < 20 or minimum_class < 5:
        raise ValueError("Promotion requires at least 20 held-out labels and five of each class")
    if artifact["is_fixture"] or evidence.get("is_fixture") is not False:
        raise ValueError("Synthetic evidence cannot promote personal models")
    if (evidence.get("protocol") != "frozen_temporal_holdout" or evidence.get("decision") != "promote"
        or evidence.get("artifact_hash") != artifact_hash(artifact)
        or evidence.get("dataset_hash") != artifact["dataset_hash"]):
        raise ValueError("Evidence must identify this artifact, dataset and frozen evaluation protocol")
    genres = evidence.get("genres", {})
    if not isinstance(genres, dict) or not genres or not set(genres) <= set(artifact["trained_genre_ids"]):
        raise ValueError("Evidence scopes must be nonempty and trained")
    previous = previous_artifact or artifact
    if previous_artifact is not None:
        validate_artifact(previous_artifact)
    if not set(previous["validated_genre_ids"]) <= set(genres):
        raise ValueError("Promotion must evaluate all previously validated genres")
    for genre, metrics in genres.items():
        if not isinstance(metrics, dict) or any(type(metrics.get(key)) is not int
                                                for key in ("n", "positive", "negative")):
            raise ValueError(f"Invalid holdout counts for {genre}")
        if (not metrics.get("holdout_snapshot_id") or metrics.get("n", 0) < minimum_holdout
            or metrics.get("positive", 0) < minimum_class or metrics.get("negative", 0) < minimum_class
            or metrics.get("positive", 0) + metrics.get("negative", 0) != metrics.get("n")
            or metrics.get("genre_violations") != 0):
            raise ValueError(f"Insufficient or invalid holdout evidence for {genre}")
        held_out = metrics.get("release_ids")
        if held_out is not None and (not isinstance(held_out, list)
                                     or set(held_out) & set(artifact.get("training_release_ids", []))):
            raise ValueError(f"Holdout overlaps training data for {genre}")
        for name in ("model_log_loss", "baseline_log_loss", "model_brier", "baseline_brier"):
            value = metrics.get(name)
            if not isinstance(value, (int, float)) or not isfinite(value) or value < 0:
                raise ValueError(f"Invalid metric {name} for {genre}")
        if (metrics["model_log_loss"] >= metrics["baseline_log_loss"]
            or metrics["model_brier"] > metrics["baseline_brier"]):
            raise ValueError(f"No primary improvement or regression in {genre}")
    promoted = deepcopy(artifact)
    promoted["validated_genre_ids"] = sorted(genres)
    promoted["promotion_evidence"] = deepcopy(evidence)
    promoted["promotion_seal"] = promotion_seal(promoted)
    return promoted
