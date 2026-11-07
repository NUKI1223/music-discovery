"""Explicit optional structure inference and import of caller-supplied separated audio."""

import copy
import hashlib
import importlib
import json
import math
import re
from bisect import bisect_left
from dataclasses import asdict, is_dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from tempfile import TemporaryDirectory

from .measure import AudioAnalysisError, _analyze_wav, _identifier

STRUCTURE_VERSION = "allin1-structure-v1"
STEMS_VERSION = "demucs-stems-import-v1"
LABELS = {"start", "end", "intro", "outro", "break", "bridge", "inst", "solo", "verse", "chorus"}
TIME_TOLERANCE = .05


def _number(value, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise AudioAnalysisError("INVALID_STRUCTURE", f"{label} must be a finite number.")
    return float(value)


def _metadata(model: str, model_version: str, config: dict) -> dict:
    _identifier(model, "model")
    _identifier(model_version, "model_version")
    if not isinstance(config, dict):
        raise AudioAnalysisError("INVALID_PROVENANCE", "An explicit model configuration object is required.")
    try:
        encoded = json.dumps(config, allow_nan=False)
        if len(encoded) > 16000:
            raise ValueError("too large")
        config = json.loads(encoded)
    except (ValueError, TypeError) as error:
        raise AudioAnalysisError("INVALID_PROVENANCE", "Model config must be bounded finite JSON.") from error
    return {"model": model, "model_version": model_version, "config": config}


def _bind(analysis: dict, source_sha256: str) -> float:
    if not re.fullmatch(r"[0-9a-f]{64}", source_sha256 or "") or source_sha256 != analysis.get("sha256"):
        raise AudioAnalysisError("SOURCE_MISMATCH", "External output must identify the analyzed source SHA-256.")
    duration = _number(analysis.get("duration_seconds"), "duration_seconds")
    if duration <= 0:
        raise AudioAnalysisError("INVALID_STRUCTURE", "The recording duration must be positive.")
    return duration


def _timestamps(values, duration: float, name: str) -> list[float]:
    if not isinstance(values, list) or len(values) > math.ceil(duration * 20) + 1:
        raise AudioAnalysisError("INVALID_STRUCTURE", f"{name} must be a bounded list of timestamps.")
    result = [_number(value, name) for value in values]
    if any(value < 0 or value > duration + TIME_TOLERANCE for value in result):
        raise AudioAnalysisError("INVALID_STRUCTURE", f"{name} extends outside the recording.")
    if any(right <= left for left, right in zip(result, result[1:])):
        raise AudioAnalysisError("INVALID_STRUCTURE", f"{name} must be strictly increasing.")
    return result


def import_allin1_result(result, analysis: dict, *, source_sha256: str,
                         model: str, model_version: str, config: dict) -> dict:
    """Validate official allin1 AnalysisResult/JSON without importing or running allin1."""
    duration = _bind(analysis, source_sha256)
    provenance = _metadata(model, model_version, config)
    if is_dataclass(result):
        # Do not copy embeddings/activations: they are outside this adapter's contract.
        result = {name: getattr(result, name, None) for name in (
            "bpm", "beats", "downbeats", "beat_positions", "segments")}
    if not isinstance(result, dict):
        raise AudioAnalysisError("INVALID_STRUCTURE", "Expected one allin1 AnalysisResult or JSON object.")
    beats = _timestamps(result.get("beats"), duration, "beats")
    downbeats = _timestamps(result.get("downbeats"), duration, "downbeats")
    for value in downbeats:
        index = bisect_left(beats, value)
        neighbors = beats[max(0, index - 1):index + 1]
        if not any(abs(value - beat) <= TIME_TOLERANCE for beat in neighbors):
            raise AudioAnalysisError("INVALID_STRUCTURE", "Every downbeat must correspond to a beat.")
    positions = result.get("beat_positions")
    if not isinstance(positions, list) or len(positions) != len(beats) or any(
        isinstance(position, bool) or not isinstance(position, int) or not 1 <= position <= 32
        for position in positions
    ):
        raise AudioAnalysisError("INVALID_STRUCTURE", "Beat positions must match beats and be integers 1–32.")
    bpm = None if result.get("bpm") is None else _number(result["bpm"], "bpm")
    if bpm is not None and not 1 <= bpm <= 1000:
        raise AudioAnalysisError("INVALID_STRUCTURE", "Predicted BPM is outside the supported range.")
    segments = result.get("segments")
    if not isinstance(segments, list) or len(segments) > math.ceil(duration * 10) + 1:
        raise AudioAnalysisError("INVALID_STRUCTURE", "Expected a bounded segment list.")
    checked = []
    for segment in segments:
        segment = asdict(segment) if is_dataclass(segment) else segment
        if not isinstance(segment, dict) or set(segment) != {"start", "end", "label"}:
            raise AudioAnalysisError("INVALID_STRUCTURE", "Each segment needs start, end, and label only.")
        start, end = _number(segment["start"], "segment.start"), _number(segment["end"], "segment.end")
        label = segment["label"]
        if not isinstance(label, str) or label not in LABELS:
            raise AudioAnalysisError("INVALID_STRUCTURE", "Unknown functional segment label.")
        if start < 0 or end <= start or end > duration + TIME_TOLERANCE:
            raise AudioAnalysisError("INVALID_STRUCTURE", "Invalid segment interval.")
        if checked and start < checked[-1]["end"] - 1e-6:
            raise AudioAnalysisError("INVALID_STRUCTURE", "Segments overlap or are out of order.")
        checked.append({"start": start, "end": end, "label": label})
    normalized = {"bpm": bpm, "beats": beats, "downbeats": downbeats,
                  "beat_positions": positions, "segments": checked}
    output = copy.deepcopy(analysis)
    output["structure"] = {
        "schema_version": STRUCTURE_VERSION, **normalized,
        "source_sha256": source_sha256, "evidence_kind": "model_prediction_not_verified_annotation",
        "provenance": {"adapter": STRUCTURE_VERSION, "package": "allin1", **provenance,
                       "normalized_result_sha256": hashlib.sha256(
                           json.dumps(normalized, sort_keys=True, allow_nan=False).encode()).hexdigest()},
        "validation": {"timestamp_tolerance_seconds": TIME_TOLERANCE,
                       "timestamps_preserved_without_clamping": True},
        "unknowns": [name for name, value in (("tempo", bpm), ("beats", beats), ("sections", checked)) if not value]
                    + ["prediction_accuracy_on_this_recording"],
    }
    # Model predictions don't make any perceptual attribute known or create training labels.
    return output


def analyze_allin1(path: str | Path, analysis: dict, *, allow_model_execution: bool = False,
                   model: str = "harmonix-all", device: str = "cpu") -> dict:
    """Explicit opt-in: allin1 may fetch weights and runs its own Demucs separation."""
    if not allow_model_execution:
        raise AudioAnalysisError("MODEL_EXECUTION_DISABLED", "Explicitly enable optional allin1 model execution.")
    if not re.fullmatch(r"harmonix-(all|fold[0-7])", model):
        raise AudioAnalysisError("INVALID_MODEL", "Choose harmonix-all or harmonix-fold0 through harmonix-fold7.")
    if device not in {"cpu", "cuda", "mps"}:
        raise AudioAnalysisError("INVALID_DEVICE", "Choose cpu, cuda, or mps.")
    path = Path(path).expanduser().resolve()
    with path.open("rb") as source:
        source_sha256 = hashlib.file_digest(source, "sha256").hexdigest()
    _bind(analysis, source_sha256)
    try:
        package_version = version("allin1")
        allin1 = importlib.import_module("allin1")
    except (ImportError, PackageNotFoundError) as error:
        raise AudioAnalysisError("OPTIONAL_DEPENDENCY_MISSING", "Install allin1 in an isolated audio environment first.") from error
    config = {"device": device, "include_activations": False, "include_embeddings": False,
              "multiprocess": False, "keep_byproducts": False, "visualize": False, "sonify": False}
    with TemporaryDirectory(prefix="discovery-allin1-") as work:
        result = allin1.analyze(str(path), model=model, out_dir=None,
                               demix_dir=str(Path(work) / "demix"), spec_dir=str(Path(work) / "spec"),
                               **config)
    with path.open("rb") as source:
        if hashlib.file_digest(source, "sha256").hexdigest() != source_sha256:
            raise AudioAnalysisError("AUDIO_CHANGED", "The source changed while the model was running.")
    output = import_allin1_result(result, analysis, source_sha256=source_sha256, model=model,
                                  model_version="unreported_by_package", config=config)
    output["structure"]["provenance"]["package_version"] = package_version
    output["structure"]["unknowns"].append("model_checkpoint_hash")
    return output


def import_demucs_stems(analysis: dict, *, vocals_path: str | Path,
                         instrumental_path: str | Path, source_sha256: str,
                         model: str, model_version: str, config: dict) -> dict:
    """Measure explicit Demucs two-stem WAV paths; never launch source separation."""
    duration = _bind(analysis, source_sha256)
    metadata = _metadata(model, model_version, config)
    if Path(vocals_path).resolve() == Path(instrumental_path).resolve():
        raise AudioAnalysisError("INVALID_STEMS", "Supply two distinct stem files explicitly.")
    output = copy.deepcopy(analysis)
    stems = {}
    for role, path in (("vocals", vocals_path), ("instrumental", instrumental_path)):
        stem = _analyze_wav(path, analysis["track_id"], analysis["release_id"],
                            is_fixture=analysis["is_fixture"], allow_silent=True)
        if abs(stem["duration_seconds"] - duration) > TIME_TOLERANCE:
            raise AudioAnalysisError("STEM_DURATION_MISMATCH", "Stem and source durations differ by more than 50 ms.")
        stem["provenance"]["separation"] = {
            "adapter": STEMS_VERSION, "package": "demucs", **metadata,
            "source_sha256": source_sha256, "stem_role": role,
            "role_basis": "caller_supplied_demucs_output_not_voice_detection",
            "source_binding": "caller_declared_hash_not_independently_verified_separation",
        }
        stem["unknowns"].extend(["stem_leakage", "separation_accuracy", "relative_stem_gain"])
        stems[role] = stem
    output["stems"] = stems
    return output
