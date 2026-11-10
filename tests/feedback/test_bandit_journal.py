from discovery.contracts import Feedback, RequestIntent, UserProfile
from discovery.evals.scenarios import AS_OF, fixture_catalog
from discovery.ranking.baseline import FEATURE_VERSION
from discovery.service import recommend
from discovery.storage import Store


def test_sample_committed_once_and_delayed_feedback_trains_only_selected_slot(tmp_path):
    catalog = fixture_catalog()
    store = Store(tmp_path / "history.db")
    request = RequestIntent(genre_scope_id="scope-a", mode="surprise")
    profile = UserProfile(seed_artist_ids_by_genre={"scope-a": ["artist-scope-a-0"]})
    first = recommend(store, catalog, request, profile, request_id="retry", as_of=AS_OF, use_bandit=True)
    assert first.policy_details["enabled"]
    assert first.feature_version == FEATURE_VERSION
    repeated = recommend(store, catalog, request, profile, request_id="retry", as_of=AS_OF, use_bandit=True)
    assert repeated == first
    assert len(store.decisions()) == 1
    for i in range(10):
        decision = recommend(store, catalog, request, profile, as_of=AS_OF, use_bandit=True)
        store.append_feedback(Feedback(
            id=f"outcome-{i}", decision_id=decision.id,
            release_id=decision.policy_details["selected_release_id"],
            kind="listened_like" if i % 2 else "listened_dislike",
        ))
    learned = recommend(store, catalog, request, profile, as_of=AS_OF, use_bandit=True)
    assert learned.policy_details["reward_model_basis"] == "experimental_scoped_logistic"
    assert learned.policy_details["reward_labels"] == 10
    assert len(learned.policy_details["reward_event_ids"]) == 10
    assert learned.policy_details["chosen_probability"] > 0


def test_mixed_in_other_decision_supersedes_bandit_like_label(tmp_path):
    from discovery.service import _reward_scores
    catalog = fixture_catalog()
    store = Store(tmp_path / "history.db")
    request = RequestIntent(genre_scope_id="scope-a", mode="surprise")
    profile = UserProfile(seed_artist_ids_by_genre={"scope-a": ["artist-scope-a-0"]})
    first = recommend(store, catalog, request, profile, as_of=AS_OF, use_bandit=True)
    rid = first.policy_details["selected_release_id"]
    store.append_feedback(Feedback(id="like", decision_id=first.id, release_id=rid, kind="listened_like"))
    assert _reward_scores(store, catalog, request, profile, AS_OF)[1]["reward_labels"] == 1
    other = recommend(store, catalog, request, profile, as_of=AS_OF)  # no bandit
    assert not other.policy_details.get("enabled")
    assert rid in {i.release_id for i in other.result.items}
    store.append_feedback(Feedback(id="mixed", decision_id=other.id, release_id=rid, kind="listened_mixed"))
    assert _reward_scores(store, catalog, request, profile, AS_OF)[1]["reward_labels"] == 0
