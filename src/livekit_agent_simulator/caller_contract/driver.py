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
from .validator import ContractValidator, validate_with_retry


class PublishSink(Protocol):
    """Abstract audio publish path. Implementations push PCM to the mic.

    MUST drop stale identities (``orchestrator.is_stale``) instead of
    publishing them — the driver checks before calling, the sink re-checks
    at publish time (defense in depth against TOCTOU races).
    """

    def publish(
        self,
        pcm: bytes,
        identity: GenerationIdentity,
        *,
        label: str,
        gain: float = 1.0,
    ) -> bool:
        """Publish PCM; returns False when dropped (stale) or failed."""
        ...


class AgentTurnWait(Protocol):
    """Abstract agent-side observation: wait for the agent turn, then report."""

    async def wait_agent_turn(self, *, timeout_s: float) -> str:
        """Block until the agent finishes a turn; returns the agent text."""
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
                plan = self.planner.plan_speak(action.say_text, _interaction(action))
                text = " ".join(plan.tokens)
                if plan.pre_delay_ms:
                    import asyncio as _asyncio

                    await _asyncio.sleep(plan.pre_delay_ms / 1000.0)
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
                sink.publish(pcm, identity, label=f"say:{action.line_no}")
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
                import asyncio as _asyncio

                await _asyncio.sleep((action.wait_ms or 0) / 1000.0)
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
        """Loop one behavior up to max_turns; returns (turns, last agent text)."""
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
        for _ in range(contract.constraints.max_turns):
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

            try:
                retry = validate_with_retry(
                    self.validator, contract, _generate, max_retries=self.max_retries
                )
            except LanguageGenerationError as exc:
                return self._fail(
                    FailureReason.LANGUAGE_GENERATION_ERROR,
                    str(exc),
                    EndedBy.ERROR,
                    0,
                    turns,
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
                continue  # superseded mid-generation; next turn re-generates
            plan = self.planner.plan_speak(candidate.utterance, _interaction(action))
            text = " ".join(plan.tokens)
            pcm = self._speak(text)
            if self.orchestrator.is_stale(candidate.identity):
                continue
            published = sink.publish(
                pcm, candidate.identity, label=f"do:{contract.behavior}"
            )
            if not published:
                continue
            turns += 1
            log.append(Turn(speaker="caller", text=text))
            _emit(
                "contract.turn_published",
                {"behavior": contract.behavior, "turn": turns, "text": text},
            )
            agent_text = await agent.wait_agent_turn(timeout_s=30.0)
            log.append(Turn(speaker="agent", text=agent_text))
            verdict = self.orchestrator.evaluate_behavior(contract, agent_text)
            if verdict == EvaluatorVerdict.SATISFIED:
                return turns, agent_text
            if self.orchestrator.check_max_turns(contract) == BehaviorOutcome.FAILED_MAX_TURNS:
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
        # Loop above always returns via SATISFIED / FAILED_MAX_TURNS / violation.
        return self._fail(
            FailureReason.BEHAVIOR_TIMEOUT,
            f"behavior {contract.behavior!r} exhausted without verdict",
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
    "PublishSink",
]
