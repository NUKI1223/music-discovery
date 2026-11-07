import json
from copy import deepcopy
from datetime import date

import pytest

from discovery.contracts import Artist, Catalog, Genre, Release, RequestIntent, UserProfile
from discovery.evals.gold import build_gold_manifest, sound_readiness
from discovery.evals.gold_run import evaluate_gold
from discovery.ranking import rank
from discovery.ranking.baseline import FEATURE_VERSION


@pytest.fixture
def gold_files(tmp_path):
    sound = {"releases": [
        {"id": "aoty:123", "aoty_url": "https://www.albumoftheyear.org/album/123-gold.php",
         "artist": "Gold Artist", "title": "Gold", "annotation_status": "annotated",
         "normalized_sound": {"overall": "SECRET SOUND"}, "preference_label": None},
        {"id": "aoty:456", "aoty_url": "https://www.albumoftheyear.org/album/456-unknown.php",
         "artist": "Unknown Artist", "title": "Unknown", "annotation_status": "not_listened",
         "normalized_sound": None, "preference_label": None},
    ]}
    cases = {"cases": [{"id": "test", "request": "Something like Anchor", "context": "",
        "listener_artist_names": ["Listener"], "positive_artists": ["Gold Artist"],
        "negative_artists": ["Avoid"], "positive_rationale": "SECRET WORD",
        "expectations": {"count": 5, "genre_scope_id": "hip-hop", "required_release_genre_id": "rage"}}],
        "artist_references": [], "artist_source_policy": {"required_any_of": ["aoty", "rym"]}}
    inputs = {"cases": [{"id": "test", "confirmed_in_source": True,
                        "filters": {"locked_subgenre_ids": ["rage"]}}]}
    paths = [tmp_path / name for name in ("sound.json", "cases.json", "inputs.json")]
    for path, data in zip(paths, (sound, cases, inputs)):
        path.write_text(json.dumps(data))
    return paths


def catalog():
    return Catalog(snapshot_id="fixture", is_fixture=True,
        genres=[Genre(id="hip-hop", name="Hip Hop"),
                Genre(id="rage", name="Rage", parent_ids=["hip-hop"])],
        artists=[Artist(id=str(i), name=name, genre_ids=["rage"]) for i, name in enumerate(
            ["Gold Artist", "Cheese", "Listener", "Anchor", "Avoid", "Other"])],
        releases=[Release(id=f"r{i}", title=f"Release {i}", artist_ids=[str(i)],
                          genre_ids=["rage"]) for i in range(6)])


def model():
    return {"schema_version": 1, "kind": "logistic_regression", "taxonomy_version": "1",
            "feature_version": FEATURE_VERSION, "dataset_hash": "fixture",
            "trained_genre_ids": ["hip-hop"], "validated_genre_ids": [],
            "is_fixture": True, "coefficients": {"similarity": 2.0}, "intercept": -1.0}


def test_protect_artist_other_albums_collaborations_and_source_id(gold_files):
    cat = catalog()
    cat.releases.append(Release(id="collab", title="Another album", artist_ids=["0", "5"], genre_ids=["rage"]))
    cat.releases[5].links = {"AOTY": "https://www.albumoftheyear.org/album/123-other-spelling.php"}
    manifest = build_gold_manifest(cat, *gold_files[:2])
    assert set(manifest["protected_release_ids"]) >= {"r0", "r2", "r3", "r4", "r5", "collab", "aoty:456"}
    assert "r1" not in manifest["protected_release_ids"]
    assert manifest["sound_release_count"] == 1
    assert manifest["pending_sound_release_count"] == 1
    assert manifest == build_gold_manifest(cat, *gold_files[:2])
    text = json.dumps(manifest)
    assert "SECRET SOUND" not in text and "SECRET WORD" not in text


def test_changed_annotation_changes_digest_and_unknown_is_not_label(gold_files):
    before = build_gold_manifest(catalog(), *gold_files[:2])
    sound = json.loads(gold_files[0].read_text())
    sound["releases"][0]["normalized_sound"]["overall"] = "changed"
    gold_files[0].write_text(json.dumps(sound))
    assert before["digest"] != build_gold_manifest(catalog(), *gold_files[:2])["digest"]
    assert sound_readiness(gold_files[0])["labelled_release_ids"] == ["aoty:123"]
    sound["releases"][1]["normalized_sound"] = {"overall": "invented"}
    gold_files[0].write_text(json.dumps(sound))
    with pytest.raises(ValueError, match="unknown"):
        build_gold_manifest(catalog(), *gold_files[:2])


def test_wrong_source_or_taste_label_rejected(gold_files):
    sound = json.loads(gold_files[0].read_text())
    sound["releases"][0]["aoty_url"] = "https://elsewhere.invalid/album/123-gold.php"
    gold_files[0].write_text(json.dumps(sound))
    with pytest.raises(ValueError, match="source URL"):
        build_gold_manifest(catalog(), *gold_files[:2])


def test_shadow_candidate_is_executed_without_promotion_or_gold_features(gold_files, monkeypatch):
    import discovery.evals.gold_run as module

    actual_rank = module.rank
    seen = []

    def inspect_rank(cat, request, profile, **kwargs):
        seen.append(json.dumps([request.model_dump(), profile.model_dump()]))
        return actual_rank(cat, request, profile, **kwargs)

    monkeypatch.setattr(module, "rank", inspect_rank)
    artifact = model()
    original = deepcopy(artifact)
    report = evaluate_gold(catalog(), artifact, sound_path=gold_files[0],
        cases_path=gold_files[1], inputs_path=gold_files[2], as_of=date(2026, 11, 1))
    assert report["cases"][0]["candidate"]["candidate_applied"]
    assert report["status"] == "needs_human_review"
    assert report["training_isolation"]["status"] == "unknown_training_provenance"
    assert not report["passed"] and not report["serving_enabled"]
    assert not report["sound"]["musical_quality_measured"]
    assert artifact == original
    assert len(seen) == 2
    assert all("SECRET" not in row and "Gold Artist" not in row for row in seen)
    production = rank(catalog(), RequestIntent(genre_scope_id="hip-hop"), UserProfile(), artifact=artifact)
    assert all("model_probability" not in item.components for item in production.items)
    assert any("fixture_artifact" in warning for warning in production.warnings)


def test_untrained_candidate_cannot_claim_it_was_evaluated(gold_files):
    artifact = model()
    artifact["trained_genre_ids"] = ["other-genre"]
    report = evaluate_gold(catalog(), artifact, sound_path=gold_files[0],
        cases_path=gold_files[1], inputs_path=gold_files[2])
    assert not report["cases"][0]["candidate"]["candidate_applied"]
    assert report["status"] == "failed"


def test_no_unconfirmed_or_missing_cases(gold_files):
    gold_files[2].write_text(json.dumps({"cases": []}))
    with pytest.raises(ValueError, match="disagree"):
        evaluate_gold(catalog(), sound_path=gold_files[0], cases_path=gold_files[1], inputs_path=gold_files[2])


def test_candidate_trained_on_gold_is_contaminated(gold_files):
    artifact = model()
    artifact["training_release_ids"] = ["r0", "r1"]
    report = evaluate_gold(catalog(), artifact, sound_path=gold_files[0],
        cases_path=gold_files[1], inputs_path=gold_files[2])
    assert report["training_isolation"]["status"] == "contaminated"
    assert report["training_isolation"]["overlapping_release_ids"] == ["r0"]
    assert report["status"] == "failed"
