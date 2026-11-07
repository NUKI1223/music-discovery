"""CLI: no dataset means insufficient_data, never fabricated training labels."""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from .pipeline import TrainingRecord, train


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path)
    parser.add_argument("--data", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    config = {}
    if args.config:
        # JSON is also valid YAML; model configs use explicit JSON for stdlib-only loading.
        config = json.loads(args.config.read_text())
    data_path = args.data or (Path(config["data_path"]) if config.get("data_path") else None)
    rows = [TrainingRecord(**r) for r in json.loads(data_path.read_text())] if data_path else []
    options = {k: v for k, v in config.items() if k != "data_path"}
    options.setdefault("cutoff", datetime.now(timezone.utc).isoformat())
    result = train(rows, **options).to_dict()
    payload = json.dumps(result, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n")
    print(payload)


if __name__ == "__main__":
    main()
