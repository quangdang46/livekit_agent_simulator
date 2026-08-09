"""Gates for Script hang_up so timed silence does not end an open dialogue."""

from __future__ import annotations

# Closing / farewell cues: agent is wrapping up — hang_up may proceed.
_CLOSING_MARKERS = (
    "goodbye",
    "good bye",
    "bye for now",
    "bye.",
    "bye!",
    "have a great",
    "have a good",
    "take care",
    "thank you for calling",
    "thanks for calling",
    "call ended",
    "hanging up",
)

# Agent still collecting info / prompting the caller.
_OPEN_PROMPT_MARKERS = (
    "what's your",
    "what is your",
    "what was your",
    "which car",
    "what sort of",
    "may i have",
    "can i have",
    "could you",
    "can you tell",
    "can you give",
    "can you hear",
    "still there",
    "are you there",
    "please provide",
    "please tell",
    "your name",
    "full name",
    "phone number",
    "email address",
    "card number",
    "how can i help",
    "anything else",
    "shall we",
    "would you like",
    "do you want",
    "are you ready",
    "whereabouts",
)


def agent_left_open_turn(text: str | None) -> bool:
    """True when the last agent final still expects a caller reply.

    Heuristic only — used to defer Script hang_up, not to ban barge-in.
    Explicit silent / ghost hang scenarios can disable the gate on the step.
    """
    if not text:
        return False
    t = text.strip()
    if not t:
        return False
    lower = t.lower()
    if any(m in lower for m in _CLOSING_MARKERS):
        return False
    # Mid-utterance questions ("Which car…? I'll check…") still expect a reply.
    if "?" in t:
        return True
    return any(m in lower for m in _OPEN_PROMPT_MARKERS)
