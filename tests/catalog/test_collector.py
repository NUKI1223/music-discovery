"""Collector integration contracts, using only locally manufactured cache records."""

import hashlib
import json
from datetime import datetime
from pathlib import Path

import pytest

from discovery.catalog import collect_musicbrainz as collector
from discovery.catalog import io, musicbrainz
from discovery.catalog.io import load_catalog, save_catalog

RID = "11111111-1111-4111-8111-111111111111"
AID = "22222222-2222-4222-8222-222222222222"
SEARCH_TIME = "2024-01-02T12:00:00+00:00"
DETAIL_TIME = "2024-02-03T12:00:00+00:00"


def cache_record(raw, endpoint, params, response, captured_at):
    key = hashlib.sha256(json.dumps([endpoint, params], sort_keys=True).encode()).hexdigest()
    raw.mkdir(parents=True, exist_ok=True)
    path = raw / f"{key}.json"
    path.write_text(json.dumps({
        "endpoint": endpoint, "params": params,
        "url": "https://musicbrainz.org/ws/2/" + endpoint,
        "captured_at": captured_at, "response": response,
    }))
    return path


@pytest.fixture
def cached_collection(tmp_path, monkeypatch):
    # An accidental real transport invocation fails before opening any socket.
    def prohibit_network(*args, **kwargs):
        raise AssertionError("test attempted a real network request")

    monkeypatch.setattr(musicbrainz.httpx.HTTPTransport, "handle_request", prohibit_network)
    config = {
        "workspace": str(tmp_path / "workspace"),
        "output": str(tmp_path / "custom-output" / "albums.json"),
        "manifest": str(tmp_path / "custom-manifests" / "source.json"),
        "seed_artist_names": [], "max_releases": 1,
        "queries": [{"label": "jazz-rap", "query": 'tag:"jazz rap"', "limit": 5}],
    }
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config))
    raw = tmp_path / "workspace" / "raw"
    cache_record(raw, "release-group/", {
        "query": 'tag:"jazz rap"', "fmt": "json", "limit": 5, "offset": 0,
    }, {"release-groups": [{
        "id": RID, "title": "Cache test album", "primary-type": "Album",
        "first-release-date": "2020", "score": 100, "count": 900,
        "artist-credit": [{"artist": {"id": AID, "name": "Cache test artist"}}],
        "tags": [{"name": "jazz rap", "count": 2}],
    }]}, SEARCH_TIME)
    detail = cache_record(raw, f"release-group/{RID}", {
        "inc": "genres+tags+ratings+artist-credits", "fmt": "json",
    }, {"id": RID, "rating": {"value": 4.2, "votes-count": 7}}, DETAIL_TIME)
    return config_path, config, detail


def test_offline_resume_preserves_capture_times_and_snapshot(cached_collection):
    config_path, config, _ = cached_collection
    first = collector.collect(config_path, offline=True)
    original = load_catalog(config["output"])
    second = collector.collect(config_path, offline=True)
    rebuilt = load_catalog(config["output"])
    assert first["network_requests_this_run"] == second["network_requests_this_run"] == 0
    assert rebuilt == original
    assert rebuilt.releases[0].observations[0].observed_at == datetime.fromisoformat(DETAIL_TIME)
    observation = rebuilt.releases[0].observations[0]
    assert (observation.rating, observation.rating_count) == (4.2, 7)
    assert second["first_started_at"] == first["started_at"]
    assert second["g0_status"] == "limited"


def test_manifest_resolves_configured_output_in_arbitrary_layout(cached_collection):
    config_path, config, _ = cached_collection
    collector.collect(config_path, offline=True)
    assert load_catalog(config["manifest"]) == load_catalog(config["output"])


def test_offline_missing_cache_with_no_seed_artists_never_uses_network(cached_collection):
    config_path, config, _ = cached_collection
    for path in (Path(config["workspace"]) / "raw").glob("*.json"):
        path.unlink()
    with pytest.raises(RuntimeError, match="offline cache miss"):
        collector.collect(config_path, offline=True)


def test_invalid_detail_preserves_published_snapshot_and_unlocks_workspace(cached_collection):
    config_path, config, detail_path = cached_collection
    collector.collect(config_path, offline=True)
    output = Path(config["output"])
    before = output.read_bytes()
    cached = json.loads(detail_path.read_text())
    original = detail_path.read_text()
    cached["response"]["id"] = "33333333-3333-4333-8333-333333333333"
    detail_path.write_text(json.dumps(cached))
    with pytest.raises(ValueError, match="detail ID"):
        collector.collect(config_path, offline=True)
    assert output.read_bytes() == before
    detail_path.write_text(original)
    assert collector.collect(config_path, offline=True)["phase"] == "complete"


def test_atomic_publish_failure_keeps_previous_snapshot(tmp_path, cached_collection, monkeypatch):
    config_path, config, _ = cached_collection
    collector.collect(config_path, offline=True)
    catalog = load_catalog(config["output"])
    target = tmp_path / "snapshot.json"
    save_catalog(catalog, target)
    before = target.read_bytes()
    changed = catalog.model_copy(update={"snapshot_id": "another-snapshot"})

    def fail_replace(*args):
        raise OSError("simulated publication failure")

    monkeypatch.setattr(io.os, "replace", fail_replace)
    with pytest.raises(OSError, match="publication failure"):
        save_catalog(changed, target)
    assert target.read_bytes() == before
    assert list(tmp_path.glob("*.tmp")) == []
