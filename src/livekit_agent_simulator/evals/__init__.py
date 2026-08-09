"""LLM evaluation package — judge backends, presets, aggregate."""

from __future__ import annotations

from .aggregate import aggregate_judges
from .presets import PRESETS, list_presets
from .runner import judge_goals, judge_run, judge_run_multi
from .types import JudgmentResult

__all__ = [
    "JudgmentResult",
    "PRESETS",
    "aggregate_judges",
    "judge_goals",
    "judge_run",
    "judge_run_multi",
    "list_presets",
]
