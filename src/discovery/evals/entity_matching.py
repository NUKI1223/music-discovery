"""Offline pair evaluation against provisional, explicitly reviewed reference labels.

This is a purposive diagnostic set, not population accuracy or human ground truth.
An unresolved reference is excluded from binary metrics; merging it is reported
as an unsupported decision, never misrepresented as a proven false merge.
"""

import argparse
import hashlib
import json
from collections import Counter
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

from discovery.catalog import matching
from discovery.contracts import Artist, Release

EVAL_VERSION = "entity-matching-eval-v1"
DEFAULT_REFERENCE = Path("data/evals/entity-matching-gold.json")
DECISIONS = {"match", "reject", "abstain"}
KINDS = {"real_snapshot", "synthetic_trap"}


def validate_reference(reference: dict) -> None:
    """Fail clearly instead of silently dropping malformed or mixed-provenance cases."""
    if reference.get("scope") != "pair_level_only" or not reference.get("version"):
        raise ValueError("Expected a versioned pair-level reference")
    if reference.get("labels_generated_by_matcher") is not False:
        raise ValueError("Matcher-generated labels cannot be used as this reference")
    cases = reference.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("Reference needs at least one case")
    ids = [case.get("id") for case in cases]
    if any(not isinstance(cid, str) or not cid for cid in ids) or len(set(ids)) != len(ids):
        raise ValueError("Case IDs must be nonempty and unique")
    artists = {artist["id"]: Artist.model_validate(artist) for artist in reference["artists"]}
    for case in cases:
        if case.get("expected") not in DECISIONS or case.get("kind") not in KINDS:
            raise ValueError("Unknown reference label or evidence kind")
        if not case.get("rationale"):
            raise ValueError("Every label requires a review rationale")
        real = case["kind"] == "real_snapshot"
        if case["row"].get("is_fixture") is not (not real):
            raise ValueError("Synthetic and real source inputs must remain separated")
        if real and case.get("review_status") != (
            "pending" if case["expected"] == "abstain" else "agent_reviewed"
        ):
            raise ValueError("Real unresolved labels must remain pending")
        if not real and (not case.get("derived_from") or "mutation" not in case):
            raise ValueError("Synthetic cases require their origin and explicit mutation")
        release = Release.model_validate(case["release"])
        if any(aid not in artists for aid in release.artist_ids):
            raise ValueError("Reference release has an unknown artist")


def verify_source_files(reference: dict, root: Path) -> list[dict]:
    """Check local snapshot bytes only. A hash is integrity, not platform authenticity."""
    checks = []
    for source in reference["source_files"]:
        path = root / source["path"]
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != source["sha256"]:
            raise ValueError(f"Reference source snapshot changed: {source['path']}")
        checks.append({"path": source["path"], "sha256": actual})
    return checks


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _metrics(results: list[dict]) -> dict:
    resolved = [r for r in results if r["expected"] != "abstain"]
    positives = [r for r in resolved if r["expected"] == "match"]
    negatives = [r for r in resolved if r["expected"] == "reject"]
    unresolved = [r for r in results if r["expected"] == "abstain"]
    tp = sum(r["predicted"] == "match" for r in positives)
    fp = sum(r["predicted"] == "match" for r in negatives)
    abstained = sum(r["predicted"] == "abstain" for r in results)
    unresolved_merges = sum(r["predicted"] == "match" for r in unresolved)
    return {
        "pairs": len(results), "resolved_pairs": len(resolved),
        "positive_labels": len(positives), "negative_labels": len(negatives),
        "pending_reference_pairs": len(unresolved),
        "unique_source_rows": len({r["source_row_id"] for r in results}),
        "unique_candidate_releases": len({r["candidate_release_id"] for r in results}),
        "true_matches": tp, "false_merges": fp,
        "missed_positive_pairs": len(positives) - tp,
        "positive_rejections": sum(r["predicted"] == "reject" for r in positives),
        "positive_abstentions": sum(r["predicted"] == "abstain" for r in positives),
        "negative_rejections": sum(r["predicted"] == "reject" for r in negatives),
        "negative_abstentions": sum(r["predicted"] == "abstain" for r in negatives),
        "precision_on_resolved_pairs": _ratio(tp, tp + fp),
        "recall_on_resolved_positives": _ratio(tp, len(positives)),
        "false_merge_rate_on_resolved_negatives": _ratio(fp, len(negatives)),
        "prediction_abstentions": abstained,
        "prediction_abstain_rate": _ratio(abstained, len(results)),
        "automatic_match_rate": _ratio(sum(r["predicted"] == "match" for r in results), len(results)),
        "unsupported_merges_on_pending_reference": unresolved_merges,
        "decisions": dict(Counter(r["predicted"] for r in results)),
        "safety_checks_passed": fp == 0 and unresolved_merges == 0,
    }


def evaluate_reference(reference: dict, *, classifier=None) -> dict:
    """Run frozen input copies; a pending prediction is not an automatic failure.

    Missed positives lower recall but do not fail the conservative merge safety
    check. Rejecting an unresolved pair is not a proven true negative either.
    """
    validate_reference(reference)
    classifier = classifier or matching.classify_pair
    artists = {item["id"]: Artist.model_validate(item) for item in reference["artists"]}
    results = []
    for case in reference["cases"]:
        result = classifier(
            deepcopy(case["row"]), Release.model_validate(deepcopy(case["release"])),
            deepcopy(artists), artist_aliases=deepcopy(reference.get("artist_aliases", {})),
        )
        if result.get("decision") not in DECISIONS or not isinstance(result.get("rule"), str):
            raise ValueError(f"Invalid matcher decision for {case['id']}")
        results.append({
            "case_id": case["id"], "kind": case["kind"], "source": case["row"]["source"],
            "source_row_id": case["row"]["row_id"],
            "candidate_release_id": case["release"]["id"],
            "expected": case["expected"], "predicted": result["decision"],
            "rule": result["rule"], "rationale": case["rationale"],
        })
    real = [r for r in results if r["kind"] == "real_snapshot"]
    synthetic = [r for r in results if r["kind"] == "synthetic_trap"]
    real_metrics, synthetic_metrics = _metrics(real), _metrics(synthetic)
    return {
        "eval_version": EVAL_VERSION, "reference_version": reference["version"],
        "label_authority": reference.get("label_authority"),
        "human_review_complete": reference.get("human_review_complete", False),
        "real_pairs": real_metrics, "synthetic_traps": synthetic_metrics,
        "real_by_source": {source: _metrics([r for r in real if r["source"] == source])
                           for source in sorted({r["source"] for r in real})},
        "safety_checks_passed": real_metrics["safety_checks_passed"]
        and synthetic_metrics["safety_checks_passed"],
        "population_accuracy_established": False, "dataset_uniqueness_tested": False,
        "results": results,
    }


def run_reference(path: Path = DEFAULT_REFERENCE, *, verify_root: Path | None = None) -> dict:
    raw = path.read_bytes()
    reference = json.loads(raw)
    report = evaluate_reference(reference)
    report.update(
        timestamp=datetime.now(UTC).isoformat(),
        reference_sha256=hashlib.sha256(raw).hexdigest(),
        matcher_version=matching.MATCHING_VERSION,
        matcher_code_sha256=hashlib.sha256(Path(matching.__file__).read_bytes()).hexdigest(),
        eval_code_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        source_hashes_verified=verify_root is not None,
        source_snapshots=verify_source_files(reference, verify_root) if verify_root else [],
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", type=Path, default=DEFAULT_REFERENCE)
    parser.add_argument("--verify-source-root", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = run_reference(args.gold, verify_root=args.verify_source_root)
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    print(payload)
    raise SystemExit(0 if report["safety_checks_passed"] else 1)


if __name__ == "__main__":
    main()
