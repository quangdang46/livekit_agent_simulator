"""Script domain: step models, log verify, behavior summary.

Public re-exports: ScriptStep, ScriptVerifySpec, SUPPORTED_*,
evaluate_script_log, build_caller_behavior_summary.

NOTE: the legacy ScriptRunner engine (script/runtime.py) was removed —
the contract path (caller_steps → caller_contract driver) is the only
caller path. Only the helpers below (still used by asserts/metrics and
the legacy-verify stub) are kept.
"""

from __future__ import annotations

from .models import (
    INTERRUPTION_CLASSES,
    OVERLAY_ROLES,
    RECOVERY_BARGE_CLASSES,
    SUPPORTED_ACTIONS,
    SUPPORTED_TRIGGERS,
    ScriptStep,
    ScriptVerifySpec,
    counts_for_recovery_barge,
    effective_overlay,
    normalize_interrupt_class,
)
from .summary import build_caller_behavior_summary
from .verify import evaluate_script_log

__all__ = [
    "INTERRUPTION_CLASSES",
    "OVERLAY_ROLES",
    "RECOVERY_BARGE_CLASSES",
    "SUPPORTED_ACTIONS",
    "SUPPORTED_TRIGGERS",
    "ScriptStep",
    "ScriptVerifySpec",
    "build_caller_behavior_summary",
    "counts_for_recovery_barge",
    "effective_overlay",
    "evaluate_script_log",
    "normalize_interrupt_class",
]
