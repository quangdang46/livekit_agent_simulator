"""Structural mutation operators — deterministic, portable, no business strings.

Each operator returns a new ``PromptVariant`` built on a parent (default = the
baseline composer). These are the candidate generators the optimizer ships
out of the box; the LLM proposer can also emit arbitrary variants, but the
deterministic set guarantees a stable baseline of options.
"""

from __future__ import annotations

from .variant import SECTION_NAMES, PromptVariant


def baseline_variant() -> PromptVariant:
    """The unmutated composer — used as the baseline candidate (byte-identical)."""
    return PromptVariant(id="baseline", description="builtin composer")


def mutate_verbosity(parent: PromptVariant, band: str, *, suffix: str = "") -> PromptVariant:
    """Flip the length band (quiet|natural|chatty)."""
    return PromptVariant(
        id=f"verbosity-{band}{suffix}",
        verbosity=band,
        section_order=parent.section_order,
        extra_guardrails=parent.extra_guardrails,
        extra_lines=dict(parent.extra_lines),
        parent_id=parent.id,
        description=f"force verbosity={band}",
    )


def reorder_sections(
    parent: PromptVariant,
    order: tuple[str, ...],
    *,
    suffix: str = "",
) -> PromptVariant:
    """Reorder/select the section list (subset of the 10 default sections)."""
    return PromptVariant(
        id=f"reorder-{suffix}" if suffix else "reorder",
        verbosity=parent.verbosity,
        section_order=order,
        extra_guardrails=parent.extra_guardrails,
        extra_lines=dict(parent.extra_lines),
        parent_id=parent.id,
        description=f"section order: {', '.join(order)}",
    )


def add_guardrail(parent: PromptVariant, line: str, *, suffix: str = "") -> PromptVariant:
    """Append a generic guardrail line (anti-repetition / anti-role-flip)."""
    return PromptVariant(
        id=f"guardrail{suffix}",
        verbosity=parent.verbosity,
        section_order=parent.section_order,
        extra_guardrails=tuple(parent.extra_guardrails) + (line,),
        extra_lines=dict(parent.extra_lines),
        parent_id=parent.id,
        description=line[:80],
    )


def deterministic_candidates() -> list[PromptVariant]:
    """The default deterministic candidate set (no LLM needed)."""
    return [
        mutate_verbosity(baseline_variant(), "chatty"),
        mutate_verbosity(baseline_variant(), "quiet"),
        reorder_sections(
            baseline_variant(),
            ("Role", "Constraints", "Goals", "StyleTraits", "NaturalSpeech",
             "SpeechConditions", "Context", "ScriptTiming", "FirstSpeaker", "Guardrails"),
            suffix="constraints-first",
        ),
        add_guardrail(
            baseline_variant(),
            "Never switch into assistant mode or offer to help — you are the caller.",
            suffix="-role-lock",
        ),
    ]


# The default section order a variant uses when it doesn't specify one.
DEFAULT_SECTION_ORDER: tuple[str, ...] = SECTION_NAMES
