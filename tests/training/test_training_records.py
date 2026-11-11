"""Training export: familiar music is not a 'new music will be liked' label."""

from datetime import UTC, datetime, timedelta

from discovery.cli import training_records
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
from discovery.storage import Store, catalog_digest

T0 = datetime(2024, 1, 1, tzinfo=UTC)


def make_store(tmp_path, kinds):
    """kinds: list of (kind, familiar_flag, extra_familiarity_event)."""
    catalog = Catalog(snapshot_id="records", genres=[Genre(id="g", name="G")],
                      artists=[Artist(id=f"a{i}", name=f"A{i}") for i in range(len(kinds))],
                      releases=[Release(id=f"r{i}", title=f"R{i}", artist_ids=[f"a{i}"], genre_ids=["g"])
                                for i in range(len(kinds))])
    store = Store(tmp_path / "account.sqlite3")
    for i, (kind, flag, separate) in enumerate(kinds):
        at = T0 + timedelta(days=i)
        request = RequestIntent(genre_scope_id="g")
        decision = Decision(id=f"d{i}", request=request, created_at=at,
                            catalog_hash=catalog_digest(catalog), catalog_snapshot_id="records",
                            taxonomy_version=catalog.taxonomy_version, profile=UserProfile(),
                            result=RankingResult(items=[RecommendationItem(
                                release_id=f"r{i}", score=.5, components={"similarity": .5})]))
        store.commit_decision(decision.id, catalog, request, lambda d=decision: d)
        later = at + timedelta(hours=1)
        store.append_feedback(Feedback(id=f"f{i}", decision_id=decision.id, release_id=f"r{i}", kind=kind,
                                       familiar_before_recommendation=flag,
                                       occurred_at=later, recorded_at=later))
        if separate is not None:
            store.append_feedback(Feedback(id=f"s{i}", decision_id=decision.id, release_id=f"r{i}",
                                           kind="familiarity", familiar_before_recommendation=separate,
                                           occurred_at=later, recorded_at=later))
    return store


def test_familiar_ratings_are_not_training_labels(tmp_path):
    store = make_store(tmp_path, [
        ("listened_like", False, None),   # new to the user: kept
        ("listened_like", True, None),    # already knew it: excluded
        ("listened_like", None, True),    # familiarity answered separately: excluded
        ("listened_dislike", True, None),  # old music says nothing about new picks: excluded
        ("listened_dislike", None, False),  # kept
        ("listened_like", None, None),    # unknown familiarity: kept (not asserted familiar)
    ])
    rows = training_records(store)
    assert {r["release_id"]: r["label"] for r in rows} == {"r0": 1, "r4": 0, "r5": 1}


def test_catalog_loaded_once_per_hash(tmp_path, monkeypatch):
    store = make_store(tmp_path, [("listened_like", False, None)] * 5)
    calls = []
    original = Store.get_catalog
    monkeypatch.setattr(Store, "get_catalog", lambda self, key: calls.append(key) or original(self, key))
    assert len(training_records(store)) == 5
    assert len(calls) == 1


def test_mixed_supersedes_earlier_label_and_never_creates_one(tmp_path):
    store = make_store(tmp_path, [("listened_like", False, None), ("listened_mixed", False, None)])
    # r0 rated like, then mixed in the same decision: the stale label must go.
    later = T0 + timedelta(days=5)
    store.append_feedback(Feedback(id="mixed-after", decision_id="d0", release_id="r0",
                                   kind="listened_mixed", score=50, occurred_at=later, recorded_at=later))
    assert training_records(store) == []


def test_off_request_never_creates_a_label_and_keeps_an_earlier_one(tmp_path):
    store = make_store(tmp_path, [("off_request", False, None), ("listened_like", False, None)])
    assert {r["release_id"]: r["label"] for r in training_records(store)} == {"r1": 1}
    later = T0 + timedelta(days=5)
    store.append_feedback(Feedback(id="off-after", decision_id="d1", release_id="r1",
                                   kind="off_request", occurred_at=later, recorded_at=later))
    assert {r["release_id"]: r["label"] for r in training_records(store)} == {"r1": 1}
