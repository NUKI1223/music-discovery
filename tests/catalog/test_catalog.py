import json
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta

import pytest
from pydantic import ValidationError

from discovery.catalog import load_catalog, save_catalog
from discovery.catalog.audit import audit_catalog
from discovery.catalog.demo import make_demo_catalog
from discovery.catalog.importer import merge_catalogs
from discovery.contracts import Artist, Catalog, Genre, Observation, Release


@pytest.fixture
def small_catalog():
    return Catalog(
        snapshot_id="real-test-input", genres=[Genre(id="genre-a", name="A")],
        artists=[Artist(id="artist-a", name="Artist A", genre_ids=["genre-a"])],
        releases=[Release(id="release-a", title="A", artist_ids=["artist-a"],
                          genre_ids=["genre-a"], year=2010)],
    )


def test_manifest_resolves_relative_to_manifest_not_cwd(tmp_path, small_catalog, monkeypatch):
    save_catalog(small_catalog, tmp_path / "snapshots/catalog.json")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"catalog_path": "snapshots/catalog.json"}))
    monkeypatch.chdir(tmp_path.parent)
    assert load_catalog(manifest) == small_catalog


def test_failed_validation_does_not_replace_existing_file(tmp_path, small_catalog):
    path = tmp_path / "catalog.json"
    save_catalog(small_catalog, path)
    before = path.read_bytes()
    small_catalog.releases[0].genre_ids.append("absent")
    with pytest.raises(ValidationError):
        save_catalog(small_catalog, path)
    assert path.read_bytes() == before


def test_repeat_import_is_idempotent_and_conflicts_do_not_overwrite(small_catalog):
    first = merge_catalogs(small_catalog, small_catalog)
    assert not any(first.added.values())
    assert first.catalog == small_catalog
    changed = small_catalog.model_copy(deep=True)
    changed.releases[0].title = "Different album, same ID"
    second = merge_catalogs(small_catalog, changed)
    assert len(second.conflicts) == 1
    assert second.catalog.releases[0].title == "A"


def test_real_fixture_mixing_is_rejected(small_catalog):
    with pytest.raises(ValueError, match="must remain separate"):
        merge_catalogs(small_catalog, make_demo_catalog())


def test_crossover_keeps_one_identity_and_multiple_parents():
    catalog = make_demo_catalog()
    assert "jazz-rap" in catalog.descendant_ids("hip-hop")
    assert "jazz-rap" in catalog.descendant_ids("jazz")
    crossover = [r.id for r in catalog.releases if "jazz-rap" in r.genre_ids]
    assert len(crossover) == len(set(crossover)) == 48


def test_synthetic_data_never_passes_g0_even_with_perfect_claimed_review():
    catalog = make_demo_catalog()
    report = audit_catalog(catalog, as_of=date(2026, 10, 28), review={
        "confirmed_genre_release_ids": [r.id for r in catalog.releases],
        "access_verified_sources": ["synthetic-demo"],
    }, requirements_by_genre={"hip-hop": {"min_releases": 1}})
    assert report["g0_status"] == "limited"
    assert "synthetic_data_is_not_product_evidence" in report["scope_reports"]["hip-hop"]["reasons"]


@pytest.mark.parametrize("offset,count", [(0, None), (-91, 8), (1, 8)])
def test_missing_stale_future_counts_are_not_usable(small_catalog, offset, count):
    small_catalog.releases[0].observations = [Observation(
        source="test", observed_at=datetime(2026, 10, 28, tzinfo=UTC) + timedelta(days=offset),
        rating_count=count, access_basis="test", url="https://example.com/album",
    )]
    report = audit_catalog(small_catalog, as_of=date(2026, 10, 28))
    scope = report["scope_reports"]["genre-a"]
    assert scope["usable_popularity_coverage"] == 0
    assert all(c["rare_release_count"] == 0 for c in scope["cohorts"])


def test_equal_vote_counts_do_not_make_whole_cohort_rare():
    catalog = make_demo_catalog()
    for release in catalog.releases:
        release.observations[0].rating_count = 10
    result = audit_catalog(catalog, as_of=date(2026, 10, 28))
    assert all(row["rare_release_count"] == 0
               for scope in result["scope_reports"].values() for row in scope["cohorts"])


def test_source_counts_are_never_added_together(small_catalog):
    small_catalog.releases[0].observations = [
        Observation(source=source, observed_at=datetime(2026, 10, 28, tzinfo=UTC),
                    rating_count=count, access_basis="test")
        for source, count in [("a", 2), ("b", 100)]
    ]
    rows = audit_catalog(small_catalog, as_of=date(2026, 10, 28))["scope_reports"]["genre-a"]["cohorts"]
    assert {row["source"] for row in rows} == {"a", "b"}
    assert all(row["usable_observation_count"] == 1 for row in rows)


def test_cli_reports_invalid_json_without_traceback(tmp_path):
    path = tmp_path / "invalid.json"
    path.write_text("{broken")
    result = subprocess.run(
        [sys.executable, "-m", "discovery.catalog.audit", "--manifest", str(path)],
        text=True, capture_output=True, check=False,
    )
    assert result.returncode == 2
    assert json.loads(result.stdout)["status"] == "invalid"
    assert "Traceback" not in result.stderr


def test_unknown_genre_cannot_be_silently_defaulted(small_catalog):
    with pytest.raises(ValueError, match="unknown genre"):
        small_catalog.descendant_ids("hip-hop")


def test_new_genre_needs_its_own_product_requirements(small_catalog):
    report = audit_catalog(small_catalog, as_of=date(2026, 10, 28))
    assert "genre_product_requirements_not_configured" in report["scope_reports"]["genre-a"]["reasons"]


def test_missing_snapshot_falls_back_to_committed_gzip_copy(tmp_path, small_catalog):
    import gzip
    save_catalog(small_catalog, tmp_path / "catalog.json")
    raw = (tmp_path / "catalog.json").read_bytes()
    (tmp_path / "catalog.json.gz").write_bytes(gzip.compress(raw))
    (tmp_path / "catalog.json").unlink()
    (tmp_path / "manifest.json").write_text(json.dumps({"catalog_path": "catalog.json"}))
    assert load_catalog(tmp_path / "catalog.json") == small_catalog
    assert load_catalog(tmp_path / "manifest.json") == small_catalog
    assert load_catalog(tmp_path / "catalog.json.gz") == small_catalog
