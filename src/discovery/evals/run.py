"""Explicitly offline eval runner with versioned reports and fault-test suites."""

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from discovery.evals.scenarios import run_scenarios

SUITES = {
    "core": ["tests/contracts", "tests/storage", "tests/catalog", "tests/ranking", "tests/feedback"],
    "ranking": ["tests/ranking"], "llm": ["tests/llm"], "agent": ["tests/agent"],
    "bandit": ["tests/bandit"], "release": ["tests"],
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", choices=SUITES, default="core")
    parser.add_argument("--offline", action="store_true", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--test-timeout", type=float, default=900,
                        help="Seconds allowed for the pytest subprocess.")
    parser.add_argument("--skip-tests", action="store_true",
                        help="Run only scenarios, e.g. when CI runs pytest as a separate visible step.")
    args = parser.parse_args()
    scenarios = run_scenarios()
    if args.skip_tests:
        test_exit_code, test_output = 0, "skipped: pytest runs as a separate step"
    else:
        try:
            tests = subprocess.run([sys.executable, "-m", "pytest", "-q", *SUITES[args.suite]],
                                   capture_output=True, text=True, timeout=args.test_timeout)
            test_exit_code, test_output = tests.returncode, tests.stdout + tests.stderr
        except subprocess.TimeoutExpired as error:
            partial = error.stdout or ""
            partial = partial.decode(errors="replace") if isinstance(partial, bytes) else partial
            test_exit_code = 124
            test_output = f"pytest timed out after {args.test_timeout:g}s\n" + partial
    code = hashlib.sha256()
    for path in sorted(Path("src/discovery").rglob("*.py")):
        code.update(str(path).encode())
        code.update(path.read_bytes())
    report = {
        "suite": args.suite, "timestamp": datetime.now(UTC).isoformat(),
        "code_hash": code.hexdigest(), "catalog_snapshot": "frozen-evals-v1",
        "evidence": "synthetic_engineering_only", "live_api_verified": False,
        "musical_usefulness_verified": False, "scenarios": scenarios,
        "tests_skipped": args.skip_tests,
        "test_exit_code": test_exit_code, "test_output": test_output,
        "passed": all(s["passed"] for s in scenarios) and test_exit_code == 0,
    }
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload)
    print(payload)
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
