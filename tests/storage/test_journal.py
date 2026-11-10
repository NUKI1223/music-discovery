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


def test_mixed_rating_is_neutral_known_and_supersedes_binary_labels(tmp_path):
    store = Store(tmp_path / "state.db")
    decision = committed(store)

    def add(name, kind, **extra):
        store.append_feedback(Feedback(id=name, decision_id=decision.id, release_id="r", kind=kind, **extra))

    add("like", "listened_like")
    assert store.profile().liked_release_ids_by_genre["g"] == ["r"]
    add("mixed", "listened_mixed", score=55, review="Есть и хорошее, и слабое")
    profile = store.profile()
    assert profile.liked_release_ids_by_genre["g"] == []
    assert profile.disliked_release_ids_by_genre["g"] == []
    assert profile.known_release_ids == ["r"]
    assert "r" in store.collection_release_ids()
    add("like-again", "listened_like")
    assert store.profile().liked_release_ids_by_genre["g"] == ["r"]
    add("dislike", "listened_dislike")
    add("mixed-2", "listened_mixed")
    profile = store.profile()
    assert profile.liked_release_ids_by_genre["g"] == [] and profile.disliked_release_ids_by_genre["g"] == []


def test_mixed_alone_creates_no_label_and_no_reward(tmp_path):
    store = Store(tmp_path / "state.db")
    decision = committed(store)
    store.append_feedback(Feedback(id="mixed", decision_id=decision.id, release_id="r",
                                   kind="listened_mixed", familiar_before_recommendation=False))
    profile = store.profile()
    assert not profile.liked_release_ids_by_genre.get("g") and not profile.disliked_release_ids_by_genre.get("g")
    assert discovery_metrics(store)["by_genre"]["g"]["discovery_rate"] in (0, None)


def test_later_mixed_removes_earlier_rating_from_metrics(tmp_path):
    store = Store(tmp_path / "state.db")
    decision = committed(store)
    store.append_feedback(Feedback(id="like", decision_id=decision.id, release_id="r",
                                   kind="listened_like", familiar_before_recommendation=False))
    assert discovery_metrics(store)["by_genre"]["g"]["discovery_rate"] == 1
    store.append_feedback(Feedback(id="mixed", decision_id=decision.id, release_id="r",
                                   kind="listened_mixed"))
    assert discovery_metrics(store)["by_genre"]["g"]["discovery_rate"] != 1


def test_feedback_contract_score_review_rules_and_old_payloads():
    from pydantic import ValidationError
    old = Feedback.model_validate_json(
        '{"id":"x","decision_id":"d","release_id":"r","kind":"listened_like"}')
    assert old.score is None and old.review is None
    for kind in ("listened_like", "listened_mixed", "listened_dislike"):
        event = Feedback(id="x", decision_id="d", release_id="r", kind=kind, score=0, review="ok")
        assert event.score == 0
        assert Feedback(id="x", decision_id="d", release_id="r", kind=kind, score=100).score == 100
    for bad in ({"score": -1}, {"score": 101}, {"review": "я" * 4001}):
        with pytest.raises(ValidationError):
            Feedback(id="x", decision_id="d", release_id="r", kind="listened_mixed", **bad)
    assert len(Feedback(id="x", decision_id="d", release_id="r", kind="listened_mixed",
                        review="я" * 4000).review) == 4000
    for kind in ("known", "save_for_later"):
        with pytest.raises(ValidationError):
            Feedback(id="x", decision_id="d", release_id="r", kind=kind, score=5)
        with pytest.raises(ValidationError):
            Feedback(id="x", decision_id="d", release_id="r", kind=kind, review="текст")
