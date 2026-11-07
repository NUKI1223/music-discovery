"""Bounded PCM measurements; no learned labels, text, or network dependencies."""

import hashlib
import math
import os
import struct
import wave
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

SCHEMA_VERSION = "audio-analysis-v1"
FEATURE_VERSION = "pcm-window-v1"
WINDOW_SECONDS = 0.5
MIN_SECONDS = 1.0
MAX_SECONDS = 1800.0
MAX_BYTES = 512 * 1024 * 1024
SILENCE_RMS = 1e-5
PCM_SUBFORMAT = bytes.fromhex("0100000000001000800000aa00389b71")


class AudioAnalysisError(ValueError):
    """An invalid or unsupported local recording, with a machine-readable code."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _identifier(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise AudioAnalysisError("INVALID_ID", f"{name} must be an explicit nonempty ID (up to 200 characters).")
    if value != value.strip() or any(ord(char) < 32 for char in value):
        raise AudioAnalysisError("INVALID_ID", f"{name} contains whitespace or control characters.")
    return value


def _decode(raw: bytes, width: int, channels: int) -> np.ndarray:
    if width == 1:
        values = (np.frombuffer(raw, dtype=np.uint8).astype(np.float64) - 128) / 128
    elif width == 3:
        triples = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        integers = triples[:, 0] | (triples[:, 1] << 8) | (triples[:, 2] << 16)
        values = ((integers ^ 0x800000) - 0x800000).astype(np.float64) / 8388608
    else:
        values = np.frombuffer(raw, dtype=f"<i{width}").astype(np.float64) / (2 ** (8 * width - 1))
    return values.reshape(-1, channels)


def _validate_pcm_header(source, file_size: int) -> None:
    """wave rounds bit depths to bytes; validate the original format declaration too."""
    header = source.read(12)
    if len(header) != 12 or header[:4] != b"RIFF" or header[8:] != b"WAVE":
        raise AudioAnalysisError("INVALID_WAV", "Expected a little-endian RIFF WAVE file.")
    riff_end = struct.unpack_from("<I", header, 4)[0] + 8
    if riff_end > file_size:
        raise AudioAnalysisError("TRUNCATED_WAV", "The RIFF payload is shorter than its declared size.")
    while source.tell() + 8 <= riff_end:
        chunk = source.read(8)
        name, length = chunk[:4], struct.unpack_from("<I", chunk, 4)[0]
        if source.tell() + length > riff_end:
            raise AudioAnalysisError("TRUNCATED_WAV", "A WAV chunk exceeds the declared RIFF size.")
        if name == b"fmt ":
            if not 16 <= length <= 1024:
                raise AudioAnalysisError("INVALID_WAV", "Invalid PCM format chunk size.")
            payload = source.read(length)
            tag, channels, rate, byte_rate, alignment, bits = struct.unpack_from("<HHIIHH", payload)
            if tag not in {1, 0xFFFE} or bits not in {8, 16, 24, 32}:
                raise AudioAnalysisError("UNSUPPORTED_WAV", "Use integer PCM with exactly 8, 16, 24, or 32 bits.")
            if alignment != channels * (bits // 8) or byte_rate != rate * alignment:
                raise AudioAnalysisError("INVALID_WAV", "PCM byte rate or block alignment is inconsistent.")
            if tag == 0xFFFE:
                if (length < 40 or struct.unpack_from("<H", payload, 16)[0] < 22
                        or struct.unpack_from("<H", payload, 18)[0] != bits
                        or payload[24:40] != PCM_SUBFORMAT):
                    raise AudioAnalysisError("UNSUPPORTED_WAV", "Only full-width integer PCM extensible WAV is supported.")
            return
        source.seek(length + (length % 2), 1)
    raise AudioAnalysisError("INVALID_WAV", "Missing PCM format chunk.")


def _window_features(samples: np.ndarray, sample_rate: int) -> tuple[dict, bool]:
    rms = float(np.sqrt(np.mean(samples * samples)))
    peak = float(np.max(np.abs(samples)))
    features = {"rms": rms, "peak_amplitude": peak,
                "dc_offset_abs": float(np.mean(np.abs(np.mean(samples, axis=0))))}
    if rms < SILENCE_RMS:
        return features, True
    features["crest_factor"] = peak / rms
    features["zero_crossing_rate"] = float(np.mean(np.signbit(samples[1:]) != np.signbit(samples[:-1])))
    centered = samples - np.mean(samples, axis=0)
    spectrum = np.mean(np.abs(np.fft.rfft(centered * np.hanning(len(samples))[:, None], axis=0)), axis=1)
    mass = float(np.sum(spectrum))
    if mass > 1e-12:
        frequencies = np.fft.rfftfreq(len(samples), 1 / sample_rate)
        features["spectral_centroid_hz"] = float(np.dot(frequencies, spectrum) / mass)
        rolloff_bin = min(int(np.searchsorted(np.cumsum(spectrum), .85 * mass)), len(frequencies) - 1)
        features["spectral_rolloff_85_hz"] = float(frequencies[rolloff_bin])
    return features, False


def analyze_wav(path: str | Path, track_id: str, release_id: str,
                is_fixture: bool = False) -> dict:
    """Measure a local integer PCM WAV, preserving explicit catalog linkage and provenance."""
    return _analyze_wav(path, track_id, release_id, is_fixture=is_fixture, allow_silent=False)


def _analyze_wav(path: str | Path, track_id: str, release_id: str, *,
                 is_fixture: bool, allow_silent: bool) -> dict:
    track_id, release_id = _identifier(track_id, "track_id"), _identifier(release_id, "release_id")
    if not isinstance(is_fixture, bool):
        raise AudioAnalysisError("INVALID_FIXTURE", "is_fixture must be a boolean.")
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise AudioAnalysisError("MISSING_AUDIO", "A regular local WAV file is required.")
    windows = []
    square_sum = 0.0
    sample_count = 0
    try:
        with path.open("rb") as source:
            before = os.fstat(source.fileno())
            if before.st_size > MAX_BYTES:
                raise AudioAnalysisError("AUDIO_LIMIT", "WAV exceeds the 512 MiB local analysis limit.")
            _validate_pcm_header(source, before.st_size)
            source.seek(0)
            sha256 = hashlib.file_digest(source, "sha256").hexdigest()
            source.seek(0)
            with wave.open(source, "rb") as reader:
                rate, channels = reader.getframerate(), reader.getnchannels()
                width, total_frames = reader.getsampwidth(), reader.getnframes()
                if reader.getcomptype() != "NONE" or width not in {1, 2, 3, 4}:
                    raise AudioAnalysisError("UNSUPPORTED_WAV", "Use uncompressed integer PCM WAV (8/16/24/32-bit).")
                if not 8000 <= rate <= 192000 or not 1 <= channels <= 8:
                    raise AudioAnalysisError("UNSUPPORTED_WAV", "Supported rates: 8–192 kHz; channels: 1–8.")
                duration = total_frames / rate
                if duration < MIN_SECONDS:
                    raise AudioAnalysisError("TOO_SHORT", "At least one second of audio is required.")
                if duration > MAX_SECONDS:
                    raise AudioAnalysisError("AUDIO_LIMIT", "A single analysis is limited to 30 minutes.")
                window_frames = round(WINDOW_SECONDS * rate)
                for start in range(0, total_frames, window_frames):
                    frames = min(window_frames, total_frames - start)
                    raw = reader.readframes(frames)
                    if len(raw) != frames * channels * width:
                        raise AudioAnalysisError("TRUNCATED_WAV", "The WAV payload is shorter than its header declares.")
                    samples = _decode(raw, width, channels)
                    square_sum += float(np.sum(samples * samples))
                    sample_count += samples.size
                    # A final one-sample window cannot define a crossing rate or spectrum.
                    if len(samples) < 2:
                        windows.append({"start_seconds": start / rate, "end_seconds": duration,
                                        "status": "too_short", "features": {}})
                        continue
                    features, silent = _window_features(samples, rate)
                    windows.append({"start_seconds": start / rate, "end_seconds": (start + frames) / rate,
                                    "status": "silent" if silent else "measured", "features": features})
            after = os.fstat(source.fileno())
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise AudioAnalysisError("AUDIO_CHANGED", "The input changed during analysis; rerun on a stable file.")
    except (wave.Error, EOFError, OSError) as error:
        raise AudioAnalysisError("INVALID_WAV", "Cannot decode this file as an integer PCM WAV.") from error
    silent = math.sqrt(square_sum / sample_count) < SILENCE_RMS
    if silent and not allow_silent:
        raise AudioAnalysisError("SILENT_AUDIO", "The recording is silent or below the analysis RMS threshold.")
    names = sorted({key for window in windows for key in window["features"]})
    aggregate = {}
    for name in names:
        selected = [(window["features"][name], window["end_seconds"] - window["start_seconds"])
                    for window in windows if name in window["features"]]
        values, weights = np.array(selected).T
        mean = float(np.average(values, weights=weights))
        aggregate[name + "_mean"] = mean
        aggregate[name + "_std"] = float(np.sqrt(np.average((values - mean) ** 2, weights=weights)))
    unknowns = ["functional_structure", "tempo", "vocal_presence", "vocal_delivery",
                "instrumentation", "perceived_hardness", "genre", "taste_match"]
    if any("spectral_centroid_hz" not in window["features"] for window in windows):
        unknowns.append("spectral_features_in_silent_or_constant_windows")
    return {
        "schema_version": SCHEMA_VERSION, "feature_version": FEATURE_VERSION,
        "track_id": track_id, "release_id": release_id, "is_fixture": is_fixture,
        "sha256": sha256, "sample_rate": rate, "channels": channels,
        "sample_width_bits": width * 8, "duration_seconds": duration,
        "status": "silent" if silent else "measured", "features": aggregate, "windows": windows,
        "unknowns": unknowns, "structure": None, "stems": {},
        "provenance": {"method": "local_pcm_measurements", "software": "discovery.audio",
                       "software_version": FEATURE_VERSION, "numpy_version": np.__version__,
                       "source_path": str(path), "source_bytes": before.st_size,
                       "created_at": datetime.now(UTC).isoformat(),
                       "identity_basis": "caller_supplied_ids_not_filename_inference"},
        "config": {"window_seconds": WINDOW_SECONDS, "overlap_seconds": 0,
                   "spectrum": "mean_channel_magnitude_rfft_hann_after_per_channel_mean_removal",
                   "rms": "all_channels_without_downmix", "zero_crossing_rate": "raw_sign_changes_per_channel",
                   "rolloff_fraction": .85, "silence_rms_threshold": SILENCE_RMS,
                   "aggregate": "duration_weighted_mean_and_population_std_of_defined_windows"},
    }
