from uuid import uuid4

import pytest

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
from discovery.service import discovery_metrics
from discovery.storage import Store, catalog_digest


def sample():
    return Catalog(snapshot_id="snapshot", is_fixture=True,
                   genres=[Genre(id="g", name="Genre")],
                   artists=[Artist(id="a", name="Artist")],
                   releases=[Release(id="r", title="Album", artist_ids=["a"], genre_ids=["g"])])


def committed(store):
    catalog = sample()
    intent = RequestIntent(genre_scope_id="g")
    decision = Decision(id="request", request=intent, catalog_snapshot_id=catalog.snapshot_id,
                        catalog_hash=catalog_digest(catalog), taxonomy_version="1",
                        profile=UserProfile(), result=RankingResult(items=[
                            RecommendationItem(release_id="r", score=1)]))
    store.commit_decision(decision.id, catalog, intent, lambda: decision)
    store.mark_shown(decision)
    return decision


def test_retry_never_runs_factory_and_id_conflicts_rejected(tmp_path):
    store = Store(tmp_path / "state.db")
    decision = committed(store)

    def forbidden():
        pytest.fail("repeated request resampled")

    assert store.commit_decision("request", sample(), decision.request, forbidden) == decision
    with pytest.raises(ValueError, match="different request"):
        store.commit_decision("request", sample(), RequestIntent(genre_scope_id="g", count=1), forbidden)


def test_feedback_idempotency_corrections_and_historical_familiarity(tmp_path):
    store = Store(tmp_path / "state.db")
    decision = committed(store)
    store.mark_shown(decision)
    event = Feedback(id="like", decision_id=decision.id, release_id="r", kind="listened_like",
                     familiar_before_recommendation=False)
    assert store.append_feedback(event)
    assert not store.append_feedback(event)
    assert store.profile().known_release_ids == ["r"]
    assert store.profile().liked_release_ids_by_genre == {"g": ["r"]}
    assert discovery_metrics(store)["by_genre"]["g"]["discovery_rate"] == 1
    assert discovery_metrics(store)["by_genre"]["g"]["shown"] == 1
    store.append_feedback(Feedback(id=str(uuid4()), decision_id=decision.id,
                                   release_id="r", kind="listened_dislike"))
    assert store.profile().liked_release_ids_by_genre["g"] == []
    assert store.profile().disliked_release_ids_by_genre["g"] == ["r"]
    assert discovery_metrics(store)["by_genre"]["g"]["discovery_rate"] == 0
    assert store.get_decision("request").profile.known_release_ids == []


def test_missing_feedback_not_dislike_and_backup_restore(tmp_path):
    store = Store(tmp_path / "state.db")
    decision = committed(store)
    store.append_feedback(Feedback(id="save", decision_id=decision.id, release_id="r",
                                   kind="save_for_later"))
    assert not store.profile().disliked_release_ids_by_genre
    assert discovery_metrics(store)["by_genre"]["g"]["discovery_rate"] is None
    store.backup(tmp_path / "backup.db")
    restored = Store(tmp_path / "backup.db")
    assert restored.export() == store.export()
    with pytest.raises(ValueError, match="already exists"):
        store.backup(tmp_path / "backup.db")


def test_feedback_requires_real_decision_item(tmp_path):
    store = Store(tmp_path / "state.db")
    committed(store)
    with pytest.raises(ValueError, match="stored decision"):
        store.append_feedback(Feedback(id="bad", decision_id="request", release_id="unknown",
                                       kind="listened_like"))


def test_impressions_reject_unstored_or_modified_decisions(tmp_path):
    store = Store(tmp_path / "state.db")
    decision = committed(store).model_copy(deep=True)
    decision.result.items[0].release_id = "not-selected"
    with pytest.raises(ValueError, match="immutable stored"):
        store.mark_shown(decision)


def test_cannot_mix_real_and_fixture_catalogs(tmp_path):
    store = Store(tmp_path / "state.db")
    decision = committed(store)
    real = sample().model_copy(update={"is_fixture": False})
    with pytest.raises(ValueError, match="separate databases"):
        store.commit_decision("real", real, decision.request, lambda: decision)


def test_concurrent_duplicate_feedback_is_idempotent(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    store = Store(tmp_path / "state.db")
    committed(store)
    event = Feedback(id="same", decision_id="request", release_id="r", kind="known")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(store.append_feedback, [event, event]))
    assert sorted(results) == [False, True]
    assert len(store.feedback()) == 1
