"""Run local PCM measurements and explicitly enabled optional adapters."""

import argparse
import json
import os
import sys
from contextlib import contextmanager, redirect_stdout
from pathlib import Path

from .adapters import analyze_allin1, import_allin1_result, import_demucs_stems
from .measure import AudioAnalysisError, analyze_wav


@contextmanager
def _model_progress_to_stderr():
    """CLI-only: redirect both Python prints and inherited subprocess stdout."""
    sys.stdout.flush()
    saved = os.dup(1)
    try:
        os.dup2(2, 1)
        with redirect_stdout(sys.stderr):
            yield
    finally:
        sys.stderr.flush()
        os.dup2(saved, 1)
        os.close(saved)


def _read_external_json(path: Path):
    if path.stat().st_size > 16 * 1024 * 1024:
        raise AudioAnalysisError("INPUT_LIMIT", "External analysis JSON is limited to 16 MiB.")
    return json.loads(path.read_text())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Analyze local WAV measurements; no models run by default.")
    parser.add_argument("path", type=Path)
    parser.add_argument("--track-id", required=True)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--fixture", action="store_true", help="Mark generated/test audio; never use fixture output as real evidence.")
    structure = parser.add_mutually_exclusive_group()
    structure.add_argument("--allin1", action="store_true", help="Explicitly run optional allin1; may download model weights.")
    structure.add_argument("--allin1-json", type=Path, help="Import an already generated allin1 result, without model execution.")
    parser.add_argument("--allin1-model", default="harmonix-all")
    parser.add_argument("--allin1-version", help="Required version provenance for --allin1-json.")
    parser.add_argument("--device", default="cpu", choices=["cpu", "cuda", "mps"])
    parser.add_argument("--source-sha256", help="Required original WAV hash for importing external structure/stems.")
    parser.add_argument("--vocals", type=Path)
    parser.add_argument("--instrumental", type=Path)
    parser.add_argument("--demucs-model")
    parser.add_argument("--demucs-version")
    parser.add_argument("--external-config", type=Path, help="JSON model configuration for imported results/stems.")
    args = parser.parse_args(argv)
    if args.output and args.output.resolve() in {
        path.resolve() for path in (args.path, args.allin1_json, args.vocals, args.instrumental, args.external_config) if path
    }:
        parser.error("Output must not overwrite an input file.")
    if args.allin1_json and not (args.allin1_version and args.source_sha256 and args.external_config):
        parser.error("--allin1-json requires --allin1-version, --source-sha256, and --external-config.")
    if (args.vocals or args.instrumental) and not all((args.vocals, args.instrumental,
            args.demucs_model, args.demucs_version, args.source_sha256, args.external_config)):
        parser.error("Stem import requires both paths, model, version, source hash, and external config.")
    try:
        result = analyze_wav(args.path, args.track_id, args.release_id, is_fixture=args.fixture)
        config = _read_external_json(args.external_config) if args.external_config else {}
        if args.allin1:
            # Model libraries print progress; stdout remains one valid JSON document.
            with _model_progress_to_stderr():
                result = analyze_allin1(args.path, result, allow_model_execution=True,
                                        model=args.allin1_model, device=args.device)
        elif args.allin1_json:
            result = import_allin1_result(_read_external_json(args.allin1_json), result,
                                          source_sha256=args.source_sha256, model=args.allin1_model,
                                          model_version=args.allin1_version, config=config)
        if args.vocals:
            result = import_demucs_stems(result, vocals_path=args.vocals, instrumental_path=args.instrumental,
                                         source_sha256=args.source_sha256, model=args.demucs_model,
                                         model_version=args.demucs_version, config=config)
        payload = json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(payload)
        else:
            sys.stdout.write(payload)
    except (AudioAnalysisError, OSError, json.JSONDecodeError) as error:
        print(f"{getattr(error, 'code', 'INPUT_ERROR')}: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
