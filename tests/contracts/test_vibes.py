"""Source-specific Vibes extend descriptors without changing legacy snapshots."""

import json

import pytest

from discovery.contracts import Artist, Catalog, Genre, Release, RequestIntent, SourceMetadata


def metadata(**extra):
    return SourceMetadata(source="aoty", source_row_id="123", title="Synthetic release",
                          artist_credit="Synthetic artist", **extra)


def test_empty_source_vibes_preserve_legacy_serialized_payload():
    legacy = {"source": "aoty", "source_row_id": "123", "title": "Synthetic release",
              "artist_credit": "Synthetic artist", "release_date_text": None,
              "primary_labels": [], "secondary_labels": [], "primary_ids": [],
              "secondary_ids": [], "descriptors": [], "unmapped": [], "mapping_evidence": []}
    assert metadata().model_dump() == legacy
    assert json.loads(metadata().model_dump_json()) == legacy
    enriched = metadata(vibes=["energetic", "dense"])
    assert enriched.model_dump()["vibes"] == ["energetic", "dense"]
    assert enriched.primary_labels == enriched.secondary_labels == enriched.descriptors == []


def test_request_accepts_vibes_as_descriptors_only_in_the_selected_genre():
    catalog = Catalog(snapshot_id="vibes-test", is_fixture=True,
        genres=[Genre(id="jazz", name="Jazz"), Genre(id="metal", name="Metal")],
        artists=[Artist(id="a", name="Synthetic artist")],
        releases=[Release(id="r", title="Synthetic release", artist_ids=["a"],
                          genre_ids=["jazz"], source_metadata=[metadata(vibes=[" Energetic ", " "])])])
    RequestIntent(genre_scope_id="jazz", preferred_descriptors=["energetic"]).validate_catalog(catalog)
    RequestIntent(genre_scope_id="jazz", avoided_descriptors=["energetic"]).validate_catalog(catalog)
    with pytest.raises(ValueError, match="Unknown source descriptor"):
        RequestIntent(genre_scope_id="metal", preferred_descriptors=["energetic"]).validate_catalog(catalog)
    with pytest.raises(ValueError, match="locked subgenres"):
        RequestIntent(genre_scope_id="jazz", locked_subgenre_ids=["energetic"]).validate_catalog(catalog)
