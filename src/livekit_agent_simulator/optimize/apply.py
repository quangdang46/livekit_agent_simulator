"""Apply a PromptVariant → a DefaultCallerPolicy (the runtime + optimizer seam).

``policy_for_variant`` rebuilds the section list from ``build_default_sections()``,
applies the variant's reorder / extra-guardrail / extra-line mutations, and returns
a ``DefaultCallerPolicy`` that composes byte-identically to the default when the
variant is a no-op (pinned by a unit test).
"""

from __future__ import annotations

from .variant import PromptVariant
from ..caller.default_policy import DefaultCallerPolicy
from ..caller.prompt_sections import (
    GuardrailsSection,
    build_default_sections,
)


def _extra_for(v: PromptVariant, section_name: str) -> list[str]:
    return list(v.extra_lines.get(section_name) or [])


def apply_variant_to_persona(persona: dict, v: PromptVariant | None) -> dict:
    """Apply a variant's persona-level knobs (verbosity) to a persona dict copy.

    Returns a NEW dict; the original is untouched. Structural knobs (verbosity)
    override the persona's ``speech_conditions.verbosity`` so the composer's
    ``resolved_verbosity()`` honors the variant.
    """
    out = dict(persona or {})
    if v is not None and v.verbosity is not None:
        sc = dict(out.get("speech_conditions") or out.get("speechConditions") or {})
        sc["verbosity"] = v.verbosity
        out["speech_conditions"] = sc
    return out


def policy_for_variant(v: PromptVariant | None) -> DefaultCallerPolicy:
    """Build a DefaultCallerPolicy for a variant (None → builtin)."""
    if v is None:
        return DefaultCallerPolicy()
    default = build_default_sections()
    if v.section_order:
        by_name = {type(c).__name__: c for c in default}
        ordered = [
            by_name[name] for name in v.section_order if name in by_name
        ]
        # Append any default sections not listed (subset semantics).
        ordered += [c for c in default if type(c).__name__ not in v.section_order]
    else:
        ordered = list(default)

    sections: list[object] = []
    for sec in ordered:
        name = type(sec).__name__
        if isinstance(sec, GuardrailsSection) and (
            v.extra_guardrails or _extra_for(v, name)
        ):
            extras = list(v.extra_guardrails) + _extra_for(v, name)
            sec = GuardrailsSection(extra_lines=extras)
        elif _extra_for(v, name):
            # Generic sections have no ctor param; skip extra injection (only
            # GuardrailsSection supports it today). Validation warns for others.
            pass
        sections.append(sec)

    return DefaultCallerPolicy(
        list(sections),  # type: ignore[arg-type]
        policy_source=f"variant:{v.id}",
    )
