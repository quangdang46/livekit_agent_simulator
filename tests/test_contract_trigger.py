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


def _driver(orch=None):
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
                        "generate": lambda self, ctx: {
                            "act": "ask",
                            "target": None,
                            "slots": {},
                            "utterance": "hi",
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
