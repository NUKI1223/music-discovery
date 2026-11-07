"""Synthetic contract tests only: no real account data or production artifact writes."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from discovery.catalog.io import save_catalog
from discovery.contracts import (
    Artist,
    Catalog,
    Decision,
    Feedback,
    Genre,
    RankingResult,
    RecommendationItem,
    Release,
    RequestIntent,
    UserProfile,
)
from discovery.evals.gold import build_gold_manifest
from discovery.ranking.baseline import FEATURE_VERSION
from discovery.storage import Store, catalog_digest
from discovery.training import autotrain
from discovery.training.autotrain import (
    REGISTRY,
    CycleConfig,
    cycle_lock,
    evaluate_candidate,
    filter_gold,
    rollback,
    run_once,
    temporal_partitions,
)
from discovery.training.pipeline import TrainingRecord, train


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    monkeypatch.setattr(autotrain, "code_fingerprint", lambda: "stable-test-code")
    # Non-fixture flags exercise personal-data boundaries using fabricated tests,
    # and every fitted artifact remains inside pytest's temporary directory.
    catalog = Catalog(snapshot_id="autotrain-contract-test",
                      genres=[Genre(id="hip-hop", name="Hip-hop")],
                      artists=[Artist(id=f"artist-{i}", name=f"Performer {i}") for i in range(200)],
                      releases=[Release(id=f"release-{i}", title=f"Album {i}",
                                        artist_ids=[f"artist-{i}"], genre_ids=["hip-hop"])
                                for i in range(200)])
    catalog_path = tmp_path / "catalog.json"
    save_catalog(catalog, catalog_path)
    sound = tmp_path / "sound.json"
    sound.write_text(json.dumps({"releases": [{"id": "aoty:99999", "artist": "Reserved artist",
        "aoty_url": "https://www.albumoftheyear.org/album/99999-reserved.php",
        "annotation_status": "awaiting_user", "normalized_sound": None}]}))
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps({"cases": [{"id": "case", "request": "Find music"}]}))
    store = Store(tmp_path / "account.sqlite3")
    return {"db": store.path, "catalog_path": catalog_path, "catalog": catalog,
            "output_dir": tmp_path / "training", "sound_path": sound, "cases_path": cases}


def add_rows(inputs, n=100, start=0, feature_version=FEATURE_VERSION):
    store, catalog = Store(inputs["db"]), inputs["catalog"]
    for index in range(start, start + n):
        date = datetime(2024, 1, 1, tzinfo=UTC) + timedelta(days=index)
        label = index % 2
        request = RequestIntent(genre_scope_id=catalog.releases[index].genre_ids[0])
        decision = Decision(id=f"decision-{index}", request=request, created_at=date, feature_version=feature_version,
                            catalog_hash=catalog_digest(catalog), catalog_snapshot_id=catalog.snapshot_id,
                            taxonomy_version=catalog.taxonomy_version, profile=UserProfile(),
                            result=RankingResult(items=[RecommendationItem(
                                release_id=f"release-{index}", score=.5,
                                components={"similarity": float(label), "mode": "explore"})]))
        store.commit_decision(decision.id, catalog, request, lambda: decision)
        store.append_feedback(Feedback(id=f"feedback-{index}", decision_id=decision.id,
                                       release_id=f"release-{index}",
                                       kind="listened_like" if label else "listened_dislike",
                                       occurred_at=date + timedelta(hours=1),
                                       recorded_at=date + timedelta(hours=1)))


def run(inputs, **options):
    return run_once(**{key: value for key, value in inputs.items() if key != "catalog"}, **options)


def registry(inputs):
    return json.loads((inputs["output_dir"] / REGISTRY).read_text())


def test_real_cycle_reproducible_idempotent_readonly_and_experimental(inputs, monkeypatch):
    add_rows(inputs)
    before = inputs["db"].read_bytes()
    result = run(inputs)
    assert result["status"] == "experimental", result
    assert result["evaluation"]["statistical_gate"] == "passed"
    assert result["evaluation"]["production_deployment"] == "blocked"
    assert result["exclusions"]["gold_labels_used_for_training"] == 0
    artifact_path = inputs["output_dir"] / result["artifact_path"]
    artifact = json.loads(artifact_path.read_text())
    assert artifact["validated_genre_ids"] == []
    assert artifact["is_fixture"] is False
    assert inputs["db"].read_bytes() == before
    assert not list(inputs["output_dir"].glob(".training-snapshot-*"))
    repeated = run(inputs)
    assert repeated["run_id"] == result["run_id"] and repeated["skipped"] is True
    assert len(list((inputs["output_dir"] / "runs").iterdir())) == 1
    second = {**inputs, "output_dir": inputs["output_dir"].with_name("repeat-training")}
    repeat = run(second)
    assert repeat["run_id"] == result["run_id"]
    assert json.loads((second["output_dir"] / repeat["artifact_path"]).read_text()) == artifact
    monkeypatch.setattr(autotrain, "train", lambda *args, **kwargs: pytest.fail("duplicate fit"))
    assert run(inputs)["skipped"] is True


def test_changed_data_config_catalog_gold_and_code_create_new_runs(inputs, monkeypatch):
    add_rows(inputs)
    ids = {run(inputs)["run_id"]}
    add_rows(inputs, n=2, start=100)
    ids.add(run(inputs)["run_id"])
    ids.add(run(inputs, config=CycleConfig(seed=19))["run_id"])
    cases = json.loads(inputs["cases_path"].read_text())
    cases["cases"].append({"id": "second", "request": "Find different music"})
    inputs["cases_path"].write_text(json.dumps(cases))
    ids.add(run(inputs)["run_id"])
    catalog = inputs["catalog"].model_copy(update={"snapshot_id": "updated-catalog"})
    save_catalog(catalog, inputs["catalog_path"])
    ids.add(run(inputs)["run_id"])
    monkeypatch.setattr(autotrain, "code_fingerprint", lambda: "changed-code")
    ids.add(run(inputs)["run_id"])
    assert len(ids) == 6


def test_insufficient_and_error_preserve_previous_and_rollback(inputs, monkeypatch):
    add_rows(inputs)
    first = run(inputs)
    add_rows(inputs, n=100, start=100)
    second = run(inputs)
    assert second["evaluation"]["statistical_gate"] == "passed", second["evaluation"]["reasons"]
    assert registry(inputs)["previous"] == first["run_id"]
    state = registry(inputs)
    insufficient = run(inputs, config=CycleConfig(minimum_labels=1000))
    assert insufficient["status"] == "insufficient_data"
    assert insufficient["artifact_path"] is None
    assert registry(inputs)["current"] == state["current"]
    assert registry(inputs)["previous"] == state["previous"]
    monkeypatch.setattr(autotrain, "train", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("private")))
    failed = run(inputs, config=CycleConfig(seed=23))
    assert failed["status"] == "error" and "private" not in json.dumps(failed)
    assert registry(inputs)["current"] == second["run_id"]
    assert rollback(inputs["output_dir"], db=inputs["db"])["current"] == first["run_id"]
    # A cache replay must not undo an intentional rollback.
    assert run(inputs)["skipped"] is True
    assert registry(inputs)["current"] == first["run_id"]


def test_blocked_run_is_candidate_only_and_rollback_skips_it(inputs):
    add_rows(inputs)
    first = run(inputs)
    assert first["evaluation"]["statistical_gate"] == "passed"
    # Same data with another seed has no fresh holdout, so the gate blocks it.
    blocked = run(inputs, config=CycleConfig(seed=18))
    assert blocked["evaluation"]["statistical_gate"] == "blocked"
    assert blocked["artifact_path"] and (inputs["output_dir"] / blocked["artifact_path"]).exists()
    state = registry(inputs)
    assert state["current"] == first["run_id"] and state["previous"] is None
    assert blocked["run_id"] in state["candidate_run_ids"]
    assert blocked["run_id"] in state["completed_run_ids"]
    # Replaying the cached blocked run must not promote it either.
    assert run(inputs, config=CycleConfig(seed=18))["skipped"] is True
    assert registry(inputs)["current"] == first["run_id"]
    assert rollback(inputs["output_dir"], db=inputs["db"])["status"] == "no_previous_artifact"


def test_rollback_refuses_previous_that_never_passed_gate(inputs):
    add_rows(inputs)
    first = run(inputs)
    blocked = run(inputs, config=CycleConfig(seed=18))
    state = registry(inputs)
    # Simulate a legacy/tampered registry pointing back at a blocked run.
    state.update(current=first["run_id"], previous=blocked["run_id"])
    (inputs["output_dir"] / REGISTRY).write_text(json.dumps(state))
    result = rollback(inputs["output_dir"], db=inputs["db"])
    assert result["status"] == "no_previous_artifact"
    assert registry(inputs)["current"] == first["run_id"]


def test_fixture_rejected_even_if_golden_rows_filtered(inputs):
    inputs["catalog"] = inputs["catalog"].model_copy(update={"is_fixture": True})
    save_catalog(inputs["catalog"], inputs["catalog_path"])
    add_rows(inputs)
    result = run(inputs)
    assert result["status"] == "fixture_rejected"
    assert result["artifact_path"] is None
    assert registry(inputs)["current"] is None


def test_lock_and_output_account_binding(inputs):
    add_rows(inputs)
    first = run(inputs)
    with cycle_lock(inputs["output_dir"]) as acquired:
        assert acquired
        assert run(inputs)["status"] == "busy"
    other = inputs["db"].with_name("other-account.sqlite3")
    Store(other)
    latest = (inputs["output_dir"] / "latest.json").read_bytes()
    state = (inputs["output_dir"] / REGISTRY).read_bytes()
    assert run({**inputs, "db": other})["status"] == "error"
    assert rollback(inputs["output_dir"], db=other)["status"] == "error"
    assert (inputs["output_dir"] / "latest.json").read_bytes() == latest
    assert (inputs["output_dir"] / REGISTRY).read_bytes() == state
    assert registry(inputs)["current"] == first["run_id"]


def test_cutoff_does_not_include_future_labels_and_splits_are_disjoint(inputs):
    add_rows(inputs)
    limited = run(inputs, cutoff="2024-01-31T23:59:59+00:00")
    assert limited["status"] == "insufficient_data" and limited["eligible_label_count"] == 31
    full = run(inputs)
    split = full["evaluation"]["split"]
    assert set(split["train"]).isdisjoint(split["validation"])
    assert set(split["train"]).isdisjoint(split["test"])
    assert set(split["validation"]).isdisjoint(split["test"])
    assert all(full["evaluation"]["temporal_checks"].values())
    assert run(inputs, cutoff="2024-01-31")["status"] == "error"


def test_previous_model_compared_only_on_fresh_unseen_holdout(inputs):
    add_rows(inputs)
    first = run(inputs)
    changed = run(inputs, config=CycleConfig(seed=18))
    assert changed["evaluation"]["statistical_gate"] == "blocked"
    assert changed["evaluation"]["fresh_holdout_count"] == 0
    assert changed["previous_run_id"] == first["run_id"]
    add_rows(inputs, n=30, start=100)
    newer = run(inputs)
    assert newer["evaluation"]["fresh_holdout_count"] == 26
    assert newer["evaluation"]["per_genre"]["hip-hop"]["previous"]["n"] == 26


def test_gold_protects_related_releases_and_historical_identity(inputs):
    # The artist was named as GOLD under an older ID; today's snapshot changed
    # both ID and name. Historical name resolution must still quarantine feedback.
    catalog = inputs["catalog"]
    artists = list(catalog.artists)
    artists[0] = Artist(id="old-reserved", name="Reserved artist")
    releases = list(catalog.releases)
    releases[0] = releases[0].model_copy(update={"artist_ids": ["old-reserved"]})
    historical = catalog.model_copy(update={"artists": artists, "releases": releases})
    inputs["catalog"] = historical
    add_rows(inputs)
    artists = list(artists)
    releases = list(releases)
    artists[0] = Artist(id="new-unrecognized", name="Changed name")
    releases[0] = releases[0].model_copy(update={"artist_ids": ["new-unrecognized"]})
    current = catalog.model_copy(update={"artists": artists, "releases": releases})
    save_catalog(current, inputs["catalog_path"])
    result = run(inputs)
    assert result["eligible_label_count"] == 99
    assert result["exclusions"]["gold_excluded_release_ids"] == ["release-0"]
    rows = json.loads((inputs["output_dir"] / "runs" / result["run_id"] / "eligible-records.json").read_text())
    assert "release-0" not in {row["release_id"] for row in rows}
    manifest = build_gold_manifest(historical, inputs["sound_path"], inputs["cases_path"])
    # Artist protection excludes releases other than the named gold album too.
    row = TrainingRecord("release-0", "hip-hop", "2024-01-01", "2024-01-02", 1,
                         {"similarity": .1}, "snapshot")
    clean, audit = filter_gold([row], historical, [], manifest)
    assert clean == [] and audit["gold_excluded_release_ids"] == ["release-0"]


def test_temporal_ties_and_fake_overlap_do_not_pass_gates(inputs):
    add_rows(inputs)
    with autotrain.snapshot_store(inputs["db"], inputs["db"].parent) as store:
        records = [TrainingRecord(**row) for row in autotrain.training_records(store)]
    records[59] = replace(records[59], available_at=records[59].decision_at)
    records[60] = replace(records[60], decision_at=records[59].decision_at)
    fitted = train(records, cutoff="2025-01-01T00:00:00+00:00", feature_version=FEATURE_VERSION)
    evaluation = evaluate_candidate(fitted.artifact, records, inputs["catalog"], None, CycleConfig())
    assert evaluation["statistical_gate"] == "blocked"
    assert "temporal_boundary_tie_or_empty_partition" in evaluation["reasons"]
    changed = dict(fitted.artifact, training_release_ids=[*fitted.artifact["training_release_ids"], "release-99"])
    with pytest.raises(ValueError, match="partition differs"):
        temporal_partitions(records, changed)


def test_cycle_reports_and_separates_legacy_feature_history(inputs):
    add_rows(inputs, n=10, feature_version="genre-tags-v1")
    add_rows(inputs, n=100, start=10)
    before = inputs["db"].read_bytes()
    report = run(inputs)
    assert report["status"] == "experimental"
    assert report["eligible_label_count"] == 100
    assert report["exclusions"]["feature_version_excluded_rows"] == 10
    assert report["exclusions"]["feature_version_counts"] == {"genre-tags-v1": 10, FEATURE_VERSION: 100}
    artifact = json.loads((inputs["output_dir"] / report["artifact_path"]).read_text())
    assert artifact["feature_version"] == FEATURE_VERSION
    assert not {f"release-{i}" for i in range(10)} & set(artifact["training_release_ids"])
    assert inputs["db"].read_bytes() == before


def test_interrupted_publication_recovers_without_retraining(inputs, monkeypatch):
    add_rows(inputs)
    original = autotrain._publish
    monkeypatch.setattr(autotrain, "_publish", lambda *args: (_ for _ in ()).throw(RuntimeError("stop")))
    assert run(inputs)["status"] == "error"
    assert registry(inputs)["current"] is None
    other = inputs["db"].with_name("different.sqlite3")
    Store(other)
    assert run({**inputs, "db": other})["error_type"] == "AccountMismatch"
    monkeypatch.setattr(autotrain, "_publish", original)
    monkeypatch.setattr(autotrain, "train", lambda *args, **kwargs: pytest.fail("should resume publication"))
    resumed = run(inputs)
    assert resumed["status"] == "experimental" and resumed["skipped"] is True
    assert registry(inputs)["current"] == resumed["run_id"]


@pytest.mark.parametrize("values", [[float("nan")], [float("inf")], [-.1], [1.1], [], [.2, .3]])
def test_metrics_fail_closed_for_invalid_predictions(values):
    row = TrainingRecord("r", "hip-hop", "2024-01-01", "2024-01-02", 1,
                         {"similarity": .1}, "snapshot")
    with pytest.raises(ValueError):
        autotrain._metrics([row], values)


def test_unrelated_genres_train_and_evaluate_without_hiphop_assumptions(inputs):
    catalog = inputs["catalog"]
    genres = [Genre(id="jazz", name="Jazz"), Genre(id="metal", name="Metal")]
    releases = [release.model_copy(update={"genre_ids": [genres[(i // 2) % 2].id]})
                for i, release in enumerate(catalog.releases)]
    inputs["catalog"] = catalog.model_copy(update={"genres": genres, "releases": releases})
    save_catalog(inputs["catalog"], inputs["catalog_path"])
    add_rows(inputs, n=200)
    result = run(inputs)
    assert result["status"] == "experimental"
    assert result["training"]["artifact"]["trained_genre_ids"] == ["jazz", "metal"]
    assert set(result["evaluation"]["per_genre"]) == {"jazz", "metal"}
    assert result["evaluation"]["statistical_gate"] == "passed"
    assert result["training"]["artifact"]["validated_genre_ids"] == []
    assert result["production_deployment"] == "blocked"
