"""Application boundary: validated requests, one commit, then feedback."""

from datetime import date
from secrets import randbits
from uuid import uuid4

from discovery.contracts import Catalog, Decision, RequestIntent, UserProfile
from discovery.ranking.baseline import FEATURE_VERSION, RANKER_VERSION
from discovery.storage import Store, catalog_digest


def _reward_scores(store: Store, catalog: Catalog, request: RequestIntent,
                   profile: UserProfile, as_of: date | None) -> tuple[dict | None, dict]:
    """Rebuild the small experimental reward learner from corrected fifth-slot outcomes."""
    from discovery.experiments.bandit import LinearRewardModel
    from discovery.ranking import score_candidates

    decisions = {d.id: d for d in store.decisions()}
    latest = {}
    for event in store.feedback():
        decision = decisions[event.decision_id]
        details = decision.policy_details
        if (decision.request.genre_scope_id == request.genre_scope_id
            and decision.feature_version == FEATURE_VERSION and details.get("enabled")
            and details.get("selected_release_id") == event.release_id
            and event.kind in {"listened_like", "listened_dislike"}):
            latest[event.decision_id, event.release_id] = event
    events = sorted(latest.values(), key=lambda event: event.recorded_at)
    if len(events) < 10 or len({e.kind for e in events}) < 2:
        return None, {"reward_model_basis": "baseline", "reward_labels": len(events)}

    def features(item):
        names = ("similarity", "novelty", "rarity", "quality", "avoidance", "negative_affinity")
        vector = {name: float(item.components.get(name) or 0) for name in names}
        vector["mode=" + str(item.components.get("mode", "unknown"))] = 1.0
        return vector

    model = LinearRewardModel()
    for event in events:
        item = next(i for i in decisions[event.decision_id].result.items
                    if i.release_id == event.release_id)
        model.observe(event.id, features(item), int(event.kind == "listened_like"))
    candidates = score_candidates(catalog, request, profile, as_of=as_of)
    scores = {item.release_id: model.predict(features(item)) for item in candidates.items}
    return scores, {
        "reward_model_basis": "experimental_scoped_logistic", "reward_labels": len(events),
        "reward_event_ids": [e.id for e in events], "reward_weights": model.weights,
        "reward_bias": model.bias,
    }


def recommend(
    store: Store, catalog: Catalog, request: RequestIntent,
    profile: UserProfile | None = None, *, request_id: str | None = None,
    as_of: date | None = None, use_bandit: bool = False, artifact: dict | None = None,
) -> Decision:
    """Recommend with current feedback; as_of only controls catalogue observation freshness.

    This live service is not a historical feedback replay API. Offline evaluation
    must provide its frozen profile and policy context explicitly.
    """
    from discovery.ranking import rank

    request.validate_catalog(catalog)
    request_id = request_id or str(uuid4())
    profile = profile or store.profile()

    def create() -> Decision:
        policy_details = {}
        if use_bandit:
            from discovery.experiments.bandit import build_slate

            scores, reward_details = _reward_scores(store, catalog, request, profile, as_of)
            result, policy_details = build_slate(
                catalog, request, profile, as_of=as_of, seed=randbits(32),
                score_overrides=scores, artifact=artifact,
            )
            policy_details.update(reward_details)
        else:
            result = rank(catalog, request, profile, as_of=as_of, artifact=artifact)
        kwargs = {"as_of": as_of} if as_of is not None else {}
        return Decision(
            id=request_id, request=request, catalog_snapshot_id=catalog.snapshot_id,
            catalog_hash=catalog_digest(catalog), taxonomy_version=catalog.taxonomy_version,
            profile=profile, result=result, policy_details=policy_details,
            ranker_version=next((str(i.components["ranking_model_version"]) for i in result.items
                                 if i.components.get("ranking_model_version")), RANKER_VERSION),
            feature_version=FEATURE_VERSION,
            policy_version=policy_details.get("policy_version", "deterministic-v1"), **kwargs,
        )

    return store.commit_decision(request_id, catalog, request, create)


def discovery_metrics(store: Store) -> dict:
    """Descriptive observed outcomes, not an unbiased policy value estimate."""
    decisions = {d.id: d for d in store.decisions()}
    latest_rating = {}
    familiarity = {}
    for event in store.feedback():
        key = event.decision_id, event.release_id
        if event.kind in {"listened_like", "listened_dislike"}:
            latest_rating[key] = event.kind == "listened_like"
        if event.kind == "known":
            familiarity[key] = True
        if event.kind == "familiarity" or event.familiar_before_recommendation is not None:
            familiarity[key] = event.familiar_before_recommendation
    impressions = store.export()["impressions"]
    by_genre = {}
    for scope in {d.request.genre_scope_id for d in decisions.values()}:
        shown = {(i["decision_id"], i["release_id"]) for i in impressions
                 if decisions[i["decision_id"]].request.genre_scope_id == scope}
        answered = {key for key in shown if familiarity.get(key) is not None}
        rated = {key for key in shown if key in latest_rating}
        complete = answered & rated
        discoveries = sum(familiarity[key] is False and latest_rating[key] for key in complete)
        by_genre[scope] = {
            "shown": len(shown), "rated": len(rated), "complete_outcomes": len(complete),
            "interesting_discoveries": discoveries,
            "discovery_rate": discoveries / len(complete) if complete else None,
            "feedback_coverage": len(rated) / len(shown) if shown else None,
            "familiarity_coverage": len(answered) / len(shown) if shown else None,
            "already_known_rate": (sum(familiarity[k] is True for k in answered) / len(answered)
                                   if answered else None),
        }
    return {"by_genre": by_genre, "interpretation": "observed_outcomes_only; selection_bias_possible"}
