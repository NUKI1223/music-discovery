"""Deterministic, genre-scoped content recommendations."""
from .baseline import exploration_candidates, rank, score_candidates

__all__ = ["rank", "score_candidates", "exploration_candidates"]
