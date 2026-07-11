"""Portable caller trait library — expands short tags into system-prompt instructions.

Scenarios use ``Persona.spec.traits: ["impatient", "quiet", ...]``. Unknown tags are
passed through as free-text behavior hints (no consumer-specific coupling).
"""

from __future__ import annotations

# Canonical trait id → instruction bullets for the sim caller (English; agent speaks in locale).
TRAIT_LIBRARY: dict[str, list[str]] = {
    "polite": [
        "Stay courteous; use soft openers and thank the agent when appropriate.",
    ],
    "impatient": [
        "You are short on time: speak a bit faster, push for a quick answer,",
        "and do not tolerate long monologues.",
    ],
    "interrupts": [
        "You sometimes cut in while the agent is still talking with a brief correction",
        "or urgency (one short phrase). Do not monologue over them.",
    ],
    "quiet": [
        "You are reserved: leave pauses before answering; replies are very short.",
    ],
    "silent": [
        "You often go quiet after the agent speaks; only answer when necessary.",
    ],
    "confused": [
        "You misunderstand details once or twice and ask the agent to repeat simply.",
    ],
    "elderly": [
        "Speak slightly slower and ask the agent to speak clearly; prefer simple words.",
    ],
    "angry": [
        "You are mildly frustrated (not abusive). Express annoyance briefly if the agent",
        "is unclear, but stay on topic.",
    ],
    "fast_speaker": [
        "Speak quickly and denser than average phone speech; still 1–2 sentences max.",
    ],
    "soft_spoken": [
        "Keep volume soft and wording gentle; avoid sharp interruptions.",
    ],
    "non_native": [
        "You are fluent enough but occasionally ask for clarification of complex words.",
    ],
    "skeptical": [
        "Question vague claims; ask for concrete next steps or confirmation.",
    ],
    "chatty": [
        "You add a small extra detail about your situation, but still finish goals.",
    ],
    "backchannel": [
        "Occasionally acknowledge with a very short uh-huh / okay while listening,",
        "without stealing the full turn.",
    ],
    "hangup_threat": [
        "If the agent is unhelpful or loops the menu, you may briefly threaten to hang up,",
        "then give one more chance before ending the call.",
    ],
    "code_switch": [
        "You mainly use the call language but may mix one short English phrase if stuck.",
    ],
    "urgent": [
        "State urgency early; prefer concrete next steps over long explanations.",
    ],
}


def expand_traits(traits: list[str] | tuple[str, ...] | str) -> list[str]:
    """Return prompt lines for known traits + passthrough for unknown tags."""
    if isinstance(traits, str):
        traits = [traits]
    lines: list[str] = []
    seen: set[str] = set()
    unknown: list[str] = []
    for raw in traits:
        tag = str(raw).strip()
        if not tag:
            continue
        key = tag.lower().replace(" ", "_").replace("-", "_")
        if key in seen:
            continue
        seen.add(key)
        if key in TRAIT_LIBRARY:
            lines.extend(TRAIT_LIBRARY[key])
        else:
            unknown.append(tag)
    if unknown:
        lines.append(
            "Additional caller behavior (follow naturally): " + ", ".join(unknown) + "."
        )
    return lines


def list_trait_ids() -> list[str]:
    return sorted(TRAIT_LIBRARY.keys())
