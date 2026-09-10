"""Contract Caller Driver — the single caller path (WHAT → WHEN → HOW → gate → publish).

Owns the canonical loop over ``CallerAction`` lists (``dsl.parse_steps``
output) so that every caller utterance reaching LiveKit passes through:

    Orchestrator turn gate (say AND do) → AI Language Adapter (do only) →
    ContractValidator (do only, VALID required) → Interaction Planner →
    TTS (caller-provided synth fn) → PublishSink (staleness-checked publish).

No shortcuts: this driver has no audio-session side effects of its own; the
only way audio leaves is ``sink.publish()``, which drops stale identities.
``say`` bypasses AI + validator but never orchestration (``gate_say``).

Pure Python, no LiveKit import — ``PublishSink`` / ``AgentTurnWait`` are
abstract interfaces so this driver is fully unit-testable; the LiveKit
adapter (bridge mixer) is wired in a later slice.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Protocol

from . import (
    BehaviorContract,
    CandidateUtterance,
    EndedBy,
    EvaluatorVerdict,
    FailureReason,
    GenerationIdentity,
    ValidationResult,
)
from .dsl import CallerAction
from .failures import RunFailure
from .interaction_planner import CallerInteractionPlanner, InteractionConfig
from .language_adapter import (
    AILanguageAdapter,
    LanguageGenerationError,
    Turn,
    build_context,
    should_invoke_adapter,
)
from .orchestrator import BehaviorOutcome, Orchestrator
from .validator import ContractValidator


class PublishDrainTimeout(RuntimeError):
    """Raised by a PublishSink when audio was accepted for publish but did
    not finish draining within the sink's own bounded timeout.

    This is an EXECUTION/TRANSPORT failure, never a caller-content problem —
    the driver maps it to ``FailureReason.TRANSPORT_ERROR``, specifically
    NOT ``CALLER_BEHAVIOR_VIOLATION`` (a stuck mixer is not the caller
    saying something wrong)."""


class PublishSink(Protocol):
    """Abstract audio publish path. Implementations push PCM to the mic.

    MUST drop stale identities (``orchestrator.is_stale``) instead of
    publishing them — the driver checks before calling, the sink re-checks
    at publish time (defense in depth against TOCTOU races).

    Invariant: ``publish()`` only returns once the action's audio has been
    accepted AND fully drained from the mixer — the driver never advances to
    the next action (including ``end``) while the previous action's caller
    audio is still playing. A sink that cannot confirm drain within its own
    bounded timeout MUST raise ``PublishDrainTimeout`` rather than returning
    early or hanging forever.
    """

    async def publish(
        self,
        pcm: bytes,
        identity: GenerationIdentity,
        *,
        label: str,
        gain: float = 1.0,
    ) -> bool:
        """Publish PCM, wait for drain, then return.

        Returns False when dropped (stale) or the mixer rejected the PCM
        (e.g. not ready). Raises PublishDrainTimeout when publish succeeded
        but drain could not be confirmed within the sink's timeout.
        """
        ...


class AgentTurnWait(Protocol):
    """Abstract agent-side observation: wait for the agent turn, then report.

    Returns ``None`` on timeout (agent never replied) — this is DELIBERATELY
    not an exception: an agent that is merely slow is not a caller-behavior
    violation (report: "agent slow != caller violation"). The driver maps a
    ``None`` to ``FailureReason.AGENT_TIMEOUT`` / ``EndedBy.TIMEOUT``, never
    to ``CALLER_BEHAVIOR_VIOLATION`` — that reason is reserved for the
    validator rejecting a candidate, a fully separate failure class.
    """

    async def wait_agent_turn(self, *, timeout_s: float) -> str | None:
        """Block until the agent finishes a turn; None on timeout."""
        ...


@dataclass
class DriverResult:
    ended_by: EndedBy
    failure: RunFailure | None = None
    behaviors_completed: int = 0
    turns_spoken: int = 0


@dataclass
class ContractCallerDriver:
    """Single-path caller loop. No AI/TTS/LiveKit imports — all injected."""

    orchestrator: Orchestrator
    validator: ContractValidator
    adapter: AILanguageAdapter
    planner: CallerInteractionPlanner = field(default_factory=CallerInteractionPlanner)
    synthesize: Any = None  # (text) -> pcm bytes; injected TTS fn
    max_retries: int = 2

    async def run(
        self,
        actions: list[CallerAction],
        sink: PublishSink,
        agent: AgentTurnWait,
        *,
        emit: Any = None,
        recent_turns: list[Turn] | None = None,
        relevant_facts: list[str] | None = None,
    ) -> DriverResult:
        """Drive every action to completion (or STOP on violation/timeout)."""
        log: list[Turn] = list(recent_turns or [])
        facts: list[str] = list(relevant_facts or [])
        completed = 0
        spoken = 0

        def _emit(kind: str, spec: dict[str, Any]) -> None:
            if emit is not None:
                emit(kind, spec=spec)

        for action in actions:
            # Every action — including say — crosses the Orchestrator turn gate.
            self.orchestrator.advance_caller_turn()

            if action.kind == "say":
                self.orchestrator.gate_say(has_crossed_turn_gate=True)
                assert action.say_text is not None
                # Say text is author-fixed (never AI-generated): synthesize the
                # EXACT authored line. Delivery shaping (hesitation/stumble)
                # inserts modelable tokens that a strict downstream
                # transcript-match (e.g. _mostly_script_say on the agent side)
                # could read as drift, so the single path keeps say PCM
                # byte-faithful to the scenario text. InteractionConfig on a
                # say step is reserved for timing (pre_delay/pace) only.
                text = action.say_text
                pre_delay_ms = (action.interaction.pre_delay_ms or 0) if action.interaction else 0
                if pre_delay_ms:
                    await asyncio.sleep(pre_delay_ms / 1000.0)
                pcm = self._speak(text)
                identity = self.orchestrator.current_identity()
                if self.orchestrator.is_stale(identity):
                    return self._fail(
                        FailureReason.TRANSPORT_ERROR,
                        "say identity went stale before publish",
                        EndedBy.ERROR,
                        completed,
                        spoken,
                    )
                try:
                    await sink.publish(pcm, identity, label=f"say:{action.line_no}")
                except PublishDrainTimeout as exc:
                    return self._fail(
                        FailureReason.TRANSPORT_ERROR,
                        f"say publish drain timed out: {exc}",
                        EndedBy.TRANSPORT,
                        completed,
                        spoken,
                    )
                spoken += 1
                log.append(Turn(speaker="caller", text=text))
                _emit("contract.say_published", {"line": action.line_no, "text": text})
                continue

            if action.kind == "do":
                assert action.contract is not None
                contract = action.contract
                self.orchestrator.start_behavior()
                outcome = await self._run_behavior(
                    contract, action, sink, agent, log, facts, _emit
                )
                if isinstance(outcome, DriverResult):
                    outcome.behaviors_completed = completed
                    outcome.turns_spoken = spoken + outcome.turns_spoken
                    return outcome
                turns, agent_text = outcome
                spoken += turns
                completed += 1
                log.append(Turn(speaker="agent", text=agent_text))
                continue

            if action.kind == "wait":
                await asyncio.sleep((action.wait_ms or 0) / 1000.0)
                _emit("contract.wait", {"ms": action.wait_ms or 0})
                continue

            if action.kind in ("end", "hangup"):
                _emit("contract.end", {"kind": action.kind})
                return DriverResult(
                    ended_by=EndedBy.SCENARIO,
                    behaviors_completed=completed,
                    turns_spoken=spoken,
                )

            # dtmf / interrupt / silence: control actions, never AI/TTS.
            _emit("contract.control", {"kind": action.kind, "line": action.line_no})

        return DriverResult(
            ended_by=EndedBy.SCENARIO,
            behaviors_completed=completed,
            turns_spoken=spoken,
        )

    async def _run_behavior(
        self,
        contract: BehaviorContract,
        action: CallerAction,
        sink: PublishSink,
        agent: AgentTurnWait,
        log: list[Turn],
        facts: list[str],
        _emit: Any,
    ) -> tuple[int, str] | DriverResult:
        """Drive one behavior to SATISFIED or to the single canonical
        BEHAVIOR_TIMEOUT. Returns (turns, last agent text) on satisfaction,
        DriverResult on any failure. The ONLY turn-budget owner is
        Orchestrator.check_max_turns() gating the while loop (advance via
        advance_behavior_turn() after each published turn); the loop has no
        independent range bound. Covers the target independently of act:
        a contract with a target is only satisfied by evidence grounded in
        the agent's ACTUAL reply text (evaluate_behavior), never by the
        caller's own claimed target."""
        if not should_invoke_adapter(action.bypasses_ai_and_validator):
            return self._fail(
                FailureReason.VALIDATION_ERROR,
                "do action unexpectedly bypasses the AI adapter",
                EndedBy.ERROR,
                0,
                0,
            )
        turns = 0
        agent_text = ""
        # start_behavior() (called by run() before entering here) already
        # reset the per-behavior turn count to zero; the loop below advances
        # it once per published caller turn via advance_behavior_turn().
        #
        # The turn budget has exactly ONE owner: Orchestrator.check_max_turns()
        # gating this while loop. There is deliberately no independent `for
        # range(max_turns)` bound alongside it -- a Python range plus an
        # Orchestrator counter was two parallel sources of truth for the same
        # budget ("half-owned" progression). The only non-satisfied exit is
        # the gate below, funnelling into the one canonical BEHAVIOR_TIMEOUT
        # failure. stalled_spins is a liveness guard (not a budget): stale
        # identities and refused publishes advance neither the budget nor the
        # turn count, so consecutive non-progress spins are capped to keep a
        # stuck sink from spinning forever; hitting the cap is
        # TRANSPORT_ERROR, never a caller violation.
        stalled_spins = 0
        max_stalled_spins = self.max_retries + 1
        while self.orchestrator.check_max_turns(contract) == BehaviorOutcome.CONTINUE:
            context = build_context(
                contract=contract,
                turn=turns,
                agent_latest=agent_text or None,
                relevant_facts=facts,
                recent_turns=log,
            )

            def _generate() -> CandidateUtterance:
                identity = self.orchestrator.new_generation()
                return self.adapter.generate_candidate(contract, context, identity)

            # validate_with_retry is SYNCHRONOUS over a sync generate() fn:
            # per-attempt backend/network failures surface as
            # LanguageGenerationError (never VALID, never a candidate), so
            # they must NOT consume the validator's retry budget or be
            # conflated with a semantic INVALID verdict. Only validator
            # verdicts count toward max_retries; transport failures fail
            # the behavior immediately as LANGUAGE_GENERATION_ERROR.
            candidates: list[CandidateUtterance] = []
            last_verdict: ValidationResult | None = None
            attempts = 0
            transport_error: LanguageGenerationError | None = None
            # Diagnostic verdict trail (emitted below): one entry per
            # validator attempt, in order. _attempt_verdicts is ALWAYS
            # defined here (before the loop) so the emit below can never
            # hit a NameError regardless of which exit path runs.
            _attempt_verdicts: list[tuple[str, str | None]] = []
            for _ in range(self.max_retries + 1):
                attempts += 1
                try:
                    candidate_attempt = _generate()
                except LanguageGenerationError as exc:
                    transport_error = exc
                    break
                result_attempt = self.validator.validate(candidate_attempt, contract)
                last_verdict = result_attempt
                _attempt_verdicts.append(
                    (result_attempt.verdict.value, result_attempt.reason)
                )
                if result_attempt.is_valid():
                    candidates.append(candidate_attempt)
                    break
            # Diagnostic: record every attempt's verdict so a live
            # behavior_violation names the ACTUAL failing verdicts instead of
            # only the last one (run 013-015 class: which attempt failed how).
            for _v in _attempt_verdicts:
                _emit(
                    "contract.attempt_verdict",
                    {
                        "behavior": contract.behavior,
                        "verdict": _v[0],
                        "reason": _v[1],
                    },
                )
            if transport_error is not None and not candidates:
                return self._fail(
                    FailureReason.LANGUAGE_GENERATION_ERROR,
                    str(transport_error),
                    EndedBy.ERROR,
                    0,
                    turns,
                )
            from .validator import RetryOutcome

            if not candidates:
                # Every attempt produced an INVALID/UNKNOWN/ERROR verdict:
                # surface the LAST verdict (bounded retry exhausted) exactly
                # like validate_with_retry did -- no candidate to publish.
                assert last_verdict is not None, "loop ran zero attempts"
                retry = RetryOutcome(
                    result=last_verdict, candidate=None, attempts=attempts
                )
            else:
                retry = RetryOutcome(
                    result=last_verdict, candidate=candidates[0], attempts=attempts
                )
            if not retry.result.is_valid():
                _emit(
                    "contract.behavior_violation",
                    {
                        "behavior": contract.behavior,
                        "reason": retry.result.reason,
                        "attempts": retry.attempts,
                    },
                )
                return self._fail(
                    FailureReason.CALLER_BEHAVIOR_VIOLATION,
                    retry.result.reason or "no VALID candidate after retries",
                    EndedBy.ERROR,
                    0,
                    turns,
                )
            candidate = retry.candidate
            assert candidate is not None
            if self.orchestrator.is_stale(candidate.identity):
                stalled_spins += 1  # superseded mid-generation; next turn re-generates
                if stalled_spins > max_stalled_spins:
                    return self._fail(
                        FailureReason.TRANSPORT_ERROR,
                        "behavior stalled: repeated stale identities, no forward progress",
                        EndedBy.TRANSPORT,
                        0,
                        turns,
                    )
                continue  # superseded mid-generation; next turn re-generates
            # do: the candidate ALREADY passed the validator on its exact
            # utterance string. Synthesize that exact string (never a
            # planner-reshaped variant) so the TTS input is byte-identical to
            # what was validated -- the security boundary and the audio stay
            # on the same text. Delivery timing (pre_delay) still applies via
            # the planner's outcome metadata without touching the words.
            plan = self.planner.plan_speak(candidate.utterance, _interaction(action))
            if plan.pre_delay_ms:
                await asyncio.sleep(plan.pre_delay_ms / 1000.0)
            text = candidate.utterance
            pcm = self._speak(text)
            if self.orchestrator.is_stale(candidate.identity):
                stalled_spins += 1  # superseded between validate and publish
                if stalled_spins > max_stalled_spins:
                    return self._fail(
                        FailureReason.TRANSPORT_ERROR,
                        "behavior stalled: repeated stale identities, no forward progress",
                        EndedBy.TRANSPORT,
                        0,
                        turns,
                    )
                continue
            try:
                published = await sink.publish(
                    pcm, candidate.identity, label=f"do:{contract.behavior}"
                )
            except PublishDrainTimeout as exc:
                return self._fail(
                    FailureReason.TRANSPORT_ERROR,
                    f"do publish drain timed out: {exc}",
                    EndedBy.TRANSPORT,
                    0,
                    turns,
                )
            if not published:
                # Sink refused (stale re-check or mixer not ready) without
                # raising: no forward progress was made, count the spin.
                stalled_spins += 1
                if stalled_spins > max_stalled_spins:
                    return self._fail(
                        FailureReason.TRANSPORT_ERROR,
                        "behavior stalled: sink repeatedly refused publish",
                        EndedBy.TRANSPORT,
                        0,
                        turns,
                    )
                continue
            stalled_spins = 0  # forward progress resets the liveness guard
            turns += 1
            self.orchestrator.advance_behavior_turn()
            log.append(Turn(speaker="caller", text=text))
            _emit(
                "contract.turn_published",
                {"behavior": contract.behavior, "turn": turns, "text": text},
            )
            # Settle the caller-audio onset latch BEFORE waiting for the
            # agent: the agent's audible answer must be attributable to THIS
            # caller turn. PublishSink.publish already drained the mixer
            # queue; this extra settle covers the LiveKit track-propagation
            # tail (remote subscribes + first audio frame) so
            # wait_agent_turn's "seen_at_start" baseline is taken after our
            # speech is fully on the wire. Bounded (1s) and OUTSIDE the agent
            # timeout budget -- a slow network here must not be misread as a
            # slow agent.
            await asyncio.sleep(1.0)
            agent_text = await agent.wait_agent_turn(timeout_s=30.0)
            if agent_text is None:
                # Agent silence, NOT a caller violation — a separate failure
                # class (see AgentTurnWait docstring). The caller's turn was
                # already validly spoken and published; only the agent side
                # timed out.
                _emit(
                    "contract.agent_timeout",
                    {"behavior": contract.behavior, "turn": turns},
                )
                return self._fail(
                    FailureReason.AGENT_TIMEOUT,
                    f"agent did not reply to behavior {contract.behavior!r} within timeout",
                    EndedBy.TIMEOUT,
                    0,
                    turns,
                )
            log.append(Turn(speaker="agent", text=agent_text))
            verdict = self.orchestrator.evaluate_behavior(contract, agent_text)
            if verdict == EvaluatorVerdict.SATISFIED:
                return turns, agent_text
            # Not satisfied: loop back to the single owner gate at the top
            # (check_max_turns -> CONTINUE means "another caller turn",
            # FAILED means fall out of the loop into the one canonical
            # BEHAVIOR_TIMEOUT below). No inline failure construction here --
            # that keeps exactly ONE exit shape for budget exhaustion.
        # The gate above is the single owner: reaching here means
        # check_max_turns() returned FAILED_MAX_TURNS -- the budget ran out
        # without the agent satisfying the behavior.
        _emit(
            "contract.behavior_violation",
            {"behavior": contract.behavior, "reason": "FAILED_MAX_TURNS"},
        )
        return self._fail(
            FailureReason.BEHAVIOR_TIMEOUT,
            f"behavior {contract.behavior!r} unsatisfied after max_turns",
            EndedBy.TIMEOUT,
            0,
            turns,
        )

    def _speak(self, text: str) -> bytes:
        if self.synthesize is None:
            raise RuntimeError("ContractCallerDriver.synthesize TTS fn is not set")
        return bytes(self.synthesize(text))

    @staticmethod
    def _fail(
        reason: FailureReason,
        detail: str,
        ended_by: EndedBy,
        completed: int,
        spoken: int,
    ) -> DriverResult:
        return DriverResult(
            ended_by=ended_by,
            failure=RunFailure(reason=reason, detail=detail),
            behaviors_completed=completed,
            turns_spoken=spoken,
        )


def _interaction(action: CallerAction) -> InteractionConfig | None:
    return action.interaction


__all__ = [
    "AgentTurnWait",
    "ContractCallerDriver",
    "DriverResult",
    "PublishDrainTimeout",
    "PublishSink",
]
