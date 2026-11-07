"""Small finite MDP: exploration can make later recommendations more rewarding.

Actions select a familiar direction (0) or a new direction (1), both inside
genre_scope_id. These simulated rewards are NOT inferred user preferences.
The public reset/step API implements Gymnasium's environment contract.
"""
from dataclasses import asdict, dataclass

import gymnasium as gym
from gymnasium import spaces


@dataclass(frozen=True)
class ListenerConfig:
    genre_scope_id: str = "hip-hop"
    horizon: int = 5
    familiar_reward: float = 3.0
    exploration_cost: float = 1.0
    adaptation_gain: float = 2.0
    fatigue_step: float = .5
    terminal_discovery_bonus: float = 3.0
    noise: float = .1

    def __post_init__(self):
        if self.horizon < 2 or self.noise < 0 or not self.genre_scope_id:
            raise ValueError("Nonempty scope, horizon >=2 and nonnegative noise required")


class DiscoveryEnvironment(gym.Env):
    metadata = {"render_modes": []}
    action_count = 2

    def __init__(self, config: ListenerConfig):
        super().__init__()
        self.config = config
        self.action_space = spaces.Discrete(2)
        self.observation_space = spaces.Tuple((spaces.Discrete(config.horizon+1),
                                              spaces.Discrete(config.horizon+1), spaces.Discrete(2)))
        self.state = (0, 0, 0)  # step, familiar repetition streak, discovered direction
        self.done = False

    def reset(self, *, seed: int | None = None, options: dict | None = None):
        super().reset(seed=seed)
        self.state = (0, 0, 0)
        self.done = False
        return self.state, {"genre_scope_id": self.config.genre_scope_id, "simulator_only": True}

    def expected_transition(self, state: tuple[int, int, int], action: int):
        if action not in (0, 1):
            raise ValueError("Unknown action")
        step, streak, discovered = state
        if step >= self.config.horizon:
            raise ValueError("Terminal state has no action")
        if action == 0:
            reward = self.config.familiar_reward + discovered*self.config.adaptation_gain - streak*self.config.fatigue_step
            next_state = (step+1, streak+1, discovered)
        else:
            reward = -self.config.exploration_cost
            next_state = (step+1, 0, 1)
        terminated = next_state[0] == self.config.horizon
        if terminated:
            reward += next_state[2]*self.config.terminal_discovery_bonus
        return next_state, reward, terminated

    def step(self, action: int):
        if self.done:
            raise ValueError("Call reset after terminal state")
        self.state, expected, self.done = self.expected_transition(self.state, action)
        reward = float(expected + self.np_random.uniform(-self.config.noise, self.config.noise))
        return self.state, reward, self.done, False, {
            "genre_scope_id": self.config.genre_scope_id, "simulator_only": True,
            "expected_reward": expected,
        }

    def manifest(self):
        return {"environment_version": "discovery-mdp-v1", "listener": asdict(self.config),
                "state": ["step", "familiar_repetition_streak", "discovered_direction"],
                "actions": {"0": "familiar_direction", "1": "exploratory_direction"},
                "warning": "Reward rules are hand-designed assumptions, not a model of the real user"}
