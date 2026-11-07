import hashlib
import json
from pathlib import Path

import pytest

from discovery.catalog import load_catalog
from discovery.catalog.dataset import export_metadata, metadata_records


def test_source_ratings_never_become_personal_labels():
    catalog = load_catalog("data/samples/musicbrainz.json")
    records = metadata_records(catalog)
    assert len(records) == len({row["release_id"] for row in records})
    assert all("label" not in row and row["is_fixture"] is False for row in records)
    expected = {r.id: [o.model_dump(mode="json") for o in r.observations] for r in catalog.releases}
    assert all(row["observations"] == expected[row["release_id"]] for row in records)


def test_export_is_content_addressed_and_existing_dataset_is_preserved(tmp_path):
    catalog = load_catalog("data/samples/musicbrainz.json")
    target = tmp_path / "export"
    manifest = export_metadata(catalog, target)
    content = (target / "releases.jsonl").read_bytes()
    assert manifest["records_sha256"] == hashlib.sha256(content).hexdigest()
    assert manifest["rows"] == len(content.splitlines()) == 33
    assert manifest["personal_labels"] == 0
    assert json.loads((target / "manifest.json").read_text()) == manifest
    with pytest.raises(FileExistsError):
        export_metadata(catalog, target)
    assert (target / "releases.jsonl").read_bytes() == content


def test_enrichment_never_joins_a_different_snapshot_or_artist(tmp_path):
    catalog = load_catalog("data/samples/musicbrainz.json")
    sidecar = json.loads(Path("data/wikidata/pilot/dataset.json").read_text())
    manifest = export_metadata(catalog, tmp_path / "matching", enrichment=sidecar)
    assert manifest["enrichment"]["matched_rows"] == 10
    sidecar["records"][0]["artist_ids"] = ["different-artist"]
    with pytest.raises(ValueError, match="exact release and artist"):
        export_metadata(catalog, tmp_path / "bad-artists", enrichment=sidecar)
    sidecar["catalog_sha256"] = "wrong-hash"
    with pytest.raises(ValueError, match="provenance"):
        export_metadata(catalog, tmp_path / "bad-snapshot", enrichment=sidecar)
    assert not (tmp_path / "bad-snapshot").exists()


@pytest.mark.parametrize("forged", [99, 1, []])
def test_enrichment_rejects_unsupported_features_and_malformed_types(tmp_path, forged):
    catalog = load_catalog("data/samples/musicbrainz.json")
    sidecar = json.loads(Path("data/wikidata/pilot/dataset.json").read_text())
    sidecar["records"][0]["features"] = ({"wikidata_genre:Q123456": forged}
                                        if isinstance(forged, int) else forged)
    with pytest.raises(ValueError):
        export_metadata(catalog, tmp_path / "forged", enrichment=sidecar)
    assert not (tmp_path / "forged").exists()
