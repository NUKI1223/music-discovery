import pytest
from pydantic import ValidationError

from discovery.contracts import (
    Artist,
    Catalog,
    Feedback,
    Genre,
    Observation,
    Release,
    RequestIntent,
)


def test_crossover_taxonomy_and_scope():
    catalog = Catalog(snapshot_id="test", is_fixture=True, genres=[
        Genre(id="a", name="A"), Genre(id="b", name="B"),
        Genre(id="c", name="C", parent_ids=["a", "b"]),
    ], artists=[Artist(id="artist", name="Artist")], releases=[
        Release(id="r", title="Crossover", artist_ids=["artist"], genre_ids=["c"])
    ])
    assert catalog.descendant_ids("a") == {"a", "c"}
    assert catalog.descendant_ids("b") == {"b", "c"}
    with pytest.raises(ValueError, match="selected scope"):
        RequestIntent(genre_scope_id="a", locked_subgenre_ids=["b"]).validate_catalog(catalog)
    with pytest.raises(ValueError, match="unknown genre"):
        RequestIntent(genre_scope_id="missing").validate_catalog(catalog)


def test_cycle_and_foreign_keys_rejected():
    with pytest.raises(ValidationError, match="cycle"):
        Catalog(snapshot_id="bad", genres=[Genre(id="a", name="A", parent_ids=["a"])])
    with pytest.raises(ValidationError, match="unknown artist"):
        Catalog(snapshot_id="bad", releases=[Release(id="r", title="x", artist_ids=["missing"])])


def test_rating_scale_and_timezone_required():
    with pytest.raises(ValidationError):
        Observation(source="demo", observed_at="2026-01-01T00:00:00Z", rating=99,
                    rating_scale=5, access_basis="fixture")
    with pytest.raises(ValidationError):
        Observation(source="demo", observed_at="2026-01-01T00:00:00", access_basis="fixture")


def test_known_feedback_cannot_claim_unfamiliarity():
    with pytest.raises(ValidationError, match="cannot be unfamiliar"):
        Feedback(id="bad", decision_id="decision", release_id="r", kind="known",
                 familiar_before_recommendation=False)
