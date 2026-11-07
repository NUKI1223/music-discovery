"""Free, isolated GOLD regression evaluation of baseline and experimental ranker.

Replays frozen, previously confirmed filters. Does not test a fresh chat/LLM call.
No expected artist names or sound descriptions enter model features or profiles.
"""

import argparse
import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path

from discovery.catalog import load_catalog
from discovery.config import DEFAULT_CATALOG, PROJECT_ROOT
from discovery.contracts import Catalog, Decision, UserProfile
from discovery.evals.gold import CASES_PATH, SOUND_PATH, build_gold_manifest, sound_readiness
from discovery.evals.user_recommendations import grade_case, name_key
from discovery.ranking import rank
from discovery.ranking.baseline import FEATURE_VERSION, RANKER_VERSION
from discovery.storage import catalog_digest
from discovery.training.serving import artifact_hash, load_artifact
from discovery.web.cards import decision_cards
from discovery.web.schemas import Filters, Message

INPUTS_PATH = PROJECT_ROOT / "data/evals/user-confirmed-inputs-v1.json"


def evaluate_gold(catalog: Catalog, artifact: dict | None = None, *,
                  cases_path: Path = CASES_PATH, sound_path: Path = SOUND_PATH,
                  inputs_path: Path = INPUTS_PATH, as_of: date | None = None) -> dict:
    dataset = json.loads(cases_path.read_text())
    inputs = json.loads(inputs_path.read_text())
    as_of = as_of or date.fromisoformat(inputs.get("as_of", "2026-11-01"))
    manifest = build_gold_manifest(catalog, sound_path=sound_path, cases_path=cases_path)
    training_ids = artifact.get("training_release_ids") if artifact else None
    provenance_available = isinstance(training_ids, list) and all(isinstance(i, str) for i in training_ids)
    overlap = sorted(set(training_ids or []) & set(manifest["protected_release_ids"])) if provenance_available else []
    isolation = {"status": "not_applicable" if artifact is None else "contaminated" if overlap
                 else "checked_current_catalog" if provenance_available else "unknown_training_provenance",
                 "overlapping_release_ids": overlap,
                 "historical_identity_check": "requires_training_run_manifest" if artifact else "not_applicable"}
    by_id = {case["id"]: case for case in inputs["cases"]}
    if len(by_id) != len(inputs["cases"]) or set(by_id) != {c["id"] for c in dataset["cases"]}:
        raise ValueError("Frozen inputs and GOLD cases disagree")
    results = []
    for case in dataset["cases"]:
        saved = by_id[case["id"]]
        if saved.get("confirmed_in_source") is not True:
            raise ValueError("Only explicitly confirmed filters may be replayed")
        filters = Filters.model_validate(saved["filters"])
        listener_keys = {name_key(n) for n in case["listener_artist_names"]}
        seeds = [a.id for a in catalog.artists if name_key(a.name) in listener_keys]
        profile = UserProfile(seed_artist_ids_by_genre={filters.genre_scope_id: seeds})
        excluded = sorted(set(filters.excluded_artist_ids) | (set(seeds)
                          if filters.exclude_known_artists else set()))
        intent = filters.intent(count=case["expectations"]["count"], excluded_artist_ids=excluded)
        row = {"id": case["id"], "filters": filters.model_dump(),
               "missing_listener_artists": [n for n in case["listener_artist_names"]
                    if not any(name_key(a.name) == name_key(n) for a in catalog.artists)]}
        for label, model in [("baseline", None)] + ([("candidate", artifact)] if artifact else []):
            try:
                filters.validate_catalog(catalog)
                result = rank(catalog, intent, profile, as_of=as_of,
                              artifact=model, _experimental=model is not None)
                decision = Decision(id=f"gold-{label}-{case['id']}", request=intent,
                    catalog_snapshot_id=catalog.snapshot_id, catalog_hash=catalog_digest(catalog),
                    taxonomy_version=catalog.taxonomy_version, profile=profile, result=result,
                    as_of=as_of, feature_version=FEATURE_VERSION, ranker_version=RANKER_VERSION)
                message = Message(id=decision.id, role="assistant", text="Offline GOLD replay",
                    created_at=datetime.now(UTC).isoformat(), cards=decision_cards(catalog, decision))
                applied = model is not None and bool(result.items) and all(
                    "experimental_personal_model" in item.reason_codes for item in result.items)
                row[label] = {
                    "cards": [c.model_dump(mode="json") for c in message.cards],
                    "grading": grade_case(dataset, case, catalog, message, filters),
                    "candidate_applied": applied,
                    "warnings": result.warnings,
                }
                if model is not None and not applied:
                    row[label]["grading"].update(status="failed", passed=False)
                    row[label]["grading"]["checks"]["candidate_applied"] = False
            except (ValueError, TypeError) as error:
                row[label] = {"status": "invalid_inputs_or_model", "error_type": type(error).__name__,
                              "grading": {"status": "failed", "passed": False},
                              "candidate_applied": False}
        results.append(row)
    failed = bool(overlap) or any(row[label]["grading"]["status"] == "failed" for row in results
                 for label in ("baseline", "candidate") if label in row)
    code = hashlib.sha256()
    for source in sorted((PROJECT_ROOT / "src/discovery").rglob("*.py")):
        code.update(str(source.relative_to(PROJECT_ROOT)).encode())
        code.update(source.read_bytes())
    return {
        "schema_version": 1, "boundary": "offline_confirmed_filter_ranking",
        "as_of": as_of.isoformat(), "status": "failed" if failed else "needs_human_review",
        "passed": False, "serving_enabled": False,
        "live_api_verified": False, "reserved_or_actual_usd": 0,
        "catalog_hash": catalog_digest(catalog), "gold_digest": manifest["digest"],
        "feature_version": FEATURE_VERSION, "ranker_version": RANKER_VERSION,
        "code_hash": code.hexdigest(),
        "inputs_sha256": hashlib.sha256(inputs_path.read_bytes()).hexdigest(),
        "artifact_hash": artifact_hash(artifact) if artifact else None,
        "training_isolation": isolation,
        "cases": results, "sound": sound_readiness(sound_path),
        "scope": "Regression GOLD already discussed during development; not a blind test",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=PROJECT_ROOT / DEFAULT_CATALOG)
    parser.add_argument("--model", type=Path, help="Evaluate candidate in shadow; never enables serving")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-docx", type=Path, help="Verify original Word file hash")
    parser.add_argument("--as-of", type=date.fromisoformat)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Report exists; choose a new output path")
    catalog = load_catalog(args.catalog)
    if args.source_docx:
        dataset = json.loads(CASES_PATH.read_text())
        if hashlib.sha256(args.source_docx.read_bytes()).hexdigest() != dataset["source"]["sha256"]:
            parser.error("Word document changed; review the GOLD transcription before evaluation")
    report = evaluate_gold(catalog, load_artifact(args.model) if args.model else None,
                           as_of=args.as_of)
    report["word_document_hash_verified"] = args.source_docx is not None
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as out:
        json.dump(report, out, ensure_ascii=False, indent=2, allow_nan=False)
        out.write("\n")
    print(json.dumps({"report": str(args.output), "status": report["status"],
                      "sound": report["sound"]["status"], "cost_usd": 0}, ensure_ascii=False))
    return 1 if report["status"] == "failed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
