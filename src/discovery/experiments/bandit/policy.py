"""Exactly logged epsilon-greedy selection for the fifth recommendation.

The caller must persist the returned result/details atomically and idempotently
before showing it. An agent must never pick the best of multiple sampled slates.
"""
import json
from dataclasses import dataclass, field
from datetime import date
from hashlib import sha256
from math import exp, isfinite
from random import Random

from discovery.contracts import Catalog, RankingResult, RequestIntent, UserProfile
from discovery.ranking import exploration_candidates, rank, score_candidates
from discovery.ranking.baseline import counts_toward_popularity_limit


def distribution(scores: dict[str, float], epsilon: float = .15) -> dict[str, float]:
    if not 0 <= epsilon <= 1 or not scores or not all(isfinite(v) for v in scores.values()):
        raise ValueError("Nonempty finite scores and epsilon in [0,1] required")
    best = min(scores, key=lambda key: (-scores[key], key))
    n = len(scores)
    return {key: epsilon/n + (1-epsilon if key == best else 0) for key in sorted(scores)}


def sample(probabilities: dict[str, float], seed: int) -> str:
    if not probabilities or abs(sum(probabilities.values())-1) > 1e-9 or any(
        not isfinite(p) or p < 0 for p in probabilities.values()
    ):
        raise ValueError("Invalid probability distribution")
    threshold = Random(seed).random()
    cumulative = 0.0
    for key in sorted(probabilities):
        cumulative += probabilities[key]
        if threshold < cumulative:
            return key
    return max(key for key, value in probabilities.items() if value > 0)


def build_slate(catalog: Catalog, request: RequestIntent, profile: UserProfile, *,
                seed: int, epsilon: float = .15, as_of: date | None = None,
                score_overrides: dict[str, float] | None = None,
                artifact: dict | None = None) -> tuple[RankingResult, dict]:
    today = as_of or date.today()
    baseline = rank(catalog, request, profile, today, artifact=artifact)
    if len(baseline.items) != 5 or baseline.profile_basis == "unavailable":
        return baseline, {"enabled": False, "reason": "five_items_and_scoped_profile_required"}
    scored = score_candidates(catalog, request, profile, today, artifact=artifact)
    prefix = baseline.items[:4]
    release_map = {r.id: r for r in catalog.releases}
    prefix_ids = [r.release_id for r in prefix]
    used_artists = set().union(*(set(release_map[r].artist_ids) for r in prefix_ids))
    pool = [r for r in exploration_candidates(scored, request) if r.release_id not in prefix_ids
            and not set(release_map[r.release_id].artist_ids) & used_artists
            and (not profile.limit_widely_known_artists
                 or sum(counts_toward_popularity_limit(release_map[item.release_id], request, profile)
                        for item in [*prefix, r]) <= profile.max_widely_known_artists)]
    if not pool:
        return baseline, {"enabled": False, "reason": "empty_experimental_pool"}
    scores = {r.release_id: (score_overrides or {}).get(r.release_id, r.score) for r in pool}
    probabilities = distribution(scores, epsilon)
    selected_id = sample(probabilities, seed)
    selected = next(r for r in pool if r.release_id == selected_id)
    result = RankingResult(items=[*prefix, selected], warnings=baseline.warnings,
                           profile_basis=baseline.profile_basis, candidate_ids=baseline.candidate_ids)
    context = {"genre_scope_id": request.genre_scope_id, "taxonomy_version": catalog.taxonomy_version,
               "catalog_snapshot_id": catalog.snapshot_id, "profile": profile.model_dump(mode="json"),
               "request": request.model_dump(mode="json"), "as_of": today.isoformat(),
               "prefix_release_ids": prefix_ids, "eligible_candidate_ids": sorted(scores)}
    if any("ranking_model_version" in item.components for item in scored.items):
        context["ranking_model_version"] = next(item.components["ranking_model_version"] for item in scored.items
                                                if "ranking_model_version" in item.components)
    context_id = sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()
    details = {"enabled": True, "policy_version": "one-slot-epsilon-v1", "context": context,
               "context_id": context_id, "prefix_release_ids": prefix_ids, "slot_index": 4,
               "candidate_ids": sorted(scores), "scores": scores, "probabilities": probabilities,
               "selected_release_id": selected_id, "chosen_probability": probabilities[selected_id],
               "epsilon": epsilon, "rng_seed": seed}
    return result, details


@dataclass
class LinearRewardModel:
    """Small experimental online logistic learner, never learns from no-response.

    Features must be the frozen context × item features from the shown decision.
    Corrections must rebuild from the event projection, not apply a second reward.
    Model state and consumed event IDs must be committed together by the caller.
    """
    weights: dict[str, float] = field(default_factory=dict)
    bias: float = 0.0
    consumed_event_ids: set[str] = field(default_factory=set)
    learning_rate: float = .05

    def predict(self, features: dict[str, float]) -> float:
        if not all(isfinite(value) for value in features.values()):
            raise ValueError("Finite features required")
        value = self.bias + sum(self.weights.get(key, 0)*value for key, value in features.items())
        return 1/(1+exp(-value)) if value >= 0 else exp(value)/(1+exp(value))

    def observe(self, event_id: str, features: dict[str, float], reward: int | None) -> bool:
        if reward is None or event_id in self.consumed_event_ids:
            return False
        if reward not in (0, 1):
            raise ValueError("Reward must be an explicit listened like/dislike")
        error = reward-self.predict(features)
        self.bias += self.learning_rate*error
        for key, value in features.items():
            self.weights[key] = self.weights.get(key, 0) + self.learning_rate*error*value
        self.consumed_event_ids.add(event_id)
        return True


def evaluate_ope(observations: list[dict], target_by_context: dict[str, dict[str, float]], *,
                 minimum_ess: float = 20) -> dict:
    """Descriptive IPS/SNIPS on a fixed-context single-slot target.

    Each observation is {policy_details: ..., reward: 0|1|None}. Missing rewards
    are reported, never imputed. The report does not claim an unbiased estimate
    of all shown recommendations when feedback is selectively missing.
    """
    weighted_rewards, weights = [], []
    missing = 0
    for observation in observations:
        details = observation["policy_details"]
        if not details.get("enabled"):
            return {"status": "unsupported", "reason": "Not a logged single-slot decision"}
        target = target_by_context.get(details["context_id"])
        behavior = details["probabilities"]
        if (not behavior or abs(sum(behavior.values())-1) > 1e-9
            or any(not isfinite(p) or p < 0 for p in behavior.values())):
            return {"status": "unsupported", "reason": "Corrupt behavior distribution"}
        if target is None or set(target) != set(behavior):
            return {"status": "unsupported", "reason": "Target changes context/prefix/eligible set"}
        if (abs(sum(target.values())-1) > 1e-9 or any(not isfinite(p) or p < 0 for p in target.values())
            or any(target[a] > 0 and behavior[a] <= 0 for a in target)):
            return {"status": "unsupported", "reason": "Invalid target or missing action support"}
        selected = details["selected_release_id"]
        logged = details["chosen_probability"]
        if selected not in behavior or logged <= 0 or abs(logged-behavior[selected]) > 1e-9:
            return {"status": "unsupported", "reason": "Corrupt propensity log"}
        reward = observation.get("reward")
        if reward is None:
            missing += 1
            continue
        if reward not in (0, 1):
            raise ValueError("Unexpected reward")
        weight = target[selected]/logged
        weights.append(weight)
        weighted_rewards.append(weight*reward)
    total_weight = sum(weights)
    sum_squared = sum(w*w for w in weights)
    ess = total_weight**2/sum_squared if sum_squared else 0
    report = {"status": "descriptive" if ess >= minimum_ess else "insufficient_data",
              "shown": len(observations), "labeled": len(weights), "missing": missing,
              "effective_sample_size": ess, "max_weight": max(weights, default=0),
              "ips": sum(weighted_rewards)/len(weights) if weights else None,
              "snips": sum(weighted_rewards)/total_weight if total_weight else None,
              "scope": "observed-feedback only; does not correct selective missing outcomes",
              "promotion_allowed": False}
    return report
