from collections import defaultdict
from dataclasses import replace
from random import Random
from statistics import mean, stdev

from .environment import DiscoveryEnvironment, ListenerConfig


def train_q_learning(config: ListenerConfig, *, seed: int = 17, episodes: int = 4000,
                     alpha: float = .15, gamma: float = 1.0, epsilon: float = .2):
    if episodes < 1 or not 0 < alpha <= 1 or not 0 <= gamma <= 1 or not 0 <= epsilon <= 1:
        raise ValueError("Invalid learning configuration")
    rng = Random(seed)
    env = DiscoveryEnvironment(config)
    q = defaultdict(lambda: [0.0, 0.0])
    curve = []
    for episode in range(episodes):
        state, _ = env.reset(seed=rng.randrange(2**31))
        total = 0.0
        while True:
            action = rng.randrange(2) if rng.random() < epsilon else max(range(2), key=lambda a: q[state][a])
            next_state, reward, done, _, _ = env.step(action)
            target = reward if done else reward+gamma*max(q[next_state])
            q[state][action] += alpha*(target-q[state][action])
            total += reward
            state = next_state
            if done:
                break
        if episode % max(1, episodes//20) == 0:
            curve.append({"episode": episode, "exploratory_training_return": total})
    return dict(q), curve


def _rollout(config, policy, seed):
    env = DiscoveryEnvironment(config)
    state, _ = env.reset(seed=seed)
    total = 0.0
    while True:
        action = policy(state, env)
        state, reward, done, _, _ = env.step(action)
        total += reward
        if done:
            return total


def _train_contextual_bandit(config, seed, episodes):
    """Tabular immediate-reward learner with same state, no future-value target."""
    values, _ = train_q_learning(config, seed=seed, episodes=episodes, gamma=0.0)
    return values


def evaluate(*, seeds: list[int] | None = None, episodes: int = 4000,
             evaluation_episodes: int = 100, genre_scope_id: str = "hip-hop") -> dict:
    seeds = [17, 29, 43] if seeds is None else seeds
    if not seeds or evaluation_episodes < 1:
        raise ValueError("Training seeds and evaluation episodes required")
    listeners = {
        "curious": ListenerConfig(genre_scope_id=genre_scope_id),
        "cautious": ListenerConfig(genre_scope_id=genre_scope_id, exploration_cost=2,
                                    adaptation_gain=.8, terminal_discovery_bonus=1),
        "no_adaptation": ListenerConfig(genre_scope_id=genre_scope_id, adaptation_gain=0,
                                         terminal_discovery_bonus=0, fatigue_step=.1),
    }
    reports = {}
    for name, config in listeners.items():
        # A held-out listener variant tests assumptions, not just fresh reward noise.
        stress = replace(config, adaptation_gain=config.adaptation_gain*.5,
                         exploration_cost=config.exploration_cost*1.5)
        scores = {policy: [] for policy in ("q_learning", "greedy", "contextual_bandit", "random")}
        stress_returns, curves = [], []
        for seed in seeds:
            q, curve = train_q_learning(config, seed=seed, episodes=episodes)
            bandit = _train_contextual_bandit(config, seed+100, episodes)
            curves.append({"seed": seed, "points": curve})
            values = {key: [] for key in scores}
            stress_values = []
            for ep in range(evaluation_episodes):
                heldout_seed = 10_000_000 + seed*10000+ep
                random_policy_rng = Random(heldout_seed+1)
                policies = {
                    "q_learning": lambda state, env: max(range(2), key=lambda a: q.get(state, [0, 0])[a]),
                    "greedy": lambda state, env: max(range(2), key=lambda a: env.expected_transition(state, a)[1]),
                    "contextual_bandit": lambda state, env: max(range(2), key=lambda a: bandit.get(state, [0, 0])[a]),
                    "random": lambda state, env: random_policy_rng.randrange(2),
                }
                for label, policy in policies.items():
                    values[label].append(_rollout(config, policy, heldout_seed))
                stress_values.append(_rollout(stress, policies["q_learning"], heldout_seed))
            for policy in scores:
                scores[policy].append(mean(values[policy]))
            stress_returns.append(mean(stress_values))
        reports[name] = {
            "environment": DiscoveryEnvironment(config).manifest(),
            "policy_returns": {policy: {"mean": mean(returns), "std_across_training_seeds": stdev(returns) if len(returns)>1 else 0,
                                        "per_seed": returns} for policy, returns in scores.items()},
            "heldout_listener_q_mean": mean(stress_returns), "learning_curves": curves,
        }
    return {"status": "simulator_only", "real_user_efficacy": "not_evaluated", "training_seeds": seeds,
            "evaluation_seed_range": "10000000 + training_seed*10000 + episode", "listeners": reports,
            "limitations": ["Hand-designed reward can be exploited; never promote from simulator scores",
                            "Real-user session feedback is required to validate transitions and rewards",
                            "Finite simulator directions are not real catalog releases"]}
