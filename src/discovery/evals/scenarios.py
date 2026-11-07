"""Held constant synthetic scenarios with independent outcome assertions."""

from datetime import UTC, date, datetime
from tempfile import TemporaryDirectory

from discovery.contracts import (
    Artist,
    Catalog,
    Feedback,
    Genre,
    Observation,
    Release,
    RequestIntent,
    UserProfile,
)
from discovery.ranking import rank
from discovery.service import discovery_metrics, recommend
from discovery.storage import Store

AS_OF = date(2026, 10, 28)


def fixture_catalog() -> Catalog:
    genres = [Genre(id="scope-a", name="First genre"), Genre(id="scope-b", name="Second genre")]
    genres += [Genre(id=f"{scope}-{tag}", name=f"{scope} {tag}", parent_ids=[scope])
               for scope in ("scope-a", "scope-b") for tag in ("near", "far")]
    artists, releases = [], []
    for scope in ("scope-a", "scope-b"):
        for i in range(40):
            aid, rid = f"artist-{scope}-{i}", f"release-{scope}-{i}"
            genre = f"{scope}-{'near' if i % 2 == 0 else 'far'}"
            artists.append(Artist(id=aid, name=aid, genre_ids=[genre]))
            releases.append(Release(id=rid, title=rid, artist_ids=[aid], genre_ids=[genre],
                                    year=2010, observations=[Observation(
                                        source="synthetic-eval", observed_at=datetime(2026, 10, 28, tzinfo=UTC),
                                        rating=70, rating_scale=100, rating_count=i+1,
                                        access_basis="invented_test_fixture",
                                    )]))
    return Catalog(snapshot_id="frozen-evals-v1", is_fixture=True, genres=genres,
                   artists=artists, releases=releases)


def run_scenarios() -> list[dict]:
    catalog = fixture_catalog()
    results = []

    def check(name, action):
        try:
            action()
            results.append({"name": name, "passed": True})
        except (AssertionError, ValueError, KeyError, TypeError) as exc:
            results.append({"name": name, "passed": False, "error": str(exc)})

    def scoped_second_genre():
        result = rank(catalog, RequestIntent(genre_scope_id="scope-b"), UserProfile(), AS_OF)
        assert len(result.items) == 5
        assert all(i.release_id.startswith("release-scope-b") for i in result.items)

    def known_exclusion():
        known = [r.id for r in catalog.releases]
        result = rank(catalog, RequestIntent(genre_scope_id="scope-a"),
                      UserProfile(known_release_ids=known), AS_OF)
        assert not result.items

    def empty_catalog():
        empty = catalog.model_copy(update={"releases": []})
        assert not rank(empty, RequestIntent(genre_scope_id="scope-a"), UserProfile(), AS_OF).items

    def unknown_rarity():
        unknown = catalog.model_copy(deep=True)
        for release in unknown.releases:
            release.observations = []
        assert not rank(unknown, RequestIntent(genre_scope_id="scope-a", obscurity="require"),
                        UserProfile(), AS_OF).items

    def actual_rarity():
        result = rank(catalog, RequestIntent(genre_scope_id="scope-a", obscurity="require"),
                      UserProfile(), AS_OF)
        assert result.items
        assert all(int(i.release_id.rsplit("-", 1)[1]) < 16 for i in result.items)

    def locked_scope():
        result = rank(catalog, RequestIntent(genre_scope_id="scope-a", mode="surprise",
                                             locked_subgenre_ids=["scope-a-far"]), UserProfile(), AS_OF)
        assert all(int(i.release_id.rsplit("-", 1)[1]) % 2 == 1 for i in result.items)

    def replay_feedback():
        with TemporaryDirectory() as directory:
            store = Store(directory + "/eval.db")
            request = RequestIntent(genre_scope_id="scope-b")
            first = recommend(store, catalog, request, request_id="fixed", as_of=AS_OF)
            store.mark_shown(first)
            item = first.result.items[0]
            store.append_feedback(Feedback(id="outcome", decision_id=first.id,
                                           release_id=item.release_id, kind="listened_like",
                                           familiar_before_recommendation=False))
            assert recommend(store, catalog, request, request_id="fixed", as_of=AS_OF) == first
            next_decision = recommend(store, catalog, request, as_of=AS_OF)
            assert item.release_id not in [i.release_id for i in next_decision.result.items]
            assert discovery_metrics(store)["by_genre"]["scope-b"]["discovery_rate"] == 1

    def mode_change():
        profile = UserProfile(seed_artist_ids_by_genre={"scope-a": ["artist-scope-a-0"]})
        close = rank(catalog, RequestIntent(genre_scope_id="scope-a", mode="close"), profile, AS_OF)
        surprise = rank(catalog, RequestIntent(genre_scope_id="scope-a", mode="surprise"), profile, AS_OF)
        def mean(items):
            return sum(i.components["novelty"] for i in items) / len(items)
        assert close.items and surprise.items
        assert mean(surprise.items) > mean(close.items)

    for name, action in [
        ("second-genre-without-core-changes", scoped_second_genre),
        ("known-releases-excluded", known_exclusion), ("empty-catalog", empty_catalog),
        ("unknown-popularity-is-not-rare", unknown_rarity), ("strict-rarity-threshold", actual_rarity),
        ("locked-subgenre-in-surprise", locked_scope), ("durable-feedback-replay", replay_feedback),
        ("novelty-mode-changes-distance", mode_change),
    ]:
        check(name, action)
    return results
