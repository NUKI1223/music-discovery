import gymnasium as gym
import pytest
from gymnasium.utils.env_checker import check_env

from discovery.experiments.sequential_rl import (
    DiscoveryEnvironment,
    ListenerConfig,
    evaluate,
    train_q_learning,
)


def test_gymnasium_environment_contract():
    env = DiscoveryEnvironment(ListenerConfig())
    assert isinstance(env, gym.Env)
    check_env(env, skip_render_check=True)


def test_deterministic_reset_and_terminal_contract():
    env = DiscoveryEnvironment(ListenerConfig(genre_scope_id="jazz", horizon=3))
    env.reset(seed=12)
    one = [env.step(a) for a in [1, 0, 0]]
    env.reset(seed=12)
    two = [env.step(a) for a in [1, 0, 0]]
    assert one == two
    assert two[-1][2] is True
    assert all(step[4]["genre_scope_id"] == "jazz" for step in two)
    with pytest.raises(ValueError, match="reset"):
        env.step(0)


def test_delayed_reward_changes_optimal_first_action():
    config = ListenerConfig(noise=0)
    env = DiscoveryEnvironment(config)
    initial, _ = env.reset()
    assert env.expected_transition(initial, 0)[1] > env.expected_transition(initial, 1)[1]
    q, _ = train_q_learning(config, seed=17, episodes=2000)
    assert q[initial][1] > q[initial][0]


def test_evaluation_is_simulator_only_with_distinct_baselines_and_seeds():
    report = evaluate(seeds=[3, 7], episodes=600, evaluation_episodes=10, genre_scope_id="metal")
    assert report["status"] == "simulator_only"
    assert report["real_user_efficacy"] == "not_evaluated"
    assert set(report["listeners"]) == {"curious", "cautious", "no_adaptation"}
    curious = report["listeners"]["curious"]
    assert set(curious["policy_returns"]) == {"q_learning", "greedy", "contextual_bandit", "random"}
    assert curious["environment"]["listener"]["genre_scope_id"] == "metal"
    assert curious["policy_returns"]["q_learning"]["mean"] > curious["policy_returns"]["greedy"]["mean"]
