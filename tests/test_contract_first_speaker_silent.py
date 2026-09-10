"""Step 1 acceptance: first_speaker + silent_mode in the contract driver.

No network, no LiveKit — pure unit tests of ContractCallerDriver.run():

- first_speaker="agent": the driver waits for the agent greeting BEFORE
  the first caller action (contract equivalent of the legacy
  nudge_caller_after_agent_greeting); no caller audio is generated until
  the agent has demonstrably spoken. Greeting agent silence is
  AGENT_TIMEOUT, never a caller violation.
- first_speaker="user" (default): immediate start, unchanged behavior.
- silent_mode=True: say/do actions are skipped (no AI, no TTS, no
  publish — the caller stays mute) while control actions still run.
  Mirrors the legacy silent-mode compile (speak steps dropped,
  wait/hang_up kept).
"""

from __future__ import annotations

import pytest

from livekit_agent_simulator.caller_contract import (
    EndedBy,
    FailureReason,
)
from livekit_agent_simulator.caller_contract.driver import (
    ContractCallerDriver,
)
from livekit_agent_simulator.caller_contract.dsl import parse_steps
from livekit_agent_simulator.caller_contract.language_adapter import (
    AILanguageAdapter,
)
from livekit_agent_simulator.caller_contract.orchestrator import Orchestrator
from livekit_agent_simulator.caller_contract.semantic import (
    RuleBasedSemanticVerifier,
)
from livekit_agent_simulator.caller_contract.validator import ContractValidator


class _ScriptedBackend:
    def __init__(self):
        self.calls = 0

    def generate(self, context):
        self.calls += 1
        behavior = context["current_behavior"]["act"]
        target = context["current_behavior"]["target"]
        return {
            "act": behavior,
            "target": target,
            "slots": {},
            "utterance": "Could you tell me more?",
        }


class FakeSink:
    def __init__(self, orch: Orchestrator):
        self.orch = orch
        self.published: list[tuple[bytes, str]] = []

    async def publish(self, pcm, identity, *, label, gain=1.0):
        if self.orch.is_stale(identity):
            return False
        self.published.append((pcm, label))
        return True


class FakeAgent:
    """Scripted agent replies; ``replies=None`` means agent stays silent."""

    def __init__(self, replies: list[str] | None = None):
        self._replies = list(replies) if replies is not None else []
        self.waits = 0

    async def wait_agent_turn(self, *, timeout_s: float = 30.0):
        self.waits += 1
        if self._replies:
            return self._replies.pop(0)
        return None


def _driver(backend=None, orch=None):
    orch = orch or Orchestrator()
    return (
        ContractCallerDriver(
            orchestrator=orch,
            validator=ContractValidator(semantic_verifier=RuleBasedSemanticVerifier()),
            adapter=AILanguageAdapter(backend=backend or _ScriptedBackend()),
            synthesize=lambda text: b"\x00\x01" * 100,
        ),
        orch,
    )


@pytest.mark.asyncio
async def test_first_speaker_agent_waits_for_greeting_before_say():
    driver, orch = _driver()
    actions = parse_steps([{"say": "Hi, I'm calling about the car."}], file="t")
    sink = FakeSink(orch)
    agent = FakeAgent(replies=["Hello, thanks for calling support."])
    events: list[tuple[str, dict]] = []
    result = await driver.run(
        actions,
        sink,
        agent,
        emit=lambda kind, spec=None: events.append((kind, spec or {})),
        first_speaker="agent",
    )
    assert result.failure is None
    assert result.turns_spoken == 1
    assert len(sink.published) == 1
    kinds = [k for k, _ in events]
    assert "contract.first_speaker_wait" in kinds
    assert "contract.first_speaker_greeting" in kinds


@pytest.mark.asyncio
async def test_first_speaker_agent_silence_is_agent_timeout_not_violation():
    driver, orch = _driver()
    actions = parse_steps([{"say": "Hi, I'm calling about the car."}], file="t")
    sink = FakeSink(orch)
    agent = FakeAgent(replies=None)  # agent never speaks
    result = await driver.run(actions, sink, agent, first_speaker="agent")
    assert result.failure is not None
    assert result.failure.reason == FailureReason.AGENT_TIMEOUT
    assert result.failure.reason != FailureReason.CALLER_BEHAVIOR_VIOLATION
    assert len(sink.published) == 0  # no caller audio before the greeting
    assert result.turns_spoken == 0


@pytest.mark.asyncio
async def test_first_speaker_user_starts_immediately():
    driver, orch = _driver()
    actions = parse_steps([{"say": "Hi, I'm calling about the car."}], file="t")
    sink = FakeSink(orch)
    agent = FakeAgent(replies=None)
    result = await driver.run(actions, sink, agent, first_speaker="user")
    assert result.failure is None
    assert result.turns_spoken == 1
    assert agent.waits == 0  # no greeting wait consumed


@pytest.mark.asyncio
async def test_silent_mode_skips_say_and_do_but_runs_controls():
    backend = _ScriptedBackend()
    driver, orch = _driver(backend=backend)
    actions = parse_steps(
        [
            {"say": "Hi there."},
            {"do": {"behavior": "ask", "constraints": {"max_turns": 2}}},
            {"wait": 10},
            {"end": True},
        ],
        file="t",
    )
    sink = FakeSink(orch)
    agent = FakeAgent(replies=["Hello."])
    events: list[tuple[str, dict]] = []
    result = await driver.run(
        actions,
        sink,
        agent,
        emit=lambda kind, spec=None: events.append((kind, spec or {})),
        silent_mode=True,
    )
    assert result.failure is None
    assert result.ended_by == EndedBy.SCENARIO
    assert len(sink.published) == 0  # caller stayed mute
    assert backend.calls == 0  # AI never invoked
    assert agent.waits == 0  # no agent wait consumed either
    kinds = [k for k, _ in events]
    assert "contract.silent_skip" in kinds
    assert "contract.wait" in kinds
    assert "contract.end" in kinds


@pytest.mark.asyncio
async def test_silent_mode_with_first_speaker_agent_still_waits_for_greeting():
    """Silent mode mutes the CALLER, not the agent: first_speaker=agent
    still observes the greeting (the dead-air scenario needs the agent's
    greeting in the log even though the caller never replies)."""
    driver, orch = _driver()
    actions = parse_steps([{"say": "Hi there."}], file="t")
    sink = FakeSink(orch)
    agent = FakeAgent(replies=["Hello, anyone there?"])
    result = await driver.run(
        actions, sink, agent, first_speaker="agent", silent_mode=True
    )
    assert result.failure is None
    assert len(sink.published) == 0
    assert agent.waits == 1
