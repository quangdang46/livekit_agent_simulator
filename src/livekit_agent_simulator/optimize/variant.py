"""PromptVariant — a structural persona-prompt mutation, serializable + reviewable.

A variant mutates the composer's STRUCTURE (verbosity band, section order,
extra guardrail lines) — never a target's business strings. It is a small JSON
object so candidates are diffable, re-applicable, and safe to commit under
``.agent-sim/optimized/<name>/prompt.yaml``.
"""

from __future__ import annotations

import yaml
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# The 10 section class names DefaultCallerPolicy composes (prompt_sections.py).
SECTION_NAMES = (
    "Role",
    "Goals",
    "StyleTraits",
    "NaturalSpeech",
    "Constraints",
    "SpeechConditions",
    "Context",
    "ScriptTiming",
    "FirstSpeaker",
    "Guardrails",
)
VALID_VERBOSITY = ("quiet", "natural", "chatty")


@dataclass(frozen=True)
class PromptVariant:
    """One candidate persona-prompt mutation."""

    id: str
    # None = unchanged; else quiet|natural|chatty (the central length knob).
    verbosity: str | None = None
    # Subset/permutation of SECTION_NAMES. Empty = default order.
    section_order: tuple[str, ...] = ()
    # Appended verbatim to GuardrailsSection (generic anti-repetition / anti-role-flip).
    extra_guardrails: tuple[str, ...] = ()
    # section-name → extra rendered lines appended to that section.
    extra_lines: dict[str, list[str]] = field(default_factory=dict)
    parent_id: str | None = None
    description: str = ""


def validate_variant(v: PromptVariant) -> list[str]:
    """Return a list of validation problems (empty = valid)."""
    problems: list[str] = []
    if v.verbosity is not None and v.verbosity not in VALID_VERBOSITY:
        problems.append(
            f"verbosity {v.verbosity!r} must be one of {VALID_VERBOSITY}"
        )
    known = set(SECTION_NAMES)
    for name in v.section_order:
        if name not in known:
            problems.append(f"unknown section {name!r} in section_order")
    if len(set(v.section_order)) != len(v.section_order):
        problems.append("section_order has duplicates")
    for name in v.extra_lines:
        if name not in known:
            problems.append(f"unknown section {name!r} in extra_lines")
    return problems


def variant_to_dict(v: PromptVariant) -> dict[str, Any]:
    data: dict[str, Any] = {"id": v.id}
    if v.verbosity is not None:
        data["verbosity"] = v.verbosity
    if v.section_order:
        data["section_order"] = list(v.section_order)
    if v.extra_guardrails:
        data["extra_guardrails"] = list(v.extra_guardrails)
    if v.extra_lines:
        data["extra_lines"] = v.extra_lines
    if v.parent_id is not None:
        data["parent_id"] = v.parent_id
    if v.description:
        data["description"] = v.description
    return data


def variant_from_dict(data: dict[str, Any]) -> PromptVariant:
    return PromptVariant(
        id=str(data.get("id") or "v"),
        verbosity=data.get("verbosity"),
        section_order=tuple(str(s) for s in (data.get("section_order") or [])),
        extra_guardrails=tuple(str(g) for g in (data.get("extra_guardrails") or [])),
        extra_lines={str(k): list(v) for k, v in (data.get("extra_lines") or {}).items()},
        parent_id=data.get("parent_id"),
        description=str(data.get("description") or ""),
    )


def write_variant(v: PromptVariant, path: Path) -> None:
    path.write_text(
        yaml.safe_dump(variant_to_dict(v), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def load_variant(path: Path | str) -> PromptVariant:
    """Load a PromptVariant from a YAML file (raises ValueError on invalid)."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("optimized prompt artifact must be a mapping")
    v = variant_from_dict(data)
    problems = validate_variant(v)
    if problems:
        raise ValueError("invalid optimized prompt artifact: " + "; ".join(problems))
    return v
