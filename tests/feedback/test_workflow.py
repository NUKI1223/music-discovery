from datetime import timedelta

import pytest

from discovery.cli import training_records
from discovery.contracts import Feedback, RequestIntent
from discovery.evals.scenarios import AS_OF, fixture_catalog
from discovery.service import recommend
from discovery.storage import Store


def test_export_uses_decision_snapshot_not_current_profile(tmp_path):
    catalog = fixture_catalog()
    store = Store(tmp_path / "state.db")
    decision = recommend(store, catalog, RequestIntent(genre_scope_id="scope-a"), as_of=AS_OF)
    first = decision.result.items[0]
    store.append_feedback(Feedback(id="like", decision_id=decision.id, release_id=first.release_id,
                                   kind="listened_like", familiar_before_recommendation=False))
    record = training_records(store)[0]
    assert record["features"]["profile_confidence"] == 0
    assert record["is_fixture"] is True
    assert record["label"] == 1
    later = Feedback(id="correction", decision_id=decision.id, release_id=first.release_id,
                     kind="listened_dislike", recorded_at=decision.created_at + timedelta(minutes=1))
    store.append_feedback(later)
    records = training_records(store)
    assert len(records) == 1 and records[0]["label"] == 0
    assert records[0]["available_at"] == later.recorded_at.isoformat()
    historical = training_records(store, cutoff=decision.created_at + timedelta(seconds=30))
    assert len(historical) == 1 and historical[0]["label"] == 1
    assert training_records(store, cutoff=decision.created_at - timedelta(seconds=1)) == []


def test_two_genres_remain_isolated_after_feedback(tmp_path):
    store = Store(tmp_path / "state.db")
    catalog = fixture_catalog()
    decision = recommend(store, catalog, RequestIntent(genre_scope_id="scope-a"), as_of=AS_OF)
    store.append_feedback(Feedback(id="like", decision_id=decision.id,
                                   release_id=decision.result.items[0].release_id, kind="listened_like"))
    other = recommend(store, catalog, RequestIntent(genre_scope_id="scope-b"), as_of=AS_OF)
    assert other.result.profile_basis == "unavailable"
    assert all(i.release_id.startswith("release-scope-b") for i in other.result.items)


@pytest.mark.parametrize("kind", ["save_for_later", "wrong_genre", "familiarity"])
def test_familiarity_is_independent_of_rating_kind(tmp_path, kind):
    catalog = fixture_catalog()
    store = Store(tmp_path / "state.db")
    request = RequestIntent(genre_scope_id="scope-a")
    decision = recommend(store, catalog, request, as_of=AS_OF)
    rid = decision.result.items[0].release_id
    store.append_feedback(Feedback(id="familiar", decision_id=decision.id, release_id=rid,
                                   kind=kind, familiar_before_recommendation=True))
    assert rid in store.profile().known_release_ids
    assert training_records(store) == []
    again = recommend(store, catalog, request, as_of=AS_OF)
    assert rid not in [item.release_id for item in again.result.items]
