"""Personal 'off request' marks: which releases are excluded for which genre requests."""
from datetime import UTC, datetime, timedelta

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


def make_store(tmp_path):
    genres = [Genre(id="g", name="G"), Genre(id="h", name="H")]
    catalog = Catalog(snapshot_id="off", genres=genres, artists=[Artist(id="a", name="A")],
                      releases=[Release(id="r", title="R", artist_ids=["a"], genre_ids=["g"])])
    return Store(tmp_path / "account.sqlite3"), catalog


def decide(store, catalog, name, scope, locked=()):
    request = RequestIntent(genre_scope_id=scope, locked_subgenre_ids=list(locked))
    decision = Decision(id=name, request=request, catalog_hash=catalog_digest(catalog),
                        catalog_snapshot_id=catalog.snapshot_id, taxonomy_version=catalog.taxonomy_version,
                        profile=UserProfile(), result=RankingResult(items=[
                            RecommendationItem(release_id="r", score=.5)]))
    store.commit_decision(name, catalog, request, lambda: decision)


def mark(store, name, kind, fid, day):
    at = T0 + timedelta(days=day)
    store.append_feedback(Feedback(id=fid, decision_id=name, release_id="r", kind=kind,
                                   occurred_at=at, recorded_at=at))


def test_same_genre_excluded_other_genre_not(tmp_path):
    store, catalog = make_store(tmp_path)
    decide(store, catalog, "d", "g")
    mark(store, "d", "off_request", "f1", 1)
    assert store.off_request_release_ids("g", []) == {"r"}
    assert store.off_request_release_ids("h", []) == set()


def test_mark_without_subgenres_excludes_any_subgenre_set(tmp_path):
    store, catalog = make_store(tmp_path)
    decide(store, catalog, "d", "g")
    mark(store, "d", "off_request", "f1", 1)
    assert store.off_request_release_ids("g", ["s1", "s2"]) == {"r"}


def test_mark_with_subgenres_excludes_subsets_only(tmp_path):
    store, catalog = make_store(tmp_path)
    decide(store, catalog, "d", "g", ["s1", "s2"])
    mark(store, "d", "off_request", "f1", 1)
    assert store.off_request_release_ids("g", ["s1", "s2"]) == {"r"}
    assert store.off_request_release_ids("g", ["s2"]) == {"r"}
    assert store.off_request_release_ids("g", []) == set()
    assert store.off_request_release_ids("g", ["s1", "s3"]) == set()
    assert store.off_request_release_ids("h", ["s1"]) == set()


def test_contexts_accumulate_across_decisions(tmp_path):
    store, catalog = make_store(tmp_path)
    decide(store, catalog, "d1", "g", ["s1"])
    decide(store, catalog, "d2", "g", ["s3"])
    mark(store, "d1", "off_request", "f1", 1)
    mark(store, "d2", "off_request", "f2", 2)
    assert store.off_request_release_ids("g", ["s3"]) == {"r"}
    assert store.off_request_release_ids("g", ["s1"]) == {"r"}
    assert store.off_request_release_ids("g", ["s2"]) == set()


def test_later_rating_clears_mark_and_neutral_kinds_do_not(tmp_path):
    store, catalog = make_store(tmp_path)
    decide(store, catalog, "d", "g")
    mark(store, "d", "off_request", "f1", 1)
    for i, kind in enumerate(["wrong_genre", "not_now", "familiarity"]):
        mark(store, "d", kind, f"n{i}", 2 + i)
    assert store.off_request_release_ids("g", []) == {"r"}
    mark(store, "d", "save_for_later", "f2", 10)
    assert store.off_request_release_ids("g", []) == set()
    mark(store, "d", "off_request", "f3", 11)
    assert store.off_request_release_ids("g", []) == {"r"}
    mark(store, "d", "listened_mixed", "f4", 12)
    assert store.off_request_release_ids("g", []) == set()


def test_revoked_event_is_ignored(tmp_path):
    store, catalog = make_store(tmp_path)
    decide(store, catalog, "d", "g")
    mark(store, "d", "off_request", "f1", 1)
    with store.connection() as conn:
        conn.execute("INSERT INTO revoked_feedback(id) VALUES ('f1')")
    assert store.off_request_release_ids("g", []) == set()


def test_off_request_is_not_taste_or_training_signal(tmp_path):
    from discovery.cli import training_records
    store, catalog = make_store(tmp_path)
    decide(store, catalog, "d", "g")
    mark(store, "d", "off_request", "f1", 1)
    profile = store.profile()
    assert profile.known_release_ids == []
    assert not any(profile.liked_release_ids_by_genre.values())
    assert not any(profile.disliked_release_ids_by_genre.values())
    assert store.collection_release_ids() == set()
    assert training_records(store) == []
