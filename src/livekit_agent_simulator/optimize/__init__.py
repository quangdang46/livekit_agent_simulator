"""Offline persona-prompt optimizer (DSPy-style live benchmark).

``lks optimize`` generates candidate persona-prompt variants (structural
mutations of the composer), scores them against an eval dataset of scenarios by
re-running them live, and writes the winner as a versioned artifact under
``.agent-sim/optimized/<name>/``. The optimizer is a dev tool — it reuses the
existing judge backend for LLM candidate generation and is never a runtime
dependency. Runtime applies a saved artifact via ``Scenario.caller_policy``
(the same seam the optimizer uses internally for every candidate eval).
"""

from __future__ import annotations

from .variant import PromptVariant, load_variant, validate_variant
from .apply import policy_for_variant
from .mutate import baseline_variant, mutate_verbosity, reorder_sections, add_guardrail, deterministic_candidates
from .eval import evaluate_variant, dataset_pass_rate, select_winner
from .gen import propose_candidates
from .optimize import optimize_persona

__all__ = [
    "PromptVariant",
    "load_variant",
    "validate_variant",
    "policy_for_variant",
    "baseline_variant",
    "mutate_verbosity",
    "reorder_sections",
    "add_guardrail",
    "deterministic_candidates",
    "evaluate_variant",
    "dataset_pass_rate",
    "select_winner",
    "propose_candidates",
    "optimize_persona",
]
