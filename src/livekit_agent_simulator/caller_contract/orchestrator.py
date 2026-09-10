"""P0-4: Deterministic Orchestrator + Turn Detector + Behavior Evaluator.

WHEN layer. Pure Python, no LiveKit SDK import here — this module consumes
only abstract timestamped events (agent audio started/stopped, transcript,
explicit interrupt signals) so it can be unit-tested without a real room,
and so no logic is ever injected into the LiveKit SDK itself (report §22).
A thin adapter in livekit/ (existing package) is expected to translate real
LiveKit room callbacks into calls on TurnDetector/Orchestrator; that
adapter is out of scope for this module.

Three responsibilities, kept as separate classes on purpose (report §21.3 /
§28.5(25) — Behavior Evaluator must never be conflated with the Caller
Contract Validator, they answer different questions):

  TurnDetector      -> WHEN did the agent finish talking? (audio-level)
  BehaviorEvaluator  -> did the agent SATISFY the current behavior? (semantic)
  Orchestrator       -> owns turn/behavior/call timeouts, generation
                        identity (staleness), and the say/do turn gate.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from enum import Enum

from . import BehaviorContract, EvaluatorVerdict, GenerationIdentity, is_current


def _now_ms() -> int:
    return int(time.monotonic() * 1000)


# ---------------------------------------------------------------------------
# TurnDetector: audio-level turn completion (agent_audio_stopped != semantic
# turn complete — report §22.2/§28.2(2)).
# ---------------------------------------------------------------------------


class TurnState(str, Enum):
    WAITING_FOR_AGENT = "WAITING_FOR_AGENT"
    AGENT_SPEAKING = "AGENT_SPEAKING"
    POSSIBLE_END = "POSSIBLE_END"  # audio stopped, still inside the debounce window
    AGENT_TURN_COMPLETE = "AGENT_TURN_COMPLETE"
    CALLER_TURN = "CALLER_TURN"


@dataclass
class TurnDetector:
    """Converts raw agent-audio start/stop signals into TurnState.

    A single logical agent turn may be delivered as multiple audio chunks
    (report §28.2(3)); as long as a new chunk starts before the silence
    debounce elapses, it is correlated into the SAME turn (chunk_count
    increments, turn does not advance to AGENT_TURN_COMPLETE).
    """

    silence_debounce_ms: int = 300
    state: TurnState = TurnState.WAITING_FOR_AGENT
    chunk_count: int = 0
    _last_stop_ms: int | None = field(default=None, repr=False)
    intentional_interrupt: bool = False

    def on_agent_audio_started(self, now_ms: int | None = None) -> None:
        now_ms = _now_ms() if now_ms is None else now_ms
        if self.state == TurnState.CALLER_TURN:
            # Agent started speaking while it was the caller's turn: this is
            # either an intentional agent-side interruption (barge-in) or a
            # transport glitch. Only an explicit signal (see
            # mark_intentional_interrupt) distinguishes the two; absent
            # that signal we conservatively treat it as unintentional so we
            # do not silently steal the caller's turn.
            pass
        self.state = TurnState.AGENT_SPEAKING
        self.chunk_count += 1
        self._last_stop_ms = None

    def on_agent_audio_stopped(self, now_ms: int | None = None) -> None:
        now_ms = _now_ms() if now_ms is None else now_ms
        if self.state == TurnState.AGENT_SPEAKING:
            self.state = TurnState.POSSIBLE_END
            self._last_stop_ms = now_ms

    def mark_intentional_interrupt(self) -> None:
        """Explicit signal (e.g. a room data message) that the agent
        deliberately interrupted, as opposed to a transport/audio glitch
        (report §28.2(4))."""
        self.intentional_interrupt = True

    def poll(self, now_ms: int | None = None) -> TurnState:
        """Advance POSSIBLE_END -> AGENT_TURN_COMPLETE once the debounce
        window has elapsed with no new audio chunk."""
        now_ms = _now_ms() if now_ms is None else now_ms
        if self.state == TurnState.POSSIBLE_END and self._last_stop_ms is not None:
            if now_ms - self._last_stop_ms >= self.silence_debounce_ms:
                self.state = TurnState.AGENT_TURN_COMPLETE
        return self.state

    def begin_caller_turn(self) -> None:
        self.state = TurnState.CALLER_TURN
        self.chunk_count = 0
        self.intentional_interrupt = False

    def reset_for_next_agent_turn(self) -> None:
        self.state = TurnState.WAITING_FOR_AGENT
        self.chunk_count = 0
        self._last_stop_ms = None
        self.intentional_interrupt = False


# ---------------------------------------------------------------------------
# Agent silence: four-way split (report §28.1(1)).
# ---------------------------------------------------------------------------


class AgentSilenceOutcome(str, Enum):
    PROCESSING = "PROCESSING"  # still inside grace period; keep waiting
    TIMEOUT = "TIMEOUT"  # no response before turn_timeout -> AGENT_TIMEOUT
    HANGUP = "HANGUP"  # agent explicitly ended the call
    TRANSPORT_ERROR = "TRANSPORT_ERROR"  # track/room lost, not agent's choice


def classify_agent_silence(
    *,
    elapsed_ms: int,
    turn_timeout_ms: int,
    agent_hung_up: bool,
    transport_lost: bool,
) -> AgentSilenceOutcome:
    """Classify an agent-not-speaking-yet situation into exactly one of
    four outcomes, never conflating them (report §28.1(1))."""
    if transport_lost:
        return AgentSilenceOutcome.TRANSPORT_ERROR
    if agent_hung_up:
        return AgentSilenceOutcome.HANGUP
    if elapsed_ms >= turn_timeout_ms:
        return AgentSilenceOutcome.TIMEOUT
    return AgentSilenceOutcome.PROCESSING


# ---------------------------------------------------------------------------
# BehaviorEvaluator: did the AGENT satisfy the behavior? Separate module
# from the Caller Contract Validator (report §28.5(25)) — different
# question, different mechanism.
# ---------------------------------------------------------------------------

_SATISFIED_PATTERNS = (
    "sure",
    "works for me",
    "works perfectly",
    "that works",
    "okay, that's",
    "yes, that's fine",
    "sounds good",
    "i can do",
    "we can do",
)
_PARTIAL_PATTERNS = (
    "probably",
    "might work",
    "let me check",
    "i'll try",
    "possibly",
    "not sure",
)


class BehaviorEvaluator:
    """Rule-based baseline: did the agent's response satisfy the current
    behavior? Kept intentionally simple/deterministic here; a stronger
    semantic backend can be swapped in later without changing callers,
    same spirit as SemanticVerifierProtocol in validator.py.
    """

    def evaluate(self, contract: BehaviorContract, agent_text: str) -> EvaluatorVerdict:
        text = agent_text.lower()
        if any(p in text for p in _SATISFIED_PATTERNS):
            return EvaluatorVerdict.SATISFIED
        if any(p in text for p in _PARTIAL_PATTERNS):
            return EvaluatorVerdict.PARTIAL
        # A bare price quote satisfies an `ask`/`negotiate` price behavior.
        if contract.target == "price" and re.search(r"\$\s?\d", text):
            return EvaluatorVerdict.SATISFIED
        return EvaluatorVerdict.NOT_SATISFIED


# ---------------------------------------------------------------------------
# Orchestrator: owns generation identity (staleness), timeout hierarchy,
# max_turns policy, and the say/do turn gate.
# ---------------------------------------------------------------------------


@dataclass
class TimeoutConfig:
    turn_timeout_ms: int = 10_000
    behavior_timeout_ms: int = 60_000
    call_timeout_ms: int = 600_000


class BehaviorOutcome(str, Enum):
    SATISFIED = "SATISFIED"
    CONTINUE = "CONTINUE"
    FAILED_MAX_TURNS = "FAILED_MAX_TURNS"


@dataclass
class Orchestrator:
    """Deterministic state owner. No AI/LLM call happens inside this class.

    Generation identity: behavior_id/turn_id/generation_id are bumped here
    and ONLY here; every candidate/audio/action elsewhere must carry the
    identity returned by `current_identity()` at the moment it was created,
    and must be checked with `is_stale()` immediately before publish.
    """

    timeouts: TimeoutConfig = field(default_factory=TimeoutConfig)
    turn_detector: TurnDetector = field(default_factory=TurnDetector)
    evaluator: BehaviorEvaluator = field(default_factory=BehaviorEvaluator)

    _behavior_seq: int = field(default=0, repr=False)
    _turn_id: int = field(default=0, repr=False)
    _generation_id: int = field(default=0, repr=False)
    _behavior_id: str = field(default="b0", repr=False)
    _behavior_turn_count: int = field(default=0, repr=False)

    def start_behavior(self) -> str:
        """Bump to a new behavior_id and reset per-behavior turn counting."""
        self._behavior_seq += 1
        self._behavior_id = f"b{self._behavior_seq}"
        self._behavior_turn_count = 0
        return self._behavior_id

    def current_identity(self) -> GenerationIdentity:
        return GenerationIdentity(
            behavior_id=self._behavior_id,
            turn_id=self._turn_id,
            generation_id=self._generation_id,
        )

    def new_generation(self) -> GenerationIdentity:
        """Call once per attempt to generate a candidate (including retries)."""
        self._generation_id += 1
        return self.current_identity()

    def is_stale(self, identity: GenerationIdentity) -> bool:
        """True when `identity` no longer matches the orchestrator's current
        state — such an action must be dropped, never published (report
        §28.1(5)(15)(16)/§29.2 invariant 2)."""
        return not is_current(identity, self.current_identity())

    def advance_caller_turn(self) -> None:
        """Called once per caller turn (say or do), BEFORE generation.

        This is the gate every action — including `say` — must pass
        through: parser output alone is never enough to publish audio
        (report §28.5(17)); crossing this gate is what it means to be
        "the caller's turn" in the Orchestrator's eyes.

        NOTE: this deliberately does NOT touch _behavior_turn_count. The
        behavior turn budget (check_max_turns) counts only turns published
        INSIDE the current behavior, advanced explicitly via
        advance_behavior_turn() — never the say:/action-level gate, which
        would otherwise let an opening say: steal one turn from the first
        behavior's budget (run 016-017 class).
        """
        self._turn_id += 1
        self._generation_id = 0
        self.turn_detector.begin_caller_turn()

    def advance_behavior_turn(self) -> None:
        """Record one published caller turn inside the current behavior.

        Called by ContractCallerDriver once per actually-published do: turn
        (after sink.publish confirms). start_behavior() resets the count to
        zero, so each behavior owns its full max_turns budget.
        """
        self._behavior_turn_count += 1

    def gate_say(self, has_crossed_turn_gate: bool) -> None:
        """Explicit assertion helper: `say` must still go through
        advance_caller_turn() — never a parser-direct publish."""
        if not has_crossed_turn_gate:
            raise RuntimeError(
                "say action attempted to publish without crossing the Orchestrator turn gate"
            )

    def check_max_turns(self, contract: BehaviorContract) -> BehaviorOutcome:
        """Default policy: hitting max_turns without satisfaction FAILS the
        run (report §28.4(13)) — never silently skip to the next behavior,
        that would make the run result a false positive."""
        if self._behavior_turn_count >= contract.constraints.max_turns:
            return BehaviorOutcome.FAILED_MAX_TURNS
        return BehaviorOutcome.CONTINUE

    def evaluate_behavior(self, contract: BehaviorContract, agent_text: str) -> EvaluatorVerdict:
        return self.evaluator.evaluate(contract, agent_text)

    def classify_silence(
        self, *, elapsed_ms: int, agent_hung_up: bool = False, transport_lost: bool = False
    ) -> AgentSilenceOutcome:
        return classify_agent_silence(
            elapsed_ms=elapsed_ms,
            turn_timeout_ms=self.timeouts.turn_timeout_ms,
            agent_hung_up=agent_hung_up,
            transport_lost=transport_lost,
        )


__all__ = [
    "AgentSilenceOutcome",
    "BehaviorEvaluator",
    "BehaviorOutcome",
    "Orchestrator",
    "TimeoutConfig",
    "TurnDetector",
    "TurnState",
    "classify_agent_silence",
]
