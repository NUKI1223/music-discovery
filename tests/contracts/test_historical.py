from datetime import UTC, datetime

import pytest

from discovery.catalog import load_catalog
from discovery.contracts import HistoricalObservation
from discovery.evals.scenarios import AS_OF, fixture_catalog
from discovery.llm.evidence import available_claims
from discovery.ranking import rank
from discovery.storage import catalog_digest


def history(**changes):
    return HistoricalObservation(source="rym", source_row_id="row", provenance_id="hash",
                                 rating=4.5, rating_scale=5, rating_count=1, **changes)


def test_old_catalog_hash_is_unchanged():
    assert catalog_digest(load_catalog("data/catalogs/musicbrainz-expanded.json")) == (
        "1d66f9d309e0d760ca601f24e829ebaeeeeb8c11a23c24ffa3f2a74f267602f4")


def test_unknown_capture_time_cannot_be_filled_with_retrieval_date():
    with pytest.raises(ValueError):
        history(observed_at=datetime.now(UTC))
    with pytest.raises(ValueError):
        history(count_semantics_verified=True)


def test_historical_values_cannot_affect_current_rarity_or_llm_count_claims():
    from discovery.contracts import RequestIntent, UserProfile

    catalog = fixture_catalog()
    for release in catalog.releases:
        release.observations = []
        release.historical_observations = [history()]
    result = rank(catalog, RequestIntent(genre_scope_id="scope-a", obscurity="require"),
                  UserProfile(), AS_OF)
    assert result.items == []
    assert all(c.field != "rating_count" for c in available_claims(catalog, [catalog.releases[0].id]))
    assert catalog.releases[0].historical_observations[0].observed_at is None
