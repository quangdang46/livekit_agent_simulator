"""P0-5b: Caller Interaction Planner — the DELIVERY layer.

Runs ONLY after Caller Contract Validator PASS (never before — this module
never validates content, it only re-shapes an already-validated utterance
into a delivery plan). Applies deterministic `interaction:` config
(pace/hesitation/stumble/pre_delay) plus routes non-speech actions
(DTMF/silence/hangup) that never go through TTS at all.

Hard invariant (report §28.5(18)): the transform whitelist is enforced BY
CONSTRUCTION. This planner operates on token-level ops (insert a hesitation
token from a fixed allowlist, repeat the first fragment for a stumble
effect, apply a pace hint, apply a pre-delay) that cannot introduce new
semantic content — there is no code path here that accepts or generates
free text. Any change that lets this module append arbitrary words is a
design violation; `verify_semantic_preserving()` exists specifically to
make that violation detectable in tests.

Backchannel / false-interrupt / noise are InteractionActions here, NOT
semantic Behaviors (report §28.2(19)/(20)) — they never touch
BehaviorContract or the Caller Contract Validator.

Barge-in: this module only emits a BARGE_IN_TRIGGER outcome. Execution
(cancelling audio, deciding when the caller may resume) stays with the
Orchestrator (P0-4) — the planner never cancels audio directly.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum

from .dsl import InteractionConfig

# Per-turn interruption probability per rate (legacy interval spirit:
# low ~= rarely, medium ~= sometimes, high ~= often — but expressed as a
# deterministic per-turn coin flip, not a wall-clock timer, so replay and
# unit tests are exact). The once-per-interval wall-clock gate lives in the
# driver alongside the decision (see should_interrupt call-site).
_INTERRUPTION_PROBABILITY = {"low": 0.15, "medium": 0.35, "high": 0.6}

# The ONLY tokens this module is allowed to insert that are not already
# present in the validated utterance. Anything else would be "generating
# text" at the delivery layer, which is exactly what report §28.5(18)
# forbids.
HESITATION_TOKEN_ALLOWLIST = frozenset({"uh,", "um,", "hmm,"})
_DEFAULT_HESITATION_TOKEN = "uh,"
_STUMBLE_SUFFIX = "..."


class InteractionActionKind(str, Enum):
    SPEAK = "SPEAK"
    DTMF = "DTMF"
    SILENCE = "SILENCE"
    HANGUP = "HANGUP"
    BACKCHANNEL = "BACKCHANNEL"
    BARGE_IN_TRIGGER = "BARGE_IN_TRIGGER"


@dataclass
class InteractionOutcome:
    kind: InteractionActionKind
    tokens: list[str] = field(default_factory=list)  # SPEAK / BACKCHANNEL only
    pre_delay_ms: int = 0
    pace: str | None = None
    dtmf_digits: str | None = None


def verify_semantic_preserving(original_utterance: str, tokens: list[str]) -> bool:
    """True iff every token is either a word from the original utterance
    (ignoring an appended stumble '...' suffix) or a hesitation token from
    the allowlist. Used both as an internal guard and directly by tests
    proving the whitelist is enforced by construction."""
    original_words = set(original_utterance.split())
    for tok in tokens:
        if tok in HESITATION_TOKEN_ALLOWLIST:
            continue
        stripped = tok[: -len(_STUMBLE_SUFFIX)] if tok.endswith(_STUMBLE_SUFFIX) else tok
        if stripped not in original_words:
            return False
    return True


class CallerInteractionPlanner:
    """Deterministic delivery-layer planner. No AI, no free-text generation."""

    def plan_speak(self, validated_utterance: str, interaction: InteractionConfig | None) -> InteractionOutcome:
        """Build a speech delivery plan for an utterance that has ALREADY
        passed the Caller Contract Validator."""
        tokens = validated_utterance.split()
        pre_delay_ms = 0
        pace: str | None = None

        if interaction is not None:
            pace = interaction.pace
            pre_delay_ms = interaction.pre_delay_ms or 0

            if interaction.hesitation and interaction.hesitation != "none":
                insert_at = 1 if len(tokens) > 1 else 0
                tokens = tokens[:insert_at] + [_DEFAULT_HESITATION_TOKEN] + tokens[insert_at:]

            if interaction.stumble and interaction.stumble != "none" and tokens:
                # Repeat the first content token as a stumble fragment,
                # e.g. "Could... Could you come down on the price?" — this
                # duplicates an existing word, it never invents a new one.
                first_content = next((t for t in tokens if t not in HESITATION_TOKEN_ALLOWLIST), tokens[0])
                tokens = [first_content + _STUMBLE_SUFFIX] + tokens

        outcome = InteractionOutcome(
            kind=InteractionActionKind.SPEAK, tokens=tokens, pre_delay_ms=pre_delay_ms, pace=pace
        )
        assert verify_semantic_preserving(validated_utterance, outcome.tokens), (
            "Interaction Planner produced a token outside the semantic-preserving "
            "whitelist — this must never happen (report §28.5(18))"
        )
        return outcome

    def plan_dtmf(self, digits: str) -> InteractionOutcome:
        return InteractionOutcome(kind=InteractionActionKind.DTMF, dtmf_digits=digits)

    def plan_silence(self) -> InteractionOutcome:
        return InteractionOutcome(kind=InteractionActionKind.SILENCE)

    def plan_hangup(self) -> InteractionOutcome:
        return InteractionOutcome(kind=InteractionActionKind.HANGUP)

    def plan_backchannel(self, text: str = "uh-huh") -> InteractionOutcome:
        """Backchannel is an InteractionAction, never a semantic Behavior —
        it carries no BehaviorContract and never touches the Caller
        Contract Validator."""
        return InteractionOutcome(kind=InteractionActionKind.BACKCHANNEL, tokens=[text])

    def trigger_barge_in(self) -> InteractionOutcome:
        """Only emits the trigger event. Cancelling audio / deciding when
        the caller may resume is the Orchestrator's job, never this
        planner's (report: 'planner never cancels audio directly')."""
        return InteractionOutcome(kind=InteractionActionKind.BARGE_IN_TRIGGER)

    def should_interrupt(
        self,
        interaction: InteractionConfig | None,
        *,
        scenario_id: str,
        agent_turn_index: int,
    ) -> bool:
        """Seeded interruption policy: True iff the caller should emit a
        fixed backchannel cut-in before this agent turn.

        Deterministic in (scenario_id, seed, agent_turn_index): the same
        scenario + seed + turn always decides the same way — the LLM never
        decides whether to interrupt. ``None`` interaction or no
        ``interruption_rate`` means never. At most one cut-in per interval
        is enforced by the driver (it advances the turn index only after an
        actual agent turn, and the interval gate lives there); this method
        answers only the per-turn coin flip.
        """
        if interaction is None or not interaction.interruption_rate:
            return False
        seed = interaction.interruption_seed or 0
        key = f"{scenario_id}:{seed}:{agent_turn_index}".encode()
        digest = hashlib.sha256(key).digest()
        roll = int.from_bytes(digest[:8], "big") / 2**64
        return roll < _INTERRUPTION_PROBABILITY[interaction.interruption_rate]


__all__ = [
    "HESITATION_TOKEN_ALLOWLIST",
    "CallerInteractionPlanner",
    "InteractionActionKind",
    "InteractionOutcome",
    "verify_semantic_preserving",
]
