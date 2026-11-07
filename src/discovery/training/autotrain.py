"""Repeatable, account-local experimental training; never changes serving configuration.

GOLD annotations are exclusion/evaluation data, never training labels. Only explicit
listened feedback from the selected database enters the existing training pipeline.
"""

import argparse
import fcntl
import hashlib
import importlib.metadata
import json
import os
import sqlite3
import tempfile
import time
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from discovery.catalog.io import load_catalog
from discovery.cli import training_records
from discovery.contracts import Catalog
from discovery.ranking.baseline import FEATURE_VERSION
from discovery.storage import Store, catalog_digest
from discovery.training.pipeline import TrainingRecord, predict, timestamp, train
from discovery.training.serving import artifact_hash, load_artifact, promote_artifact

REGISTRY = "experimental_registry.json"


@dataclass(frozen=True)
class CycleConfig:
    minimum_labels: int = 60
    minimum_minority: int = 15
    seed: int = 17
    feature_version: str = FEATURE_VERSION
    minimum_holdout: int = 20
    minimum_holdout_class: int = 5
    evaluation_as_of: str | None = None

    def __post_init__(self):
        if self.evaluation_as_of is not None:
            date.fromisoformat(self.evaluation_as_of)
        if self.minimum_labels < 60 or self.minimum_minority < 15:
            raise ValueError("Training requires at least 60 labels and 15 per minority class")
        if self.minimum_holdout < 20 or self.minimum_holdout_class < 5:
            raise ValueError("Holdout requires at least 20 labels and five per class")


def _hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False).encode()).hexdigest()


def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=path.parent, suffix=".tmp", delete=False) as out:
            name = out.name
            json.dump(value, out, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


@contextmanager
def cycle_lock(output_dir: Path):
    output_dir.mkdir(parents=True, exist_ok=True)
    output_dir.chmod(0o700)
    with (output_dir / ".autotrain.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


class ReadOnlyStore(Store):
    """Read a consistent private snapshot without running Store schema migrations."""
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            yield conn
        finally:
            conn.close()


@contextmanager
def snapshot_store(db: Path, output_dir: Path):
    if not db.is_file():
        raise ValueError("The explicitly selected account database does not exist")
    with tempfile.TemporaryDirectory(prefix=".training-snapshot-", dir=output_dir) as temporary:
        snapshot = Path(temporary) / "account.sqlite3"
        with sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True) as source:
            with sqlite3.connect(snapshot) as target:
                source.backup(target)
        yield ReadOnlyStore(snapshot)


def code_fingerprint() -> str:
    # Include extraction, contracts and GOLD isolation, not just the fitted estimator.
    from discovery import cli, contracts, storage
    from discovery.evals import gold, gold_run, user_recommendations
    from discovery.ranking import baseline
    from discovery.training import pipeline, serving
    from discovery.web import cards

    modules = [Path(__file__), *(Path(module.__file__) for module in
                                (cli, contracts, storage, gold, gold_run, user_recommendations, baseline, cards, pipeline, serving))]
    return _hash({str(path.relative_to(Path(__file__).parents[2])):
                  hashlib.sha256(path.read_bytes()).hexdigest() for path in modules})


def _registry(output_dir: Path) -> dict:
    path = output_dir / REGISTRY
    return json.loads(path.read_text()) if path.exists() else {
        "schema_version": 1, "status": "experimental_only", "serving_enabled": False,
        "current": None, "previous": None, "completed_run_ids": [], "account_hash": None,
    }


def _artifact_path(output_dir: Path, run_id: str) -> Path:
    if len(run_id) != 64 or any(char not in "0123456789abcdef" for char in run_id):
        raise ValueError("Invalid experiment identifier")
    return output_dir / "runs" / run_id / "artifact.json"


def filter_gold(records: list[TrainingRecord], catalog: Catalog, snapshots: list[Catalog],
                manifest: dict) -> tuple[list[TrainingRecord], dict]:
    protected_releases = set(manifest["protected_release_ids"])
    protected_artists = set(manifest["protected_artist_ids"])
    for snapshot in [catalog, *snapshots]:
        protected_releases.update(release.id for release in snapshot.releases
                                  if protected_artists.intersection(release.artist_ids))
    catalog_ids = {release.id for release in catalog.releases}
    golden = [row.release_id for row in records if row.release_id in protected_releases]
    unknown = [row.release_id for row in records
               if row.release_id not in catalog_ids and row.release_id not in protected_releases]
    clean = [row for row in records
             if row.release_id not in protected_releases and row.release_id in catalog_ids]
    return clean, {"gold_excluded_release_ids": sorted(golden),
                   "missing_catalog_release_ids": sorted(unknown),
                   "protected_release_count": len(protected_releases),
                   "protected_artist_count": len(protected_artists),
                   "gold_labels_used_for_training": 0}


def temporal_partitions(records: list[TrainingRecord], artifact: dict) -> dict:
    """Reconstruct and audit the existing pipeline's frozen 60/20/20 partition."""
    available = sorted((row for row in records
                        if timestamp(row.available_at) <= timestamp(artifact["cutoff"])),
                       key=lambda row: (timestamp(row.decision_at), row.release_id))
    n = len(available)
    train_end, valid_end = max(1, int(n * .6)), max(2, int(n * .8))
    train_cutoff = timestamp(available[train_end].decision_at)
    validation_cutoff = timestamp(available[min(valid_end, n - 1)].decision_at)
    eligible = set(artifact["trained_genre_ids"])
    training = [row for row in available[:train_end]
                if timestamp(row.available_at) <= train_cutoff and row.genre_scope_id in eligible]
    validation = [row for row in available[train_end:valid_end]
                  if timestamp(row.available_at) <= validation_cutoff]
    test = available[valid_end:]
    ids = [{row.release_id for row in partition} for partition in (training, validation, test)]
    if ids[0] != set(artifact["training_release_ids"]):
        raise ValueError("Training partition differs from fitted artifact")
    if ids[0] & ids[1] or ids[0] & ids[2] or ids[1] & ids[2]:
        raise ValueError("Temporal partitions overlap")
    if any(timestamp(row.available_at) > train_cutoff for row in training):
        raise ValueError("Future labels entered training")
    def before(left, right, field):
        return (bool(left) and bool(right)
                and max(timestamp(getattr(row, field)) for row in left)
                < min(timestamp(row.decision_at) for row in right))

    temporal_checks = {
        "train_decisions_before_validation": before(training, validation, "decision_at"),
        "train_labels_before_validation": before(training, validation, "available_at"),
        "validation_decisions_before_test": before(validation, test, "decision_at"),
        "validation_labels_before_test": before(validation, test, "available_at"),
    }
    strictly_temporal = all(temporal_checks.values())
    return {"train": training, "validation": validation, "test": test,
            "train_boundary": train_cutoff.isoformat(),
            "validation_boundary": validation_cutoff.isoformat(),
            "strictly_temporal": strictly_temporal, "temporal_checks": temporal_checks}


def _metrics(rows: list[TrainingRecord], probabilities: list[float]) -> dict:
    from math import isfinite, log

    if not rows or len(rows) != len(probabilities):
        raise ValueError("Metric rows and predictions must have equal nonzero lengths")
    if any(not isinstance(value, (int, float)) or not isfinite(value) or not 0 <= value <= 1
           for value in probabilities):
        raise ValueError("Predictions must be finite probabilities in [0, 1]")
    bounded = [min(1 - 1e-12, max(1e-12, probability)) for probability in probabilities]
    return {"n": len(rows), "positive": sum(row.label for row in rows),
            "negative": sum(1 - row.label for row in rows),
            "log_loss": -sum(row.label * log(p) + (1 - row.label) * log(1 - p)
                             for row, p in zip(rows, bounded)) / len(rows),
            "brier": sum((row.label - p) ** 2 for row, p in zip(rows, bounded)) / len(rows)}


def evaluate_candidate(artifact: dict, records: list[TrainingRecord], catalog: Catalog,
                       previous: dict | None, config: CycleConfig) -> dict:
    split = temporal_partitions(records, artifact)
    test_rows = split["test"]
    previous_training = set(previous.get("training_release_ids", [])) if previous else set()
    # Previous model must never have trained on this comparison. A fresh horizon
    # also prevents repeatedly tuning promotion against the same old holdout.
    fresh = [row for row in test_rows if row.release_id not in previous_training
             and (previous is None or timestamp(row.decision_at) > timestamp(previous["cutoff"]))]
    release_map = {release.id: release for release in catalog.releases}
    metrics, reasons = {}, []
    if not split["strictly_temporal"]:
        reasons.append("temporal_boundary_tie_or_empty_partition")
    evidence = {"artifact_hash": artifact_hash(artifact), "dataset_hash": artifact["dataset_hash"],
                "is_fixture": artifact["is_fixture"], "protocol": "frozen_temporal_holdout",
                "decision": "promote", "genres": {}}
    for genre in artifact["trained_genre_ids"]:
        rows = [row for row in fresh if row.genre_scope_id == genre]
        training = [row for row in split["train"] if row.genre_scope_id == genre]
        if not rows:
            reasons.append(f"no_fresh_holdout:{genre}")
            metrics[genre] = {"status": "insufficient_data", "n": 0}
            continue
        prior = sum(row.label for row in training) / len(training)
        predictions = [predict(artifact, row, allow_experimental=True) for row in rows]
        model = _metrics(rows, predictions)
        baseline = _metrics(rows, [prior] * len(rows))
        allowed = catalog.scope_ids(genre)
        violations = sum(not allowed.intersection(release_map[row.release_id].genre_ids) for row in rows) if genre != "all" else 0
        report = {"model": model, "training_prevalence_baseline": baseline,
                  "genre_violations": violations,
                  "release_ids": [row.release_id for row in rows],
                  "holdout_snapshot_id": _hash([asdict(row) for row in rows])}
        if previous is not None:
            previous_predictions = [predict(previous, row, allow_experimental=True) for row in rows]
            if any(value is None for value in previous_predictions):
                report["previous"] = {"status": "untrained_genre"}
                reasons.append(f"previous_untrained_genre:{genre}")
            elif (previous["taxonomy_version"] != artifact["taxonomy_version"]
                  or previous["feature_version"] != artifact["feature_version"]):
                report["previous"] = {"status": "incompatible_features"}
                reasons.append(f"previous_incompatible_features:{genre}")
            else:
                earlier = _metrics(rows, previous_predictions)
                report["previous"] = earlier
                if model["log_loss"] >= earlier["log_loss"] or model["brier"] > earlier["brier"]:
                    reasons.append(f"no_improvement_over_previous:{genre}")
        metrics[genre] = report
        evidence["genres"][genre] = {
            "n": model["n"], "positive": model["positive"], "negative": model["negative"],
            "holdout_snapshot_id": report["holdout_snapshot_id"], "genre_violations": violations,
            "release_ids": report["release_ids"],
            "model_log_loss": model["log_loss"], "model_brier": model["brier"],
            "baseline_log_loss": baseline["log_loss"], "baseline_brier": baseline["brier"],
        }
    try:
        promote_artifact(artifact, evidence, minimum_holdout=config.minimum_holdout,
                         minimum_class=config.minimum_holdout_class, previous_artifact=previous)
    except ValueError as error:
        reasons.append(str(error))
    if previous and not set(previous["trained_genre_ids"]) <= set(evidence["genres"]):
        reasons.append("not_all_previous_scopes_evaluated")
    return {
        "protocol": "frozen_temporal_holdout", "statistical_gate": "blocked" if reasons else "passed",
        "reasons": reasons, "per_genre": metrics, "evidence": evidence,
        "split": {key: [row.release_id for row in split[key]] for key in ("train", "validation", "test")},
        "train_boundary": split["train_boundary"], "validation_boundary": split["validation_boundary"],
        "temporal_checks": split["temporal_checks"],
        "fresh_holdout_count": len(fresh), "previous_training_overlap_excluded": len(
            previous_training.intersection(row.release_id for row in test_rows)),
        "ranking_baseline": "not_measured_probability_baseline_only",
        "gold_quality": "needs_audio_or_independent_features_and_labels",
        "production_deployment": "blocked", "serving_enabled": False,
    }


def run_once(db: Path, catalog_path: Path, output_dir: Path, *, cutoff: str | None = None,
             config: CycleConfig | None = None, sound_path: Path | None = None,
             cases_path: Path | None = None) -> dict:
    output_dir, db, catalog_path = Path(output_dir), Path(db), Path(catalog_path)
    config = config or CycleConfig()
    with cycle_lock(output_dir) as acquired:
        if not acquired:
            return {"status": "busy", "serving_enabled": False}
        try:
            bound = _registry(output_dir).get("account_hash")
            account_hash = hashlib.sha256(str(db.resolve()).encode()).hexdigest()
            if bound not in (None, account_hash):
                return {"status": "error", "error_type": "AccountMismatch", "serving_enabled": False}
            return _run_locked(db, catalog_path, output_dir, cutoff, config, sound_path, cases_path)
        except Exception as error:
            # Do not expose SQLite contents, provider secrets, or other account paths.
            result = {"status": "error", "error_type": type(error).__name__,
                      "reason": "Training cycle failed; previous experimental artifact preserved.",
                      "serving_enabled": False}
            atomic_json(output_dir / "latest.json", result)
            return result


def _run_locked(db: Path, catalog_path: Path, output_dir: Path, cutoff: str | None,
                config: CycleConfig, sound_path: Path | None, cases_path: Path | None) -> dict:
    from discovery.evals.gold import CASES_PATH, SOUND_PATH, build_gold_manifest
    from discovery.evals.gold_run import INPUTS_PATH, evaluate_gold

    registry = _registry(output_dir)
    account_hash = hashlib.sha256(str(db.resolve()).encode()).hexdigest()
    if registry.get("account_hash") not in (None, account_hash):
        raise ValueError("Output directory is already bound to another account")
    if registry.get("account_hash") is None:
        registry["account_hash"] = account_hash
        # Bind before materializing any snapshot or immutable run, including an
        # interrupted first attempt whose artifact has not yet been published.
        atomic_json(output_dir / REGISTRY, registry)
    catalog = load_catalog(catalog_path)
    manifest = build_gold_manifest(catalog, sound_path=sound_path, cases_path=cases_path)
    frozen_inputs = json.loads(INPUTS_PATH.read_text())
    evaluation_as_of = config.evaluation_as_of or frozen_inputs.get("as_of")
    boundary = timestamp(cutoff) if cutoff else datetime.now(UTC)
    if cutoff and datetime.fromisoformat(cutoff.replace("Z", "+00:00")).tzinfo is None:
        raise ValueError("Cutoff must include timezone")
    with snapshot_store(db, output_dir) as snapshot:
        raw = training_records(snapshot, cutoff=boundary)
        records = [TrainingRecord(**row) for row in raw]
        decisions = snapshot.decisions()
        snapshots = [snapshot.get_catalog(key) for key in sorted({d.catalog_hash for d in decisions})]
        # Provenance is checked before GOLD removal so fixtures cannot disappear silently.
        fixture = catalog.is_fixture or any(row.is_fixture for row in records)
        historical_manifests = [build_gold_manifest(item, sound_path=sound_path, cases_path=cases_path)
                                for item in snapshots]
        isolation_manifest = dict(manifest)
        for field in ("protected_release_ids", "protected_artist_ids"):
            isolation_manifest[field] = sorted(set(manifest[field]).union(
                *(set(item[field]) for item in historical_manifests)))
        records, exclusions = filter_gold(records, catalog, snapshots, isolation_manifest)
    # New live decisions may coexist with immutable historical feature versions.
    # Select the configured version explicitly and report every excluded row;
    # never relabel historical numerical features as the current representation.
    exclusions["feature_version_counts"] = dict(sorted(Counter(row.feature_version for row in records).items()))
    excluded_versions = [row for row in records if row.feature_version != config.feature_version]
    exclusions["feature_version_excluded_rows"] = len(excluded_versions)
    exclusions["feature_version_excluded_release_ids"] = sorted({row.release_id for row in excluded_versions})
    records = [row for row in records if row.feature_version == config.feature_version]
    records.sort(key=lambda row: (timestamp(row.decision_at), row.release_id))
    effective_cutoff = cutoff or max((row.available_at for row in records),
                                    key=timestamp, default="1970-01-01T00:00:00+00:00")
    dataset = [asdict(row) for row in records]
    provenance = {"data_hash": _hash(raw), "eligible_data_hash": _hash(dataset),
                  "catalog_hash": catalog_digest(catalog), "gold_hash": manifest["digest"],
                  "gold_manifest_hash": _hash(manifest),
                  "historical_gold_manifest_hash": _hash(historical_manifests),
                  "isolation_manifest_hash": _hash(isolation_manifest), "code_hash": code_fingerprint(),
                  "config_hash": _hash(asdict(config)), "cutoff": effective_cutoff,
                  "evaluation_as_of": evaluation_as_of,
                  "dependency_versions": {name: importlib.metadata.version(name)
                                          for name in ("scikit-learn", "numpy", "scipy")},
                  "account_hash": account_hash,
                  "gold_inputs_hash": hashlib.sha256(INPUTS_PATH.read_bytes()).hexdigest()}
    run_id = _hash(provenance)
    report_path = output_dir / "runs" / run_id / "report.json"
    if report_path.exists():
        report = json.loads(report_path.read_text())
        _publish(output_dir, registry, report)
        return {**report, "skipped": True}
    previous = load_artifact(_artifact_path(output_dir, registry["current"])) if registry["current"] else None
    result = {"schema_version": 1, "run_id": run_id, "skipped": False,
              "provenance": provenance, "exclusions": exclusions,
              "raw_label_count": len(raw), "eligible_label_count": len(records),
              "gold": {"digest": manifest["digest"],
                       "sound_release_count": manifest["sound_release_count"],
                       "recommendation_case_count": manifest["recommendation_case_count"],
                       "status": "reserved_for_evaluation_not_training"},
              "previous_run_id": registry["current"], "serving_enabled": False,
              "production_deployment": "blocked", "artifact_path": None}
    if fixture:
        result.update(status="fixture_rejected", reasons=["Fixture catalog or feedback cannot train a personal model."])
    else:
        report = train(records, cutoff=effective_cutoff, minimum_labels=config.minimum_labels,
                       minimum_minority=config.minimum_minority, seed=config.seed,
                       taxonomy_version=catalog.taxonomy_version, feature_version=config.feature_version)
        result.update(status=report.status, training=report.to_dict())
        if report.artifact is not None:
            evaluation = evaluate_candidate(report.artifact, records, catalog, previous, config)
            result["evaluation"] = evaluation
            # Statistical eligibility is not GOLD validation. Keep serving disabled.
            artifact = report.artifact
            assert artifact["validated_genre_ids"] == []
            result["artifact_path"] = str(Path("runs") / run_id / "artifact.json")
            atomic_json(_artifact_path(output_dir, run_id), artifact)
    candidate = result.get("training", {}).get("artifact")
    try:
        result["gold_report"] = evaluate_gold(catalog, candidate, cases_path=cases_path or CASES_PATH,
                                             sound_path=sound_path or SOUND_PATH,
                                             as_of=date.fromisoformat(evaluation_as_of))
    except (OSError, ValueError, KeyError, TypeError) as error:
        result["gold_report"] = {"status": "unavailable", "passed": False,
                                 "error_type": type(error).__name__,
                                 "reason": "GOLD cases require matching frozen confirmed inputs.",
                                 "serving_enabled": False}
    result["candidate_artifact_hash"] = artifact_hash(candidate) if candidate else None
    run_dir = report_path.parent
    atomic_json(run_dir / "eligible-records.json", dataset)
    atomic_json(run_dir / "gold-manifest.json", manifest)
    atomic_json(run_dir / "historical-gold-manifests.json", historical_manifests)
    atomic_json(report_path, result)
    _publish(output_dir, registry, result)
    return result


def _gate_passed(report: dict) -> bool:
    return (report.get("evaluation") or {}).get("statistical_gate") == "passed"


def _run_passed_gate(output_dir: Path, run_id: str) -> bool:
    path = _artifact_path(output_dir, run_id).with_name("report.json")
    try:
        return _gate_passed(json.loads(path.read_text()))
    except (OSError, ValueError):
        return False


def _publish(output_dir: Path, registry: dict, report: dict) -> None:
    # The complete immutable run is written first. If a process stops before this
    # single atomic pointer commit, replay publishes it without fitting twice.
    run_id = report["run_id"]
    completed = registry.setdefault("completed_run_ids", [])
    if run_id not in completed:
        if report["artifact_path"]:
            load_artifact(_artifact_path(output_dir, run_id))
            if _gate_passed(report):
                registry.update(previous=registry["current"], current=run_id)
            else:
                # A blocked run is kept for inspection but never becomes current.
                registry.setdefault("candidate_run_ids", []).append(run_id)
        completed.append(run_id)
        atomic_json(output_dir / REGISTRY, registry)
    atomic_json(output_dir / "latest.json", report)


def rollback(output_dir: Path, *, db: Path) -> dict:
    output_dir = Path(output_dir)
    with cycle_lock(output_dir) as acquired:
        if not acquired:
            return {"status": "busy", "serving_enabled": False}
        registry = _registry(output_dir)
        if registry.get("account_hash") not in (None, hashlib.sha256(str(Path(db).resolve()).encode()).hexdigest()):
            return {"status": "error", "error_type": "AccountMismatch", "serving_enabled": False}
        if not registry["previous"]:
            return {"status": "no_previous_artifact", "serving_enabled": False}
        if not _run_passed_gate(output_dir, registry["previous"]):
            return {"status": "no_previous_artifact", "serving_enabled": False,
                    "reason": "previous_run_did_not_pass_gate"}
        load_artifact(_artifact_path(output_dir, registry["previous"]))
        registry["current"], registry["previous"] = registry["previous"], registry["current"]
        atomic_json(output_dir / REGISTRY, registry)
        return {**registry, "status": "rolled_back"}


def main() -> None:
    parser = argparse.ArgumentParser(description="Локальное экспериментальное обучение одного аккаунта")
    parser.add_argument("--db", type=Path, required=True, help="Явно выбранная база одного аккаунта")
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--watch", action="store_true")
    mode.add_argument("--rollback", action="store_true")
    parser.add_argument("--interval", type=int, default=300)
    parser.add_argument("--cutoff")
    parser.add_argument("--sound-gold", type=Path)
    parser.add_argument("--cases-gold", type=Path)
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--evaluation-as-of", type=date.fromisoformat)
    args = parser.parse_args()
    if args.interval < 60:
        parser.error("--interval должен быть не меньше 60 секунд")
    if args.rollback:
        print(json.dumps(rollback(args.output_dir, db=args.db), ensure_ascii=False))
        return
    try:
        while True:
            result = run_once(args.db, args.catalog, args.output_dir, cutoff=args.cutoff,
                              config=CycleConfig(seed=args.seed, evaluation_as_of=(args.evaluation_as_of.isoformat()
                                  if args.evaluation_as_of else None)), sound_path=args.sound_gold,
                              cases_path=args.cases_gold)
            print(json.dumps({key: result.get(key) for key in
                              ("status", "run_id", "skipped", "eligible_label_count", "serving_enabled")},
                             ensure_ascii=False), flush=True)
            if not args.watch:
                if result["status"] == "error":
                    raise SystemExit(1)
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
