"""Generated WAVs test measurements only; no audio or perceptual labels are fabricated as evidence."""

import builtins
import hashlib
import json
import math
import struct
import subprocess
import sys
import wave
from dataclasses import dataclass
from types import SimpleNamespace

import numpy as np
import pytest

from discovery.audio import AudioAnalysisError, analyze_wav
from discovery.audio.__main__ import main
from discovery.audio.adapters import analyze_allin1, import_allin1_result, import_demucs_stems


def write_wave(path, *, frequency=440, seconds=2, rate=16000, width=2, antiphase=False, silent=False):
    time = np.arange(round(seconds * rate)) / rate
    values = np.zeros(len(time)) if silent else .5 * np.sin(2 * np.pi * frequency * time)
    samples = np.stack([values, -values], axis=1) if antiphase else values[:, None]
    if width == 1:
        raw = np.clip(np.round(samples * 128 + 128), 0, 255).astype(np.uint8).tobytes()
    elif width == 3:
        integers = np.round(samples * 8388608).astype(np.int32).reshape(-1)
        raw = np.stack([integers & 255, (integers >> 8) & 255, (integers >> 16) & 255], axis=1).astype(np.uint8).tobytes()
    else:
        raw = np.round(samples * (2 ** (8 * width - 1))).astype(f"<i{width}").tobytes()
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(samples.shape[1])
        audio.setsampwidth(width)
        audio.setframerate(rate)
        audio.writeframes(raw)
    return path


@pytest.fixture
def source(tmp_path):
    return write_wave(tmp_path / "not-the-track-id.wav")


def measured(path):
    return analyze_wav(path, "explicit-track", "explicit-release", is_fixture=True)


def structure():
    return {"bpm": 120, "beats": [0.1, .6, 1.1, 1.6], "downbeats": [.1],
            "beat_positions": [1, 2, 3, 4], "segments": [
                {"start": 0, "end": 1, "label": "intro"},
                {"start": 1, "end": 2, "label": "verse"}]}


def attach(result, analysis, **kwargs):
    return import_allin1_result(result, analysis, source_sha256=analysis["sha256"],
                               model="harmonix-all", model_version="fixture-revision", config={}, **kwargs)


def test_explicit_identity_hash_and_no_perceptual_labels(source):
    result = measured(source)
    assert result["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert result["track_id"] == "explicit-track" and result["release_id"] == "explicit-release"
    assert result["is_fixture"] is True and result["feature_version"] == "pcm-window-v1"
    assert result["sample_rate"] == 16000 and result["duration_seconds"] == 2
    assert len(result["windows"]) == 4
    assert result["structure"] is None and result["stems"] == {}
    assert "vocal_presence" in result["unknowns"] and "perceived_hardness" in result["unknowns"]
    assert all(isinstance(v, float) and math.isfinite(v) for v in result["features"].values())
    assert not any("hardness" in key or "vocal" in key or "genre" in key for key in result["features"])
    json.dumps(result, allow_nan=False)


def test_default_real_marker_and_required_identity(source):
    assert analyze_wav(source, "track", "release")["is_fixture"] is False
    for track in ("", " ", "name\n", None):
        with pytest.raises(AudioAnalysisError, match="track_id"):
            analyze_wav(source, track, "release")
    with pytest.raises(AudioAnalysisError, match="boolean"):
        analyze_wav(source, "track", "release", is_fixture="false")


@pytest.mark.parametrize("width", [1, 2, 3, 4])
def test_pcm_formats_measure_sine_as_expected(tmp_path, width):
    result = measured(write_wave(tmp_path / f"pcm-{width}.wav", width=width))
    features = result["features"]
    assert features["rms_mean"] == pytest.approx(.5 / math.sqrt(2), abs=.005)
    assert features["crest_factor_mean"] == pytest.approx(math.sqrt(2), abs=.02)
    if width > 1:
        assert features["spectral_centroid_hz_mean"] == pytest.approx(440, abs=2)
    # 8-bit quantization adds high-frequency harmonics, shifting the magnitude centroid.
    assert features["spectral_rolloff_85_hz_mean"] == pytest.approx(440, abs=5)
    assert features["zero_crossing_rate_mean"] == pytest.approx(2 * 440 / 16000, abs=.001)


def test_different_tones_produce_different_spectral_measurements_and_same_rms(tmp_path):
    low = measured(write_wave(tmp_path / "low.wav", frequency=220))["features"]
    high = measured(write_wave(tmp_path / "high.wav", frequency=2000))["features"]
    assert high["spectral_centroid_hz_mean"] > low["spectral_centroid_hz_mean"] * 8
    assert high["spectral_rolloff_85_hz_mean"] > low["spectral_rolloff_85_hz_mean"] * 8
    assert high["rms_mean"] == pytest.approx(low["rms_mean"], abs=.001)


def test_stereo_antiphase_does_not_become_silence(tmp_path):
    result = measured(write_wave(tmp_path / "stereo.wav", antiphase=True))
    assert result["channels"] == 2
    assert result["features"]["rms_mean"] > .35
    assert result["features"]["spectral_centroid_hz_mean"] == pytest.approx(440, abs=2)


@pytest.mark.parametrize(("seconds", "silent", "code"), [(.2, False, "TOO_SHORT"), (2, True, "SILENT_AUDIO")])
def test_short_and_silent_input_rejected(tmp_path, seconds, silent, code):
    path = write_wave(tmp_path / "invalid.wav", seconds=seconds, silent=silent)
    with pytest.raises(AudioAnalysisError) as error:
        measured(path)
    assert error.value.code == code


def test_malformed_truncated_and_float_wav_are_rejected(source, tmp_path):
    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"not a WAV")
    with pytest.raises(AudioAnalysisError) as error:
        measured(bad)
    assert error.value.code == "INVALID_WAV"
    truncated = source.read_bytes()[:-200]
    bad.write_bytes(truncated)
    with pytest.raises(AudioAnalysisError) as error:
        measured(bad)
    assert error.value.code == "TRUNCATED_WAV"
    floating = bytearray(source.read_bytes())
    struct.pack_into("<H", floating, 20, 3)  # IEEE float format is intentionally unsupported.
    bad.write_bytes(floating)
    with pytest.raises(AudioAnalysisError):
        measured(bad)


@pytest.mark.parametrize(("offset", "format", "value", "code"), [
    (34, "<H", 12, "UNSUPPORTED_WAV"),
    (32, "<H", 4, "INVALID_WAV"),
    (28, "<I", 12345, "INVALID_WAV"),
])
def test_actual_pcm_format_fields_are_validated(source, offset, format, value, code):
    payload = bytearray(source.read_bytes())
    struct.pack_into(format, payload, offset, value)
    source.write_bytes(payload)
    with pytest.raises(AudioAnalysisError) as error:
        measured(source)
    assert error.value.code == code


@pytest.mark.parametrize("valid_bits", [16, 12])
def test_extensible_pcm_requires_full_valid_bit_width(source, valid_bits):
    payload = source.read_bytes()
    fmt = bytearray(payload[20:36])
    struct.pack_into("<H", fmt, 0, 0xFFFE)
    fmt.extend(struct.pack("<HHI", 22, valid_bits, 0))
    fmt.extend(bytes.fromhex("0100000000001000800000aa00389b71"))
    body = b"WAVEfmt " + struct.pack("<I", len(fmt)) + fmt + payload[36:]
    source.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)
    if valid_bits == 16:
        assert measured(source)["sample_width_bits"] == 16
    else:
        with pytest.raises(AudioAnalysisError) as error:
            measured(source)
        assert error.value.code == "UNSUPPORTED_WAV"


def test_final_partial_window_and_silence_unknowns(tmp_path):
    path = write_wave(tmp_path / "partial.wav", seconds=1.0000625)
    result = measured(path)
    assert result["windows"][-1]["status"] == "too_short"
    assert result["windows"][-1]["features"] == {}
    assert all(math.isfinite(value) for value in result["features"].values())


def test_normal_path_never_imports_optional_ml_modules(source, monkeypatch):
    original = builtins.__import__
    def guarded(name, *args, **kwargs):
        if name.split(".")[0] in {"allin1", "demucs", "torch", "librosa"}:
            pytest.fail(f"unexpected optional dependency: {name}")
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", guarded)
    measured(source)
    assert main([str(source), "--track-id", "track", "--release-id", "release", "--fixture"]) == 0


def test_structure_import_preserves_numeric_features_and_is_not_gold(source):
    base = measured(source)
    result = attach(structure(), base)
    assert base["structure"] is None
    assert result["features"] == base["features"]
    assert result["structure"]["segments"][1]["label"] == "verse"
    assert result["structure"]["evidence_kind"] == "model_prediction_not_verified_annotation"
    assert "vocal_presence" in result["unknowns"]


@pytest.mark.parametrize("change", [
    {"beats": [1, .5]}, {"beats": [float("nan")]}, {"beats": [-1]},
    {"beats": [0.1, 4]}, {"downbeats": [.3]}, {"beat_positions": [1]},
    {"bpm": float("inf")}, {"bpm": True},
    {"segments": [{"start": 1, "end": .5, "label": "verse"}]},
    {"segments": [{"start": 0, "end": 2.3, "label": "verse"}]},
    {"segments": [{"start": 0, "end": 1, "label": "hard_vocals"}]},
    {"segments": [{"start": 0, "end": 1.5, "label": "intro"}, {"start": 1, "end": 2, "label": "verse"}]},
])
def test_invalid_model_output_rejected(source, change):
    with pytest.raises(AudioAnalysisError) as error:
        attach({**structure(), **change}, measured(source))
    assert error.value.code == "INVALID_STRUCTURE"


def test_unknown_structure_is_explicit_not_zero_score(source):
    result = attach({"bpm": None, "beats": [], "downbeats": [], "beat_positions": [], "segments": []}, measured(source))
    assert result["structure"]["bpm"] is None
    assert set(result["structure"]["unknowns"]) >= {"tempo", "beats", "sections"}


def test_allin1_disabled_and_missing_dependency_do_not_silently_fallback(source, monkeypatch):
    import discovery.audio.adapters as adapters
    base = measured(source)
    with pytest.raises(AudioAnalysisError) as error:
        analyze_allin1(source, base)
    assert error.value.code == "MODEL_EXECUTION_DISABLED"
    def missing(_):
        from importlib.metadata import PackageNotFoundError
        raise PackageNotFoundError("allin1")
    monkeypatch.setattr(adapters, "version", missing)
    with pytest.raises(AudioAnalysisError) as error:
        analyze_allin1(source, base, allow_model_execution=True)
    assert error.value.code == "OPTIONAL_DEPENDENCY_MISSING"


def test_allin1_calls_verified_api_only_on_opt_in(source, monkeypatch):
    import discovery.audio.adapters as adapters
    calls = []
    @dataclass
    class Segment:
        start: float
        end: float
        label: str
    @dataclass
    class Result:
        bpm: int
        beats: list
        downbeats: list
        beat_positions: list
        segments: list
    raw = structure()
    def analyze(path, **config):
        calls.append((path, config))
        return Result(**{**raw, "segments": [Segment(**item) for item in raw["segments"]]})
    monkeypatch.setattr(adapters, "version", lambda _: "test-package-version")
    monkeypatch.setitem(sys.modules, "allin1", SimpleNamespace(analyze=analyze))
    result = analyze_allin1(source, measured(source), allow_model_execution=True)
    assert len(calls) == 1
    assert calls[0][1]["device"] == "cpu" and calls[0][1]["multiprocess"] is False
    assert calls[0][1]["out_dir"] is None and calls[0][1]["include_embeddings"] is False
    assert result["structure"]["provenance"]["package_version"] == "test-package-version"
    assert "model_checkpoint_hash" in result["structure"]["unknowns"]


def test_external_source_hash_must_match(source):
    base = measured(source)
    with pytest.raises(AudioAnalysisError) as error:
        import_allin1_result(structure(), base, source_sha256="0" * 64,
                             model="harmonix-all", model_version="test", config={})
    assert error.value.code == "SOURCE_MISMATCH"


def test_explicit_stems_separate_features_unknown_leakage_and_silent_vocals(source, tmp_path):
    base = measured(source)
    vocals = write_wave(tmp_path / "vocals.wav", silent=True)
    instrumental = write_wave(tmp_path / "no_vocals.wav", frequency=1000)
    result = import_demucs_stems(base, vocals_path=vocals, instrumental_path=instrumental,
                                source_sha256=base["sha256"], model="htdemucs", model_version="test-revision",
                                config={"two_stems": "vocals", "clip_mode": "rescale"})
    assert base["stems"] == {} and result["features"] == base["features"]
    assert result["stems"]["vocals"]["status"] == "silent"
    assert "spectral_centroid_hz_mean" not in result["stems"]["vocals"]["features"]
    assert result["stems"]["instrumental"]["features"]["spectral_centroid_hz_mean"] == pytest.approx(1000, abs=2)
    assert "vocal_presence" in result["stems"]["vocals"]["unknowns"]
    assert "relative_stem_gain" in result["stems"]["instrumental"]["unknowns"]
    assert result["stems"]["instrumental"]["is_fixture"] is True
    assert result["stems"]["vocals"]["provenance"]["separation"]["source_sha256"] == base["sha256"]


def test_stem_duration_mismatch_and_same_path_rejected(source, tmp_path):
    base = measured(source)
    longer = write_wave(tmp_path / "longer.wav", seconds=3)
    kwargs = dict(source_sha256=base["sha256"], model="htdemucs", model_version="test", config={})
    with pytest.raises(AudioAnalysisError) as error:
        import_demucs_stems(base, vocals_path=source, instrumental_path=longer, **kwargs)
    assert error.value.code == "STEM_DURATION_MISMATCH"
    with pytest.raises(AudioAnalysisError) as error:
        import_demucs_stems(base, vocals_path=source, instrumental_path=source, **kwargs)
    assert error.value.code == "INVALID_STEMS"


def test_cli_writes_versioned_json_and_preserves_input(source, tmp_path):
    output = tmp_path / "analysis.json"
    args = [str(source), "--track-id", "track", "--release-id", "release", "--fixture"]
    assert main([*args, "--output", str(output)]) == 0
    result = json.loads(output.read_text())
    assert result["track_id"] == "track" and result["is_fixture"] is True
    before = source.read_bytes()
    with pytest.raises(SystemExit):
        main([*args, "--output", str(source)])
    assert source.read_bytes() == before
    malformed = tmp_path / "bad.wav"
    malformed.write_bytes(b"bad")
    assert main([str(malformed), *args[1:]]) == 2


def test_cli_optional_model_subprocess_progress_does_not_corrupt_json(source, monkeypatch, capfd):
    import discovery.audio.__main__ as cli

    def model(path, result, **kwargs):
        print("Python model progress")
        subprocess.run([sys.executable, "-c", "print('Demucs child progress')"], check=True)
        return result

    monkeypatch.setattr(cli, "analyze_allin1", model)
    assert main([str(source), "--track-id", "track", "--release-id", "release", "--allin1", "--fixture"]) == 0
    captured = capfd.readouterr()
    assert json.loads(captured.out)["track_id"] == "track"
    assert "Python model progress" in captured.err and "Demucs child progress" in captured.err
