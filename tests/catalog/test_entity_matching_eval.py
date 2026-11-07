"""Check metric semantics and provenance, not an implementation-shaped success score."""

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest

from discovery.evals.entity_matching import (
    evaluate_reference,
    run_reference,
    validate_reference,
    verify_source_files,
)

ROOT = Path(__file__).resolve().parents[2]
GOLD = ROOT / "data/evals/entity-matching-gold.json"


@pytest.fixture
def reference():
    return json.loads(GOLD.read_text())


def subset(reference, case_ids):
    reference["cases"] = [c for c in reference["cases"] if c["id"] in case_ids]
    return reference


def predictions(*decisions):
    iterator = iter(decisions)

    def classify(*args, **kwargs):
        return {"decision": next(iterator), "rule": "controlled_test_prediction"}

    return classify


def test_precision_recall_include_abstained_positives_but_not_pending_labels(reference):
    subset(reference, {"real-01", "real-02", "real-31", "real-45"})
    report = evaluate_reference(reference, classifier=predictions("match", "abstain", "match", "abstain"))
    metric = report["real_pairs"]
    assert metric["precision_on_resolved_pairs"] == 0.5
    assert metric["recall_on_resolved_positives"] == 0.5
    assert metric["positive_abstentions"] == 1
    assert metric["false_merges"] == 1
    assert metric["resolved_pairs"] == 3
    assert metric["pending_reference_pairs"] == 1
    assert metric["prediction_abstain_rate"] == 0.5
    assert report["safety_checks_passed"] is False


def test_all_pending_is_safe_but_not_perfect_precision_or_recall(reference):
    subset(reference, {"real-01", "real-31", "real-45"})
    report = evaluate_reference(reference, classifier=predictions("abstain", "abstain", "abstain"))
    metric = report["real_pairs"]
    assert report["safety_checks_passed"] is True
    assert metric["precision_on_resolved_pairs"] is None
    assert metric["recall_on_resolved_positives"] == 0
    assert metric["prediction_abstain_rate"] == 1
    assert metric["automatic_match_rate"] == 0


def test_merging_unresolved_pair_is_unsupported_not_proven_false_merge(reference):
    subset(reference, {"real-45"})
    report = evaluate_reference(reference, classifier=predictions("match"))
    metric = report["real_pairs"]
    assert metric["false_merges"] == 0
    assert metric["unsupported_merges_on_pending_reference"] == 1
    assert metric["precision_on_resolved_pairs"] is None
    assert metric["recall_on_resolved_positives"] is None
    assert report["safety_checks_passed"] is False


def test_synthetic_safety_failure_never_changes_real_accuracy(reference):
    subset(reference, {"real-01", "synthetic-same-title-other-artist"})
    report = evaluate_reference(reference, classifier=predictions("match", "match"))
    assert report["real_pairs"]["precision_on_resolved_pairs"] == 1
    assert report["real_pairs"]["false_merges"] == 0
    assert report["synthetic_traps"]["false_merges"] == 1
    assert report["safety_checks_passed"] is False


def test_actual_matcher_safety_on_reviewed_reference(reference):
    report = evaluate_reference(reference)
    assert report["real_pairs"]["pairs"] == 50
    assert report["real_pairs"]["positive_labels"] == 30
    assert report["real_pairs"]["negative_labels"] == 14
    assert report["real_pairs"]["pending_reference_pairs"] == 6
    assert report["synthetic_traps"]["pairs"] == 6
    assert report["safety_checks_passed"] is True
    assert report["human_review_complete"] is False
    assert report["population_accuracy_established"] is False
    assert report["dataset_uniqueness_tested"] is False


def test_frozen_real_rows_are_exact_copies_of_pinned_sources(reference):
    # This integration check requires local source snapshots, unlike the standalone
    # eval which can replay the embedded reference without the larger datasets.
    files = [ROOT / source["path"] for source in reference["source_files"]]
    if not all(path.exists() for path in files):
        pytest.skip("Local historical source snapshots are absent; embedded replay remains available")
    verify_source_files(reference, ROOT)
    source_rows = {}
    for source in ("rym", "aoty"):
        path = ROOT / f"data/community/{source}-historical/releases.jsonl"
        source_rows.update({r["row_id"]: r for r in map(json.loads, path.read_text().splitlines())})
    catalog = json.loads((ROOT / "data/catalogs/musicbrainz-expanded.json").read_text())
    releases = {r["id"]: r for r in catalog["releases"]}
    for case in reference["cases"]:
        if case["kind"] == "real_snapshot":
            assert case["row"] == source_rows[case["row"]["row_id"]]
            assert case["release"] == releases[case["release"]["id"]]
            assert case["row"]["observed_at"] is None
            assert case["evidence"]["source_release_type"] is None


def test_hash_check_detects_snapshot_drift(tmp_path):
    path = tmp_path / "source.json"
    path.write_text("original")
    reference = {"source_files": [{"path": path.name, "sha256": hashlib.sha256(b"original").hexdigest()}]}
    assert len(verify_source_files(reference, tmp_path)) == 1
    path.write_text("modified")
    with pytest.raises(ValueError, match="snapshot changed"):
        verify_source_files(reference, tmp_path)


@pytest.mark.parametrize("mutation", ["duplicate", "fixture", "matcher_labels", "unknown_label", "resolved_pending"])
def test_reference_validation_rejects_provenance_and_label_corruption(reference, mutation):
    if mutation == "duplicate":
        reference["cases"].append(deepcopy(reference["cases"][0]))
    elif mutation == "fixture":
        reference["cases"][0]["row"]["is_fixture"] = True
    elif mutation == "matcher_labels":
        reference["labels_generated_by_matcher"] = True
    elif mutation == "unknown_label":
        reference["cases"][0]["expected"] = "accepted"
    else:
        reference["cases"][0]["review_status"] = "pending"
    with pytest.raises(ValueError):
        validate_reference(reference)


def test_callback_cannot_mutate_frozen_inputs_or_other_cases(reference):
    subset(reference, {"real-01", "real-02"})
    before = deepcopy(reference)

    def destructive_callback(row, release, artists, **kwargs):
        assert row["title"] == "The Money Store"
        row["title"] = "mutated"
        artists.clear()
        kwargs["artist_aliases"].clear()
        release.title = "mutated"
        return {"decision": "abstain", "rule": "deliberate_test_mutation"}

    evaluate_reference(reference, classifier=destructive_callback)
    assert reference == before


def test_invalid_matcher_output_is_not_silently_counted(reference):
    with pytest.raises(ValueError, match="Invalid matcher decision"):
        evaluate_reference(reference, classifier=predictions("pending"))


def test_report_pins_exact_reference_and_code_bytes():
    report = run_reference(GOLD)
    assert report["reference_sha256"] == hashlib.sha256(GOLD.read_bytes()).hexdigest()
    assert len(report["matcher_code_sha256"]) == 64
    assert len(report["eval_code_sha256"]) == 64
    assert report["source_hashes_verified"] is False
