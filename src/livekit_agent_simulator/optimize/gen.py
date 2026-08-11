"""Candidate generation — deterministic operators + LLM-proposed variants.

The LLM proposer sees only the composed system-instruction STRUCTURE (never
target business facts) and returns a JSON array of ``PromptVariant`` mutations.
The deterministic set is always included so a run never depends on the LLM.
"""

from __future__ import annotations

import json
from typing import Any

from ._backend import OptimizeProposer
from .mutate import deterministic_candidates
from .variant import PromptVariant, validate_variant, variant_from_dict

_PROPOSER_SYSTEM = """You are a prompt-optimization assistant for a simulated-caller
persona-prompt composer. You propose SMALL, STRUCTURAL mutations to improve how
naturally the simulated human caller pursues their goals.

You may change:
- verbosity: "quiet" | "natural" | "chatty" (the utterance-length band)
- section_order: a reordering/subset of these section names:
  Role, Goals, StyleTraits, NaturalSpeech, Constraints, SpeechConditions,
  Context, ScriptTiming, FirstSpeaker, Guardrails
- extra_guardrails: short generic lines appended to the guardrails block
- extra_lines: {section_name: [lines]} appended to a named section

You must NOT invent business facts, phone numbers, or goal text. Mutate structure
only. Respond with a JSON array of variant objects, each like:
{"id":"...","description":"...","verbosity":"chatty"}
or {"id":"...","description":"...","extra_guardrails":["..."]}."""


def _parse_candidates(text: str) -> list[PromptVariant]:
    """Tolerant parse of the proposer's JSON array into PromptVariant objects."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("```", 2)[1] if "```" in stripped[3:] else stripped
        stripped = stripped.split("```")[0] if "```" in stripped else stripped
    start = stripped.find("[")
    end = stripped.rfind("]")
    if start < 0 or end < 0 or end <= start:
        return []
    try:
        raw = json.loads(stripped[start : end + 1])
    except json.JSONDecodeError:
        return []
    if not isinstance(raw, list):
        return []
    out: list[PromptVariant] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        item.setdefault("id", f"llm-{i}")
        try:
            v = variant_from_dict(item)
        except Exception:
            continue
        if not validate_variant(v):
            out.append(v)
    return out


async def propose_candidates(
    proposer: OptimizeProposer,
    *,
    current_instruction: str,
    max_candidates: int = 6,
) -> list[PromptVariant]:
    """Deterministic set + LLM-proposed variants, deduped by id, capped."""
    seen: dict[str, PromptVariant] = {}
    for v in deterministic_candidates():
        seen[v.id] = v

    llm_count = max(0, max_candidates - len(seen))
    if llm_count > 0:
        user = (
            "Here is the current composed caller instruction. Propose up to "
            f"{llm_count} distinct structural mutations (JSON array):\n\n"
            f"---\n{current_instruction}\n---"
        )
        try:
            text = await proposer.propose(system=_PROPOSER_SYSTEM, user=user)
            for v in _parse_candidates(text):
                if v.id not in seen and len(seen) < max_candidates:
                    seen[v.id] = v
        except Exception:
            # Never fail the run on proposer noise — deterministic set stands.
            pass

    return list(seen.values())
