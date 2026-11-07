"""Explicit page capture or offline parsing; neither runs a bulk crawler."""

import argparse
import hashlib
import json
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from discovery.catalog.web.common import ParseError


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    parse = commands.add_parser("parse", help="Parse one saved release HTML file without network")
    parse.add_argument("--source", choices=["rym", "aoty"], required=True)
    parse.add_argument("--html", type=Path, required=True)
    parse.add_argument("--url", required=True)
    parse.add_argument("--observed-at", type=datetime.fromisoformat, required=True,
                       help="Actual source capture time, including timezone")
    parse.add_argument("--output", type=Path, required=True)
    parse.add_argument("--fixture", action="store_true", help="Mark authored test input as synthetic")
    fetch = commands.add_parser("fetch", help="One bounded, robots-aware public page request")
    fetch.add_argument("--url", required=True)
    fetch.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "fetch":
            from discovery.catalog.web.fetch import fetch_html

            result = fetch_html(args.url, args.output_dir)
            print(json.dumps(asdict(result), default=str, indent=2))
            return
        if args.html.stat().st_size > 5_000_000:
            raise ParseError("INVALID_HTML", "Input exceeds the 5 MB limit")
        raw = args.html.read_bytes()
        html = raw.decode("utf-8-sig")
        if args.source == "rym":
            from discovery.catalog.web.rym import parse_rym

            record = parse_rym(html, url=args.url, observed_at=args.observed_at)
        else:
            from discovery.catalog.web.aoty import parse_aoty

            record = parse_aoty(html, url=args.url, observed_at=args.observed_at)
        payload = {
            "schema_version": 1,
            "dataset_kind": "unmatched_source_release",
            "is_fixture": args.fixture,
            "capture_verification": "synthetic_fixture" if args.fixture else "user_supplied_html",
            "rights_status": "synthetic_fixture" if args.fixture else "not_established_by_parser",
            "input_sha256": hashlib.sha256(raw).hexdigest(),
            "live_parser_verified": False,
            "personal_labels": 0,
            "record": record.model_dump(mode="json"),
            "limitations": [
                "Parsed source facts do not establish access or redistribution rights.",
                "No canonical MusicBrainz match is inferred from title or artist names.",
                "Community ratings are not the user's preference labels.",
            ],
        }
        # A failed parse never touches an existing export.
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({"status": "parsed", "output": str(args.output),
                          "source": record.source, "warnings": record.warnings}, ensure_ascii=False))
    except (ValueError, OSError, UnicodeError) as exc:
        from discovery.catalog.web.fetch import FetchBlocked

        error = {"status": "blocked" if isinstance(exc, FetchBlocked) else "failed",
                 "code": getattr(exc, "code", type(exc).__name__), "message": str(exc)}
        if isinstance(exc, FetchBlocked):
            error["http_status"] = exc.status_code
        print(json.dumps(error, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
