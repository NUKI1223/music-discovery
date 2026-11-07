import argparse
import json
from pathlib import Path

from .learning import evaluate


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    config = json.loads(args.config.read_text()) if args.config else {}
    report = evaluate(**config)
    payload = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload+"\n")
    print(payload)


if __name__ == "__main__":
    main()
