"""Builtin dimensional criteria (Hamming dims + LiveKit-shaped instructions)."""

from __future__ import annotations

from typing import Any

# Keys usable as PassCriteria judges[].builtin or criteria "builtin:<key>"
PRESETS: dict[str, str] = {
    "task_completion": (
        "Task completion: Did the caller's goal finish with the correct final state "
        "(completed, appropriately handed off, or correctly declined)? "
        "Ignore tone; focus on outcome and tool results."
    ),
    "factual_accuracy": (
        "Factual accuracy: Were facts, prices, eligibility, dates, and policy statements "
        "consistent with tool outputs / evidence? Flag contradictions or unsupported claims."
    ),
    "policy_compliance": (
        "Policy and compliance: Were required disclosures, refusals, consent statements, "
        "and restricted topics handled correctly when applicable?"
    ),
    "conversation_flow": (
        "Conversation flow: Did the agent avoid useless loops, ignored context, "
        "and excessive dead-air or overtalk relative to the caller's needs?"
    ),
    "empathy": (
        "Empathy and professionalism: Was tone appropriate for the caller's situation "
        "without replacing task or policy correctness?"
    ),
    "escalation": (
        "Escalation judgment: Did the agent transfer, hand off, or refuse at the right time "
        "given severity and policy?"
    ),
    "accuracy": (
        "Accuracy (LiveKit-style): Verify the agent grounds claims in tool outputs; "
        "catch hallucinations and contradictions with tool results."
    ),
    "coherence": (
        "Coherence (LiveKit-style): Responses follow a logical structure and stay on-topic "
        "across turns without ignoring the caller."
    ),
}


def expand_criterion(item: str) -> str:
    s = str(item).strip()
    if s.startswith("builtin:"):
        key = s[len("builtin:") :].strip()
        if key not in PRESETS:
            raise KeyError(f"Unknown judge builtin preset: {key}")
        return PRESETS[key]
    return s


def expand_criteria(items: list[str]) -> list[str]:
    return [expand_criterion(c) for c in items]


def expand_judge_group(group: dict[str, Any]) -> dict[str, Any]:
    """Return a copy with criteria expanded from builtin / builtin: keys."""
    out = dict(group)
    builtin = group.get("builtin")
    criteria = list(group.get("criteria") or [])
    if builtin:
        key = str(builtin).strip()
        if key not in PRESETS:
            raise KeyError(f"Unknown judge builtin preset: {key}")
        criteria = [PRESETS[key], *expand_criteria([str(c) for c in criteria])]
    else:
        criteria = expand_criteria([str(c) for c in criteria])
    out["criteria"] = criteria
    return out


def list_presets() -> list[str]:
    return sorted(PRESETS.keys())
