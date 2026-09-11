"""Slice 4 acceptance: minimal trigger/delay execution in the contract driver.

No network, no LiveKit — FakeAgent carries a scripted speaking timeline
(``speaking: list[bool]`` sampled once per poll, then held at the last
value), FakeSink records publishes.

Covers:
- DSL: 3 kinds parse with defaults; strictness (unknown kind/keys,
  sibling-hole, trigger/barge_in restricted to say:).
- time: fires after delay_ms.
- agent_speaking: fires only after continuous min_agent_active_ms;
  flicker resets the clock (naive reset = documented Slice 4 limitation).
- silence: fires after continuous delay_ms of silence; resets on speech.
- barge_in=True skips the non-barge agent-silence gate; False waits.
- trigger budget expiry -> BEHAVIOR_TIMEOUT (monkeypatched budget).
"""

from __future__ import annotations

import pytest

from livekit_agent_simulator.caller_contract import (
    EndedBy,
    FailureReason,
)
from livekit_agent_simulator.caller_contract import driver as driver_mod
from livekit_agent_simulator.caller_contract.driver import (
    ContractCallerDriver,
)
from livekit_agent_simulator.caller_contract.dsl import DSLError, parse_steps
from livekit_agent_simulator.caller_contract.language_adapter import (
    AILanguageAdapter,
)
from livekit_agent_simulator.caller_contract.orchestrator import Orchestrator
from livekit_agent_simulator.caller_contract.semantic import (
    RuleBasedSemanticVerifier,
)
from livekit_agent_simulator.caller_contract.validator import ContractValidator


class FakeAgent:
    """wait_agent_turn scripted; speaking sampled per poll from a timeline."""

    def __init__(
        self,
        replies: list[str] | None = None,
        speaking: list[bool] | None = None,
    ):
        self._replies = list(replies) if replies is not None else []
        self._speaking = list(speaking) if speaking is not None else []
        self.waits = 0

    async def wait_agent_turn(self, *, timeout_s: float = 30.0):
        self.waits += 1
        if self._replies:
            return self._replies.pop(0)
        return None

    def is_agent_speaking_now(self) -> bool:
        if self._speaking:
            return self._speaking.pop(0)
        return False


class FakeSink:
    def __init__(self, orch: Orchestrator):
        self.orch = orch
        self.published: list[tuple[bytes, str]] = []

    async def publish(self, pcm, identity, *, label, gain=1.0):
        if self.orch.is_stale(identity):
            return False
        self.published.append((pcm, label))
        return True


def _driver(orch=None, utterance: str = "Could you tell me more?"):
    """Scripted backend: echoes the behavior/target with a fixed utterance
    that passes the rule-based verifier as ``ask`` (default) or the given
    text for callers that need a specific valid line."""
    orch = orch or Orchestrator()
    return (
        ContractCallerDriver(
            orchestrator=orch,
            validator=ContractValidator(semantic_verifier=RuleBasedSemanticVerifier()),
            adapter=AILanguageAdapter(
                backend=type(
                    "B",
                    (),
                    {
                        "generate": lambda self, ctx, _u=utterance: {
                            "act": ctx["current_behavior"]["act"],
                            "target": ctx["current_behavior"]["target"],
                            "slots": {},
                            "utterance": _u,
                        }
                    },
                )()
            ),
            synthesize=lambda text: b"\x00\x01" * 10,
        ),
        orch,
    )


# --- DSL parse ---


def test_trigger_time_parses_with_defaults():
    action = parse_steps(
        [{"say": "Hi.", "trigger": {"kind": "time", "delay_ms": 500}}], file="t"
    )[0]
    assert action.trigger is not None
    assert action.trigger.kind == "time"
    assert action.trigger.delay_ms == 500
    assert action.trigger.min_agent_active_ms == 400  # legacy mirror default
    assert action.barge_in is False


def test_trigger_agent_speaking_parses():
    action = parse_steps(
        [
            {
                "say": "One sec.",
                "trigger": {
                    "kind": "agent_speaking",
                    "min_agent_active_ms": 350,
                    "delay_ms": 100,
                },
                "barge_in": True,
            }
        ],
        file="t",
    )[0]
    assert action.trigger is not None
    assert action.trigger.kind == "agent_speaking"
    assert action.trigger.min_agent_active_ms == 350
    assert action.trigger.delay_ms == 100
    assert action.barge_in is True


def test_trigger_silence_parses():
    action = parse_steps(
        [{"say": "Hello?", "trigger": {"kind": "silence", "delay_ms": 700}}],
        file="t",
    )[0]
    assert action.trigger is not None
    assert action.trigger.kind == "silence"
    assert action.trigger.delay_ms == 700


def test_say_without_trigger_has_no_gate():
    action = parse_steps([{"say": "Hi."}], file="t")[0]
    assert action.trigger is None
    assert action.barge_in is False


@pytest.mark.parametrize(
    "step",
    [
        {"say": "Hi.", "frobnicate": 1},
        {"say": "Hi.", "trigger": {"kind": "banana"}},
        {"say": "Hi.", "trigger": {"delay_ms": 5}},
        {"say": "Hi.", "trigger": {"kind": "time", "delay_ms": -1}},
        {"say": "Hi.", "trigger": {"kind": "time", "bogus": 1}},
        {"say": "Hi.", "trigger": "time"},
        {"say": "Hi.", "barge_in": "yes"},
        {"wait": 10, "trigger": {"kind": "time"}},
        {"wait": 10, "barge_in": True},
        {"dtmf": "123", "trigger": {"kind": "time"}},
        {"end": True, "barge_in": True},
        {"do": "ask", "trigger": {"kind": "time"}},
        {"do": "ask", "barge_in": True},
    ],
)
def test_trigger_strictness(step):
    with pytest.raises(DSLError):
        parse_steps([step], file="t")


# --- execution ---


@pytest.mark.asyncio
async def test_gap_tolerance_survives_brief_dropout():
    """A signal dropout shorter than TRIGGER_GAP_TOLERANCE_S must not reset
    the agent_speaking continuity clock (legacy 1200ms parity)."""
    import livekit_agent_simulator.caller_contract.driver as driver_mod

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(driver_mod, "TRIGGER_GAP_TOLERANCE_S", 0.3)
    try:
        driver, orch = _driver()
        actions = parse_steps(
            [
                {
                    "say": "One second.",
                    "trigger": {
                        "kind": "agent_speaking",
                        "min_agent_active_ms": 150,
                        "delay_ms": 0,
                    },
                    "barge_in": True,
                }
            ],
            file="t",
        )
        sink = FakeSink(orch)
        # Sustained speech with one short dropout mid-way: with naive reset
        # this would restart the clock; with gap tolerance it still fires.
        agent = FakeAgent(speaking=[True] * 3 + [False] + [True] * 20)
        result = await driver.run(actions, sink, agent)
    finally:
        monkeypatch.undo()
    assert result.failure is None
    assert len(sink.published) == 1


@pytest.mark.asyncio
async def test_long_gap_resets_continuity_clock(monkeypatch):
    """A dropout LONGER than the gap tolerance resets the clock: speech that
    never sustains min_agent_active_ms within budget -> BEHAVIOR_TIMEOUT."""
    monkeypatch.setattr(driver_mod, "TRIGGER_GAP_TOLERANCE_S", 0.05)
    monkeypatch.setattr(driver_mod, "TRIGGER_WAIT_BUDGET_S", 0.4)
    driver, orch = _driver()
    actions = parse_steps(
        [
            {
                "say": "One second.",
                "trigger": {
                    "kind": "agent_speaking",
                    "min_agent_active_ms": 10000,
                    "delay_ms": 0,
                },
                "barge_in": True,
            }
        ],
        file="t",
    )
    sink = FakeSink(orch)
    agent = FakeAgent(speaking=[True, False] + [True, False] * 20)
    result = await driver.run(actions, sink, agent)
    assert result.failure is not None
    assert result.failure.reason == FailureReason.BEHAVIOR_TIMEOUT
    assert len(sink.published) == 0


@pytest.mark.asyncio
async def test_interruption_policy_is_deterministic_and_gated():
    """Seeded policy: same (scenario, seed, turn) decides the same way;
    silent agent never gets interrupted; interval gates repeats."""
    import asyncio as _asyncio

    from livekit_agent_simulator.caller_contract.dsl import InteractionConfig
    from livekit_agent_simulator.caller_contract.interaction_planner import (
        CallerInteractionPlanner,
    )

    planner = CallerInteractionPlanner()
    ic = InteractionConfig(interruption_rate="high", interruption_seed=3)
    first = [planner.should_interrupt(ic, scenario_id="s", agent_turn_index=i) for i in range(8)]
    second = [planner.should_interrupt(ic, scenario_id="s", agent_turn_index=i) for i in range(8)]
    assert first == second
    assert planner.should_interrupt(None, scenario_id="s", agent_turn_index=0) is False

    # Driver: speaking agent + favorable flip -> policy_interrupt published.
    driver, orch = _driver()
    driver._scenario_id = "interrupt-rate-medium"
    seed_yes = next(
        s
        for s in range(50)
        if planner.should_interrupt(
            InteractionConfig(interruption_rate="high", interruption_seed=s),
            scenario_id="interrupt-rate-medium",
            agent_turn_index=0,
        )
    )
    actions = parse_steps(
        [
            {
                "do": {
                    "behavior": "ask",
                    "constraints": {"max_turns": 1},
                    "interaction": {
                        "interruption_rate": "high",
                        "interruption_seed": seed_yes,
                        "interruption_interval_ms": 1000,
                    },
                }
            }
        ],
        file="t",
    )
    sink = FakeSink(orch)

    class _SpeakingThenReply(FakeAgent):
        """Replies only after several polls so the policy sees active
        speech mid-wait (P1: decision happens WHILE the agent speaks)."""

        def __init__(self):
            super().__init__(replies=[], speaking=[])
            self._polls = 0

        async def wait_agent_turn(self, *, timeout_s: float = 30.0):
            self.waits += 1
            self._polls += 1
            if self._polls < 4:
                await __import__("asyncio").sleep(timeout_s)
                return None
            return "Sure, open 9 to 5."

        def is_agent_speaking_now(self) -> bool:
            return True

    agent = _SpeakingThenReply()
    result = await driver.run(actions, sink, agent)
    assert result.failure is None
    assert any(label == "policy_interrupt" for _, label in sink.published)

    # Driver: silent agent -> never interrupted even with favorable flip.
    driver2, orch2 = _driver()
    driver2._scenario_id = "interrupt-rate-medium"
    actions2 = parse_steps(
        [
            {
                "do": {
                    "behavior": "ask",
                    "constraints": {"max_turns": 1},
                    "interaction": {
                        "interruption_rate": "high",
                        "interruption_seed": seed_yes,
                        "interruption_interval_ms": 1000,
                    },
                }
            }
        ],
        file="t",
    )
    sink2 = FakeSink(orch2)
    agent2 = FakeAgent(replies=["Sure, open 9 to 5."], speaking=[False] * 30)
    result2 = await driver2.run(actions2, sink2, agent2)
    assert result2.failure is None
    assert all(label != "policy_interrupt" for _, label in sink2.published)


@pytest.mark.asyncio
async def test_interrupt_publishes_immediately_without_validator():
    """interrupt: is an explicit interaction action (barge delivery): fixed
    text, no AI, no validator, immediate publish even mid-speech."""
    driver, orch = _driver()
    actions = parse_steps([{"interrupt": True}], file="t")
    sink = FakeSink(orch)
    agent = FakeAgent(speaking=[True] * 20)
    texts: list[str] = []
    orig_speak = driver._speak
    driver._speak = lambda text: (texts.append(text), orig_speak(text))[1]
    result = await driver.run(actions, sink, agent)
    assert result.failure is None
    assert texts == ["Wait — one second."]
    assert len(sink.published) == 1


@pytest.mark.asyncio
async def test_interrupt_backchannel_class_uses_backchannel_line():
    driver, orch = _driver()
    actions = parse_steps(
        [{"interrupt": True, "interaction": {"interrupt_class": "backchannel"}}],
        file="t",
    )
    sink = FakeSink(orch)
    agent = FakeAgent()
    texts: list[str] = []
    orig_speak = driver._speak
    driver._speak = lambda text: (texts.append(text), orig_speak(text))[1]
    result = await driver.run(actions, sink, agent)
    assert result.failure is None
    assert texts == ["Mhm."]


def test_agent_wait_speaking_signal_delegates_to_observer():
    """ObserverAgentWait.is_agent_speaking_now() is a one-line delegate to
    observer.agent_is_active_speaker (the realtime signal trigger-wait polls)."""
    from types import SimpleNamespace as _NS

    from livekit_agent_simulator.caller_contract.agent_wait import ObserverAgentWait

    assert ObserverAgentWait(observer=_NS(agent_is_active_speaker=True)).is_agent_speaking_now() is True
    assert ObserverAgentWait(observer=_NS(agent_is_active_speaker=False)).is_agent_speaking_now() is False
    assert ObserverAgentWait(observer=_NS()).is_agent_speaking_now() is False


@pytest.mark.asyncio
async def test_time_trigger_fires_after_delay():
    driver, orch = _driver()
    actions = parse_steps(
        [{"say": "Bye.", "trigger": {"kind": "time", "delay_ms": 120}}], file="t"
    )
    sink = FakeSink(orch)
    agent = FakeAgent()
    import time as _time

    t0 = _time.monotonic()
    result = await driver.run(actions, sink, agent)
    dt_ms = (_time.monotonic() - t0) * 1000.0
    assert result.failure is None
    assert len(sink.published) == 1
    assert dt_ms >= 120


@pytest.mark.asyncio
async def test_agent_speaking_fires_after_continuous_threshold():
    driver, orch = _driver()
    actions = parse_steps(
        [
            {
                "say": "One second.",
                "trigger": {
                    "kind": "agent_speaking",
                    "min_agent_active_ms": 100,
                    "delay_ms": 0,
                },
                "barge_in": True,
            }
        ],
        file="t",
    )
    sink = FakeSink(orch)
    # Silence first, then sustained speech: must not fire early.
    agent = FakeAgent(speaking=[False, False] + [True] * 20)
    result = await driver.run(actions, sink, agent)
    assert result.failure is None
    assert len(sink.published) == 1


@pytest.mark.asyncio
async def test_agent_speaking_flicker_resets_clock():
    """Naive reset on every False sample (documented Slice 4 limitation:
    no gap tolerance). A flicker before the threshold means the clock
    restarts — the say still fires once speech sustains afterwards."""
    driver, orch = _driver()
    actions = parse_steps(
        [
            {
                "say": "One second.",
                "trigger": {
                    "kind": "agent_speaking",
                    "min_agent_active_ms": 100,
                    "delay_ms": 0,
                },
                "barge_in": True,
            }
        ],
        file="t",
    )
    sink = FakeSink(orch)
    agent = FakeAgent(speaking=[True, False] + [True] * 20)
    result = await driver.run(actions, sink, agent)
    assert result.failure is None
    assert len(sink.published) == 1


@pytest.mark.asyncio
async def test_silence_fires_after_continuous_silence():
    driver, orch = _driver()
    actions = parse_steps(
        [{"say": "Hello?", "trigger": {"kind": "silence", "delay_ms": 100}}],
        file="t",
    )
    sink = FakeSink(orch)
    # Agent speaking first resets the silence clock; sustained silence after fires.
    agent = FakeAgent(speaking=[True, True] + [False] * 20)
    result = await driver.run(actions, sink, agent)
    assert result.failure is None
    assert len(sink.published) == 1


@pytest.mark.asyncio
async def test_silence_resets_on_speech():
    driver, orch = _driver()
    actions = parse_steps(
        [{"say": "Hello?", "trigger": {"kind": "silence", "delay_ms": 100}}],
        file="t",
    )
    sink = FakeSink(orch)
    agent = FakeAgent(speaking=[False, True] + [False] * 20)
    result = await driver.run(actions, sink, agent)
    assert result.failure is None
    assert len(sink.published) == 1


@pytest.mark.asyncio
async def test_barge_skips_silence_gate_but_non_barge_waits():
    # Barge: agent speaking throughout — publish must still happen.
    driver, orch = _driver()
    actions = parse_steps([{"say": "Stop.", "barge_in": True}], file="t")
    sink = FakeSink(orch)
    agent = FakeAgent(speaking=[True] * 50)
    result = await driver.run(actions, sink, agent)
    assert result.failure is None
    assert len(sink.published) == 1

    # Non-barge: agent speaking first, then silent — publish waits for silence.
    driver2, orch2 = _driver()
    actions2 = parse_steps([{"say": "Hi."}], file="t")
    sink2 = FakeSink(orch2)
    agent2 = FakeAgent(speaking=[True, True] + [False] * 20)
    result2 = await driver2.run(actions2, sink2, agent2)
    assert result2.failure is None
    assert len(sink2.published) == 1


@pytest.mark.asyncio
async def test_trigger_timeout_is_behavior_timeout(monkeypatch):
    monkeypatch.setattr(driver_mod, "TRIGGER_WAIT_BUDGET_S", 0.15)
    driver, orch = _driver()
    actions = parse_steps(
        [
            {
                "say": "One second.",
                "trigger": {
                    "kind": "agent_speaking",
                    "min_agent_active_ms": 10000,
                    "delay_ms": 0,
                },
                "barge_in": True,
            }
        ],
        file="t",
    )
    sink = FakeSink(orch)
    agent = FakeAgent(speaking=[False] * 100)
    events: list[tuple[str, dict]] = []
    result = await driver.run(
        actions,
        sink,
        agent,
        emit=lambda kind, spec=None: events.append((kind, spec or {})),
    )
    assert result.failure is not None
    assert result.failure.reason == FailureReason.BEHAVIOR_TIMEOUT
    assert result.ended_by == EndedBy.TIMEOUT
    assert len(sink.published) == 0
    kinds = [k for k, _ in events]
    assert "contract.trigger_armed" in kinds


@pytest.mark.asyncio
async def test_tts_flaky_retries_same_text_no_ai_recall():
    """TTS-only retry: flaky synth fails twice then succeeds — same text,
    AI backend called once (never re-invoked for an audio failure)."""
    calls = {"tts": 0, "ai": 0}

    class _Backend:
        def generate(self, context):
            calls["ai"] += 1
            return {
                "act": "ask",
                "target": None,
                "slots": {},
                "utterance": "Could you tell me more?",
            }

    def _flaky(text: str) -> bytes:
        calls["tts"] += 1
        if calls["tts"] <= 2:
            raise RuntimeError("synth boom")
        return b"\x00\x01" * 10

    orch = Orchestrator()
    driver = ContractCallerDriver(
        orchestrator=orch,
        validator=ContractValidator(semantic_verifier=RuleBasedSemanticVerifier()),
        adapter=AILanguageAdapter(backend=_Backend()),
        synthesize=_flaky,
    )
    from livekit_agent_simulator.caller_contract.driver import (
        ContractCallerDriver as _D,
    )

    assert _D is ContractCallerDriver  # import sanity
    sink = FakeSink(orch)
    agent = FakeAgent()
    result = await driver.run(parse_steps([{"say": "Hi."}], file="t"), sink, agent)
    assert result.failure is None
    assert calls["tts"] == 3


@pytest.mark.asyncio
async def test_tts_broken_maps_to_tts_error_not_violation():
    """Permanently broken TTS -> TTS_ERROR / EndedBy.ERROR (never
    CALLER_BEHAVIOR_VIOLATION, never TRANSPORT — the wire was untouched)."""
    from livekit_agent_simulator.caller_contract import EndedBy, FailureReason

    def _broken(text: str) -> bytes:
        raise RuntimeError("synth dead")

    orch = Orchestrator()
    driver = ContractCallerDriver(
        orchestrator=orch,
        validator=ContractValidator(semantic_verifier=RuleBasedSemanticVerifier()),
        adapter=AILanguageAdapter(
            backend=type(
                "B",
                (),
                {
                    "generate": lambda self, ctx: {
                        "act": "ask",
                        "target": None,
                        "slots": {},
                        "utterance": "Could you tell me more?",
                    }
                },
            )()
        ),
        synthesize=_broken,
    )
    sink = FakeSink(orch)
    agent = FakeAgent()
    result = await driver.run(parse_steps([{"say": "Hi."}], file="t"), sink, agent)
    assert result.failure is not None
    assert result.failure.reason == FailureReason.TTS_ERROR
    assert result.ended_by == EndedBy.ERROR
    assert len(sink.published) == 0


@pytest.mark.asyncio
async def test_hold_watchdog_fires_after_agent_dead_air():
    """Contract hold timeout (legacy hold_music_timeout_s equivalent):
    armed once the agent demonstrably spoke; fires on_hold_timeout +
    sim.hold_timeout after the silence budget."""
    import time as _time

    from livekit_agent_simulator.caller_contract.driver import (
        ContractCallerDriver as _Driver,
    )

    class _HoldAgent(FakeAgent):
        def __init__(self):
            super().__init__(replies=[])
            self._t0 = _time.monotonic()

        def last_speech_at_ms(self):
            return (self._t0 - 5.0) * 1000.0  # spoke 5s ago

    orch = Orchestrator()
    driver = _Driver(
        orchestrator=orch,
        validator=ContractValidator(semantic_verifier=RuleBasedSemanticVerifier()),
        adapter=AILanguageAdapter(
            backend=type(
                "B",
                (),
                {
                    "generate": lambda self, ctx: {
                        "act": "ask",
                        "target": None,
                        "slots": {},
                        "utterance": "Could you tell me more?",
                    }
                },
            )()
        ),
        synthesize=lambda text: b"\x00\x01" * 10,
    )
    fired: list[bool] = []
    events: list[tuple[str, dict]] = []
    result = await driver.run(
        parse_steps([{"wait": 1500}], file="t"),
        FakeSink(orch),
        _HoldAgent(),
        emit=lambda kind, spec=None: events.append((kind, spec or {})),
        hold_timeout_s=0.4,
        on_hold_timeout=lambda: fired.append(True),
    )
    assert result.failure is None
    assert fired == [True]
    assert any(k == "sim.hold_timeout" for k, _ in events)


@pytest.mark.asyncio
async def test_hold_watchdog_stays_off_before_agent_speaks():
    """No agent speech evidence -> watchdog never arms (same rule as the
    legacy loop: hold fires only after agent_has_spoken)."""

    class _QuietAgent(FakeAgent):
        def last_speech_at_ms(self):
            return None

    orch = Orchestrator()
    driver, _ = _driver(orch)
    fired: list[bool] = []
    result = await driver.run(
        parse_steps([{"wait": 400}], file="t"),
        FakeSink(orch),
        _QuietAgent(),
        hold_timeout_s=0.1,
        on_hold_timeout=lambda: fired.append(True),
    )
    assert result.failure is None
    assert fired == []


def test_last_speech_prefers_active_speaker_over_stale_final():
    """An agent mid-utterance (active flag up, final 10s old) reads as
    speaking NOW — never as 10s of dead air (hold-watchdog regression)."""
    import time as _time
    from types import SimpleNamespace as _NS

    from livekit_agent_simulator.caller_contract.agent_wait import ObserverAgentWait

    stale_mono = _time.monotonic() - 10.0
    wait = ObserverAgentWait(
        observer=_NS(last_agent_final_mono=stale_mono, agent_is_active_speaker=True)
    )
    assert wait.last_speech_at_ms() is not None
    assert abs(wait.last_speech_at_ms() - _time.monotonic() * 1000.0) < 1000.0

    quiet = ObserverAgentWait(
        observer=_NS(last_agent_final_mono=stale_mono, agent_is_active_speaker=False)
    )
    assert quiet.last_speech_at_ms() == stale_mono * 1000.0

    never = ObserverAgentWait(observer=_NS(agent_is_active_speaker=False))
    assert never.last_speech_at_ms() is None


@pytest.mark.parametrize(
    "step",
    [
        {"end": True, "typo_field": 1},
        {"silence": True, "trigger": {"kind": "time"}},
        {"hangup": True, "barge_in": True},
        {"end": "now"},
        {"silence": True, "interaction": {}},
    ],
)
def test_end_silence_hangup_reject_siblings(step):
    """Presence-only control actions: any sibling/value is a hard DSLError
    (P1 review fix — unknown fields must never silently disappear)."""
    with pytest.raises(DSLError):
        parse_steps([step], file="t")
