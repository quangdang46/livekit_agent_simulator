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
from .dsl import CallerAction, TriggerConfig
from .failures import RunFailure, TTSSynthesisError

# Poll cadence for trigger condition waits (matches legacy ScriptRunner 50ms).
_TRIGGER_POLL_S = 0.05

# Budget for a condition trigger (agent_speaking/silence) that never fires.
# Expiry is BEHAVIOR_TIMEOUT (caller choreography could not proceed) — never
# AGENT_TIMEOUT (reserved for the greeting wait) nor TRANSPORT. ``time``
# triggers never hit this budget (their deadline always arrives).
# Not exposed in the DSL: one less tuning knob until Slice 4b proves it needed.
TRIGGER_WAIT_BUDGET_S = 30.0

# How long a non-barge ``say`` waits for the agent to go silent before
# publishing (contract equivalent of the legacy non-barge turn-taking gate;
# the mixer-drain half already lives in the sink). Expiry falls through and
# publishes anyway — a talkative agent must not wedge the caller forever.
AGENT_SILENCE_WAIT_S = 6.0

# Gap tolerance for speech/silence continuity tracking (mirrors the legacy
# ScriptRunner 1200ms active-speaker gap tolerance): a signal dropout shorter
# than this does not reset the continuity clock. Deliberately generous
# relative to the 50ms poll cadence, deliberately smaller than any real
# turn boundary. Deferred from Slice 4 (naive reset) — see _wait_trigger.
TRIGGER_GAP_TOLERANCE_S = 1.2

# Wall-clock floor per interruption rate when the DSL omits
# interruption_interval_ms (legacy interval spirit: low 90s / medium 45s /
# high 30s). Separate from the seeded per-turn coin flip (see
# CallerInteractionPlanner.should_interrupt): the flip decides WHETHER this
# turn interrupts, the floor decides whether enough time passed since the
# last cut-in.
_INTERVAL_MS_BY_RATE = {"low": 90_000, "medium": 45_000, "high": 30_000}
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

    def is_agent_speaking_now(self) -> bool:
        """Realtime agent-speech signal for trigger gating (Slice 4).

        Polled (never blocking): True while the agent is actively speaking.
        The driver reads it via ``getattr(agent, "is_agent_speaking_now",
        lambda: False)`` so older fakes without this method behave as
        "agent silent" — preserving pre-trigger behavior.
        """
        ...


class AudioAssetPlayer(Protocol):
    """Abstract background-bed playback (ambient/office noise).

    Kept separate from ``PublishSink`` on purpose: speech PCM goes through
    the staleness-checked, drain-awaited sink; noise beds ride the mixer's
    parallel noise layer (never drain-awaited, never an utterance). The
    driver reads it via ``getattr(player, "play_asset", None)`` — a run
    without asset support fails the ``play_audio`` action with
    TRANSPORT_ERROR rather than silently dropping the bed.
    """

    async def play_asset(
        self, asset: str, *, gain: float, loop: bool, label: str
    ) -> bool:
        """Resolve + push an asset bed; True when accepted."""
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
    # Scenario identity for the seeded interruption policy (set by live_wiring;
    # empty in unit tests unless assigned). Part of the determinism key.
    _scenario_id: str = ""
    _last_interrupt_ms: float | None = None
    # Optional record hook: called after EVERY validate() (pass or fail) so
    # record-then-replay reproduces the identical verdict trail without AI.
    # Set by live_wiring from --record; None in unit tests unless assigned.
    recorder: Any = None

    async def run(
        self,
        actions: list[CallerAction],
        sink: PublishSink,
        agent: AgentTurnWait,
        *,
        emit: Any = None,
        recent_turns: list[Turn] | None = None,
        relevant_facts: list[str] | None = None,
        first_speaker: str = "user",
        silent_mode: bool = False,
        greeting_timeout_s: float = 30.0,
        assets: AudioAssetPlayer | None = None,
        hold_timeout_s: float | None = None,
        on_hold_timeout: Any = None,
    ) -> DriverResult:
        """Drive every action to completion (or STOP on violation/timeout).

        ``first_speaker="agent"`` waits for the agent greeting BEFORE the
        first caller action (the contract equivalent of the legacy
        nudge_caller_after_agent_greeting: no caller audio is generated
        until the agent has demonstrably spoken). Agent silence here is
        AGENT_TIMEOUT, never a caller violation. ``"user"`` (default)
        starts immediately, preserving the pre-first-speaker behavior.
        Any other value is a hard ValueError — never silently treated
        as "user".

        ``greeting_timeout_s`` bounds the greeting wait; live_wiring passes
        the scenario's own timeout so no second timeout semantics exists.

        ``silent_mode=True`` drops ``say``/``do`` actions (no AI, no TTS,
        no publish — the caller stays mute) while control actions
        (wait/end/hangup/…) still run. Mirrors the legacy silent-mode
        compile (explicit speak steps dropped, wait/hang_up kept).
        """
        if first_speaker not in ("user", "agent"):
            raise ValueError(
                f"first_speaker must be 'user'|'agent', got {first_speaker!r}"
            )
        log: list[Turn] = list(recent_turns or [])
        facts: list[str] = list(relevant_facts or [])
        completed = 0
        spoken = 0

        def _emit(kind: str, spec: dict[str, Any]) -> None:
            if emit is not None:
                emit(kind, spec=spec)

        if first_speaker == "agent":
            _emit(
                "contract.first_speaker_wait",
                {"expected_speaker": "agent", "timeout_s": greeting_timeout_s},
            )
            greeting = await agent.wait_agent_turn(timeout_s=greeting_timeout_s)
            if greeting is None:
                _emit("contract.agent_timeout", {"phase": "first_speaker_greeting"})
                return self._fail(
                    FailureReason.AGENT_TIMEOUT,
                    "agent did not speak first within timeout",
                    EndedBy.TIMEOUT,
                    0,
                    0,
                )
            log.append(Turn(speaker="agent", text=greeting))
            _emit("contract.first_speaker_greeting", {"text": greeting})

        hold_task: asyncio.Task | None = None
        if hold_timeout_s is not None:
            hold_task = asyncio.create_task(
                self._hold_watchdog(
                    agent, hold_timeout_s, _emit, on_hold_timeout=on_hold_timeout
                )
            )
        try:
            return await self._run_actions(
                actions,
                sink,
                agent,
                log=log,
                facts=facts,
                completed=0,
                spoken=0,
                silent_mode=silent_mode,
                assets=assets,
                _emit=_emit,
            )
        finally:
            if hold_task is not None:
                hold_task.cancel()
                await asyncio.gather(hold_task, return_exceptions=True)

    async def _hold_watchdog(
        self,
        agent: AgentTurnWait,
        hold_timeout_s: float,
        _emit: Any,
        *,
        on_hold_timeout: Any = None,
    ) -> None:
        """Agent dead-air watchdog (contract equivalent of the legacy hold
        timeout): once the agent has spoken at least once, give up after
        ``hold_timeout_s`` of agent silence. Fires ``on_hold_timeout`` (the
        live wiring hangs up the bridge) and emits ``sim.hold_timeout`` with
        the same event shape the legacy loop used.
        """
        import time as _time

        while True:
            await asyncio.sleep(0.25)
            last_ms = None
            probe = getattr(agent, "last_speech_at_ms", None)
            if callable(probe):
                try:
                    last_ms = probe()
                except Exception:  # noqa: BLE001 — best-effort probe
                    last_ms = None
            if last_ms is None:
                continue  # not armed: agent never demonstrably spoke
            idle_s = _time.monotonic() - last_ms / 1000.0
            if idle_s >= hold_timeout_s:
                _emit(
                    "sim.hold_timeout",
                    {
                        "timeout_s": hold_timeout_s,
                        "agent_idle_ms": int(idle_s * 1000),
                        "note": "Caller gave up waiting on agent dead air (hold_music_timeout_s)",
                    },
                )
                if on_hold_timeout is not None:
                    result = on_hold_timeout()
                    if asyncio.iscoroutine(result):
                        await result
                return

    async def _run_actions(
        self,
        actions: list[CallerAction],
        sink: PublishSink,
        agent: AgentTurnWait,
        *,
        log: list[Turn],
        facts: list[str],
        completed: int,
        spoken: int,
        silent_mode: bool,
        assets: AudioAssetPlayer | None,
        _emit: Any,
    ) -> DriverResult:
        for action in actions:
            if silent_mode and action.kind in ("say", "do"):
                # Silent caller stays mute: skip AI/TTS/publish entirely,
                # but keep the action visible in the event trail.
                _emit("contract.silent_skip", {"kind": action.kind, "line": action.line_no})
                continue
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
                # Trigger gate (Slice 4): wait for the WHEN condition before
                # the non-barge silence gate. Expiry is BEHAVIOR_TIMEOUT —
                # the choreography could not proceed (never AGENT_TIMEOUT,
                # which is reserved for the greeting wait, nor TRANSPORT).
                if action.trigger is not None:
                    _emit(
                        "contract.trigger_armed",
                        {"kind": action.trigger.kind, "line": action.line_no},
                    )
                    fired = await _wait_trigger(action.trigger, agent, emit=_emit)
                    if not fired:
                        _emit(
                            "contract.behavior_violation",
                            {"reason": "TRIGGER_TIMEOUT", "line": action.line_no},
                        )
                        return self._fail(
                            FailureReason.BEHAVIOR_TIMEOUT,
                            f"trigger {action.trigger.kind!r} never fired",
                            EndedBy.TIMEOUT,
                            completed,
                            spoken,
                        )
                if not action.barge_in:
                    # Non-barge: never talk over the agent (legacy
                    # wait_agent_idle equivalent; mixer-drain half lives in
                    # the sink). Bounded: a talkative agent must not wedge
                    # the caller — expiry falls through and publishes anyway.
                    # Barge skips this gate entirely by design.
                    await _wait_agent_silence(agent)
                else:
                    # `class` is load-bearing, not decoration: recovery
                    # asserts / barge_recovery_rate count only correction and
                    # escalate cut-ins, and read the class off this event
                    # (same vocabulary the legacy sim.script.cue used).
                    _emit(
                        "contract.barge",
                        {"line": action.line_no, "class": _interrupt_class(action)},
                    )
                try:
                    pcm = self._speak(text)
                except TTSSynthesisError as exc:
                    _emit("contract.tts_error", {"line": action.line_no, "error": str(exc)[:200]})
                    return self._fail(
                        FailureReason.TTS_ERROR,
                        str(exc)[:300],
                        EndedBy.ERROR,
                        completed,
                        spoken,
                    )
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

            if action.kind == "play_audio":
                # Background bed (ambient/office noise): NOT an utterance.
                # No TTS, no validator, no turn-log entry — it rides the
                # mixer's parallel noise layer via AudioAssetPlayer. Optional
                # trigger gates WHEN the bed starts (e.g. time 1500ms).
                assert action.audio_asset is not None
                if action.trigger is not None:
                    _emit(
                        "contract.trigger_armed",
                        {
                            "kind": action.trigger.kind,
                            "line": action.line_no,
                            "action": "play_audio",
                        },
                    )
                    fired = await _wait_trigger(action.trigger, agent, emit=_emit)
                    if not fired:
                        _emit(
                            "contract.behavior_violation",
                            {"reason": "TRIGGER_TIMEOUT", "line": action.line_no},
                        )
                        return self._fail(
                            FailureReason.BEHAVIOR_TIMEOUT,
                            f"trigger {action.trigger.kind!r} never fired",
                            EndedBy.TIMEOUT,
                            completed,
                            spoken,
                        )
                player = assets
                if player is None or getattr(player, "play_asset", None) is None:
                    return self._fail(
                        FailureReason.TRANSPORT_ERROR,
                        "play_audio requires an AudioAssetPlayer (no silent drop)",
                        EndedBy.TRANSPORT,
                        completed,
                        spoken,
                    )
                try:
                    accepted = await player.play_asset(
                        action.audio_asset,
                        gain=action.audio_gain,
                        loop=action.audio_loop,
                        label=f"play_audio:{action.line_no}",
                    )
                except Exception as exc:  # noqa: BLE001 — asset failure is transport, never a caller violation
                    return self._fail(
                        FailureReason.TRANSPORT_ERROR,
                        f"play_audio failed for {action.audio_asset!r}: {exc}",
                        EndedBy.TRANSPORT,
                        completed,
                        spoken,
                    )
                if not accepted:
                    return self._fail(
                        FailureReason.TRANSPORT_ERROR,
                        f"play_audio refused for {action.audio_asset!r}",
                        EndedBy.TRANSPORT,
                        completed,
                        spoken,
                    )
                _emit(
                    "contract.audio_playing",
                    {
                        "asset": action.audio_asset,
                        "gain": action.audio_gain,
                        "loop": action.audio_loop,
                        "line": action.line_no,
                    },
                )
                continue

            if action.kind in ("end", "hangup"):
                _emit("contract.end", {"kind": action.kind})
                return DriverResult(
                    ended_by=EndedBy.SCENARIO,
                    behaviors_completed=completed,
                    turns_spoken=spoken,
                )

            if action.kind == "interrupt":
                # Explicit caller interaction action (NOT a semantic behavior,
                # NOT validator-gated): a short fixed cut-in line. Delivery
                # policy identical to barge_in=True say: publish immediately
                # without waiting for agent silence.
                _emit(
                    "contract.interrupt",
                    {"line": action.line_no, "class": _interrupt_class(action)},
                )
                interrupt_text = _interrupt_text(action)
                try:
                    interrupt_pcm = self._speak(interrupt_text)
                except TTSSynthesisError as exc:
                    _emit("contract.tts_error", {"line": action.line_no, "error": str(exc)[:200]})
                    return self._fail(
                        FailureReason.TTS_ERROR,
                        str(exc)[:300],
                        EndedBy.ERROR,
                        completed,
                        spoken,
                    )
                identity = self.orchestrator.current_identity()
                if self.orchestrator.is_stale(identity):
                    return self._fail(
                        FailureReason.TRANSPORT_ERROR,
                        "interrupt identity went stale before publish",
                        EndedBy.ERROR,
                        completed,
                        spoken,
                    )
                try:
                    await sink.publish(
                        interrupt_pcm, identity, label=f"interrupt:{action.line_no}"
                    )
                except PublishDrainTimeout as exc:
                    return self._fail(
                        FailureReason.TRANSPORT_ERROR,
                        f"interrupt publish drain timed out: {exc}",
                        EndedBy.TRANSPORT,
                        completed,
                        spoken,
                    )
                spoken += 1
                log.append(Turn(speaker="caller", text=interrupt_text))
                continue

            # dtmf / silence: control actions, never AI/TTS.
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
                observed_trail: list[dict[str, Any]] = []
                result_attempt = self.validator.validate(
                    candidate_attempt, contract, record_observed=observed_trail
                )
                last_verdict = result_attempt
                _attempt_verdicts.append(
                    (result_attempt.verdict.value, result_attempt.reason)
                )
                if self.recorder is not None:
                    self.recorder.record_attempt(
                        candidate_attempt,
                        result_attempt,
                        retry_index=attempts - 1,
                        observed=observed_trail[-1] if observed_trail else None,
                    )
                # Replay mode: the backend served a recorded candidate, so the
                # verdict MUST match the recording — divergence fails loudly
                # (never silently produces a different run).
                assert_verdict = getattr(
                    self.adapter.backend, "assert_verdict", None
                )
                if assert_verdict is not None:
                    assert_verdict(result_attempt.verdict)
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
            try:
                pcm = self._speak(text)
            except TTSSynthesisError as exc:
                # Validated text, broken audio: TTS_ERROR (never a caller
                # violation, never a regenerate-the-candidate loop). The
                # validator already passed this exact string; retrying TTS
                # happened inside _speak — exhaustion ends the behavior here.
                _emit("contract.tts_error", {"behavior": contract.behavior, "error": str(exc)[:200]})
                return self._fail(
                    FailureReason.TTS_ERROR,
                    str(exc)[:300],
                    EndedBy.ERROR,
                    0,
                    turns,
                )
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
            # Seeded interruption policy (P1 fix): decided WHILE the agent is
            # speaking, not before it starts. wait_agent_turn is wrapped so
            # the policy can cut in mid-answer: the wrapper polls the
            # speaking flag during the wait and emits one fixed backchannel
            # ("Mhm.", never AI, never validator) the moment the agent is
            # audibly responding and the seeded flip + interval allow it.
            agent_text = await self._wait_agent_turn_with_policy(
                action, sink, agent, log, _emit, timeout_s=30.0
            )
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

    async def _wait_agent_turn_with_policy(
        self,
        action: CallerAction,
        sink: PublishSink,
        agent: AgentTurnWait,
        log: list[Turn],
        _emit: Any,
        *,
        timeout_s: float,
    ) -> str | None:
        """Wait for the agent turn, cutting in mid-answer when the seeded
        interruption policy allows it (P1 fix: the decision happens WHILE
        the agent speaks, not before it starts — the old pre-wait check saw
        a silent agent and could never fire in real conversations).

        Polls the speaking flag during the wait: the first poll that sees
        active speech evaluates the seeded flip + interval; on yes, one
        fixed backchannel ("Mhm.", never AI, never validator) publishes
        immediately and the wait continues for the actual turn text.
        Returns the agent text, or None on timeout (same contract as
        AgentTurnWait.wait_agent_turn).
        """
        interaction = action.interaction
        if interaction is None or not interaction.interruption_rate:
            return await agent.wait_agent_turn(timeout_s=timeout_s)
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout_s
        interrupted = False
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            # Short slices so the cut-in lands mid-answer, not after it.
            try:
                text = await asyncio.wait_for(
                    agent.wait_agent_turn(timeout_s=min(remaining, 0.25)),
                    timeout=min(remaining, 0.25) + 5.0,
                )
            except asyncio.TimeoutError:
                text = None
            if text is not None:
                return text
            if interrupted:
                continue
            if self._policy_cut_in(action, sink, agent, log, _emit):
                interrupted = True
                # Publish synchronously below (needs sink + TTS); the wait
                # loop continues afterwards for the real turn text.
                outcome = await self._publish_policy_cut_in(
                    sink, log, _emit
                )
                if isinstance(outcome, DriverResult):
                    # Publish failure inside a turn wait: surface as the
                    # turn's agent text being unavailable is wrong — stash
                    # the failure by returning None is wrong too. The
                    # cut-in is best-effort decoration: log and continue
                    # waiting for the real answer.
                    _emit(
                        "contract.policy_interrupt_dropped",
                        {"reason": outcome.failure.reason.value if outcome.failure else "unknown"},
                    )

    def _policy_cut_in(
        self,
        action: CallerAction,
        sink: PublishSink,
        agent: AgentTurnWait,
        log: list[Turn],
        _emit: Any,
    ) -> bool:
        """Pure decision: should the policy cut in RIGHT NOW?

        Gates (all must hold): interaction enables interruption_rate; the
        agent is speaking NOW (never cut into silence); the seeded flip for
        this agent-turn index says yes; the wall-clock interval elapsed.
        No I/O, no publish — the caller publishes on True.
        """
        interaction = action.interaction
        if interaction is None or not interaction.interruption_rate:
            return False
        speaking = getattr(agent, "is_agent_speaking_now", lambda: False)
        if not speaking():
            return False
        scenario_id = getattr(self, "_scenario_id", "")
        turn_index = len([t for t in log if t.speaker == "agent"])
        if not self.planner.should_interrupt(
            interaction, scenario_id=scenario_id, agent_turn_index=turn_index
        ):
            return False
        interval_ms = interaction.interruption_interval_ms or _INTERVAL_MS_BY_RATE.get(
            interaction.interruption_rate, 45_000
        )
        now_ms = asyncio.get_event_loop().time() * 1000.0
        last_ms = getattr(self, "_last_interrupt_ms", None)
        if last_ms is not None and now_ms - last_ms < interval_ms:
            return False
        return True

    async def _publish_policy_cut_in(
        self,
        sink: PublishSink,
        log: list[Turn],
        _emit: Any,
    ) -> int | DriverResult:
        """Publish one fixed backchannel cut-in ("Mhm."). Returns 1, or a
        DriverResult on publish failure (caller treats it as decoration:
        logged, never fatal to the behavior)."""
        self._last_interrupt_ms = asyncio.get_event_loop().time() * 1000.0
        outcome = self.planner.plan_backchannel()
        text = " ".join(outcome.tokens) if outcome.tokens else "Mhm."
        turn_index = len([t for t in log if t.speaker == "agent"])
        _emit("contract.policy_interrupt", {"text": text, "turn": turn_index})
        try:
            interrupt_pcm = self._speak(text)
        except TTSSynthesisError as exc:
            _emit("contract.tts_error", {"error": str(exc)[:200]})
            return self._fail(
                FailureReason.TTS_ERROR,
                str(exc)[:300],
                EndedBy.ERROR,
                0,
                0,
            )
        identity = self.orchestrator.current_identity()
        if self.orchestrator.is_stale(identity):
            return self._fail(
                FailureReason.TRANSPORT_ERROR,
                "policy interrupt identity went stale before publish",
                EndedBy.ERROR,
                0,
                0,
            )
        try:
            published = await sink.publish(
                interrupt_pcm, identity, label="policy_interrupt"
            )
        except PublishDrainTimeout as exc:
            return self._fail(
                FailureReason.TRANSPORT_ERROR,
                f"policy interrupt publish drain timed out: {exc}",
                EndedBy.TRANSPORT,
                0,
                0,
            )
        if not published:
            return 0  # sink refused without raising: no progress, no failure
        log.append(Turn(speaker="caller", text=text))
        return 1

    def _speak(self, text: str) -> bytes:
        """Synthesize already-validated text to PCM, with TTS-only retry.

        Uses TTSPublishState (failures.py): a synthesis failure retries the
        SAME text (never re-invokes the AI adapter), bounded by
        ``max_retries``; exhaustion is TTS_ERROR, never a caller violation
        and never TRANSPORT (the wire was never touched).
        """
        if self.synthesize is None:
            raise RuntimeError("ContractCallerDriver.synthesize TTS fn is not set")
        attempts = self.max_retries + 1
        last_error: Exception | None = None
        for _ in range(attempts):
            try:
                return bytes(self.synthesize(text))
            except Exception as exc:  # noqa: BLE001 — TTS failure retries TTS only, then TTS_ERROR
                last_error = exc
        raise TTSSynthesisError(
            f"TTS synthesis failed after {attempts} attempt(s): {last_error}"
        )

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


# Fixed cut-in lines per interrupt class. Author-fixed text (like ``say``):
# never AI-generated, never validator-gated — but unlike ``say`` it always
# publishes immediately (barge delivery), because an interrupt that waits
# for silence is a contradiction. Classes mirror the legacy interrupt_class
# vocabulary (correction|backchannel); unknown classes fall back to the
# neutral correction line rather than failing the run.
_INTERRUPT_LINES = {
    "correction": "Wait — one second.",
    "backchannel": "Mhm.",
}


def _interrupt_class(action: CallerAction) -> str:
    raw = action.interaction
    cls = getattr(raw, "interrupt_class", None) if raw is not None else None
    if isinstance(cls, str) and cls.strip():
        return cls.strip().lower()
    return "correction"


def _interrupt_text(action: CallerAction) -> str:
    return _INTERRUPT_LINES.get(_interrupt_class(action), _INTERRUPT_LINES["correction"])


async def _sleep_ms(ms: int) -> None:
    if ms > 0:
        await asyncio.sleep(ms / 1000.0)


async def _wait_trigger(
    trigger: TriggerConfig,
    agent: AgentTurnWait,
    *,
    emit: Any,
) -> bool:
    """Wait until a trigger condition fires. Returns True on fire, False on
    budget expiry (caller: fail BEHAVIOR_TIMEOUT).

    Polls ``is_agent_speaking_now`` (via getattr: fakes without the method
    read as "agent silent") every ``_TRIGGER_POLL_S``. Continuity tracking
    tolerates signal dropouts shorter than ``TRIGGER_GAP_TOLERANCE_S``
    (legacy 1200ms parity) — only a longer opposite run resets the clock.

    Cancellation/staleness: every sleep is a bare ``asyncio.sleep`` with no
    shielding, so task cancellation propagates immediately; the orchestrator
    staleness check before publish (call-site) drops anything armed by a
    superseded run.
    """
    speaking = getattr(agent, "is_agent_speaking_now", lambda: False)
    if trigger.kind == "time":
        await _sleep_ms(trigger.delay_ms)
        return True

    loop = asyncio.get_event_loop()
    budget_deadline = loop.time() + TRIGGER_WAIT_BUDGET_S

    def _continuous_ms(since: float | None, now: float) -> float:
        return (now - since) * 1000.0 if since is not None else 0.0

    if trigger.kind == "agent_speaking":
        continuous_since: float | None = None
        gap_since: float | None = None
        while loop.time() < budget_deadline:
            now = loop.time()
            if speaking():
                if continuous_since is None:
                    continuous_since = now
                gap_since = None
                if _continuous_ms(continuous_since, now) >= trigger.min_agent_active_ms:
                    emit("contract.trigger_fired", {"kind": "agent_speaking"})
                    await _sleep_ms(trigger.delay_ms)
                    return True
            else:
                if continuous_since is not None:
                    if gap_since is None:
                        gap_since = now
                    elif now - gap_since >= TRIGGER_GAP_TOLERANCE_S:
                        continuous_since = None
                        gap_since = None
            await asyncio.sleep(_TRIGGER_POLL_S)
        return False

    # kind == "silence": delay_ms IS the required continuous silence duration.
    silence_since: float | None = None
    gap_since: float | None = None
    while loop.time() < budget_deadline:
        now = loop.time()
        if not speaking():
            if silence_since is None:
                silence_since = now
            gap_since = None
            if _continuous_ms(silence_since, now) >= trigger.delay_ms:
                emit("contract.trigger_fired", {"kind": "silence"})
                return True
        else:
            if silence_since is not None:
                if gap_since is None:
                    gap_since = now
                elif now - gap_since >= TRIGGER_GAP_TOLERANCE_S:
                    silence_since = None
                    gap_since = None
        await asyncio.sleep(_TRIGGER_POLL_S)
    return False


async def _wait_agent_silence(agent: AgentTurnWait) -> None:
    """Non-barge ``say`` gate: wait until the agent stops speaking (or the
    bounded wait expires, in which case fall through and publish anyway).

    Uses the same getattr fallback as _wait_trigger so pre-Slice-4 fakes
    (no is_agent_speaking_now) read as "already silent" and fire immediately.
    """
    speaking = getattr(agent, "is_agent_speaking_now", lambda: False)
    deadline = asyncio.get_event_loop().time() + AGENT_SILENCE_WAIT_S
    while speaking():
        if asyncio.get_event_loop().time() >= deadline:
            return
        await asyncio.sleep(_TRIGGER_POLL_S)


__all__ = [
    "AgentTurnWait",
    "ContractCallerDriver",
    "DriverResult",
    "PublishDrainTimeout",
    "PublishSink",
]
