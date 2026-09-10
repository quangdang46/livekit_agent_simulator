"""Unit tests for the Contract Caller Driver (single path, no LiveKit)."""

from __future__ import annotations

import pytest

from livekit_agent_simulator.caller_contract import (
    EndedBy,
    FailureReason,
    GenerationIdentity,
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
    """LanguageBackendProtocol stub: scripted in-contract replies."""

    def __init__(self, utterances: list[dict] | None = None):
        self._utterances = list(utterances or [])
        self.calls = 0

    def generate(self, context):
        self.calls += 1
        if self._utterances:
            return self._utterances.pop(0)
        behavior = context["current_behavior"]["act"]
        target = context["current_behavior"]["target"]
        if behavior == "negotiate" and target == "price":
            return {
                "act": "negotiate",
                "target": "price",
                "slots": {"max_budget": 30000},
                "utterance": "Would you be able to come down to $30,000?",
            }
        return {"act": behavior, "target": target, "slots": {}, "utterance": "Could you tell me more?"}


class FakeSink:
    """PublishSink stand-in: records publishes, drops stale identities."""

    def __init__(self, orch: Orchestrator):
        self.orch = orch
        self.published: list[tuple[bytes, str]] = []
        self.dropped = 0

    def publish(self, pcm, identity, *, label, gain=1.0):
        if self.orch.is_stale(identity):
            self.dropped += 1
            return False
        self.published.append((pcm, label))
        return True


class FakeAgent:
    """AgentTurnWait stand-in: scripted agent replies."""

    def __init__(self, replies: list[str] | None = None):
        self._replies = list(replies or ["I can do $30,000."])
        self.waits = 0

    async def wait_agent_turn(self, *, timeout_s: float = 30.0):
        self.waits += 1
        if self._replies:
            return self._replies.pop(0)
        return "Sure, that works for me."


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
async def test_say_bypasses_ai_but_crosses_turn_gate():
    driver, orch = _driver()
    actions = parse_steps([{"say": "Hi, I'm calling about the car."}], file="t")
    sink, agent = FakeSink(orch), FakeAgent()
    result = await driver.run(actions, sink, agent)
    assert result.ended_by == EndedBy.SCENARIO
    assert result.turns_spoken == 1
    assert len(sink.published) == 1
    # Backend (AI) never invoked for say.
    assert driver.adapter.backend.calls == 0


@pytest.mark.asyncio
async def test_do_validates_then_publishes():
    driver, orch = _driver()
    actions = parse_steps(
        [
            {
                "do": {
                    "behavior": "negotiate",
                    "target": "price",
                    "constraints": {"max_turns": 3, "max_budget": 30000},
                }
            }
        ],
        file="t",
    )
    sink, agent = FakeSink(orch), FakeAgent(replies=["I can do $30,000."])
    result = await driver.run(actions, sink, agent)
    assert result.failure is None
    assert result.behaviors_completed == 1
    assert result.turns_spoken == 1
    assert driver.adapter.backend.calls >= 1


@pytest.mark.asyncio
async def test_do_invalid_exhaustion_stops_with_violation():
    backend = _ScriptedBackend(
        utterances=[
            {
                "act": "negotiate",
                "target": "price",
                "slots": {},
                "utterance": "Do you offer financing options?",
            }
        ]
        * 6
    )
    driver, orch = _driver(backend=backend)
    actions = parse_steps(
        [
            {
                "do": {
                    "behavior": "negotiate",
                    "target": "price",
                    "constraints": {
                        "max_turns": 3,
                        "forbidden_intents": ["financing"],
                    },
                }
            }
        ],
        file="t",
    )
    sink, agent = FakeSink(orch), FakeAgent()
    result = await driver.run(actions, sink, agent)
    assert result.failure is not None
    assert result.failure.reason == FailureReason.CALLER_BEHAVIOR_VIOLATION
    # Rejected candidate is NEVER published.
    assert len(sink.published) == 0


@pytest.mark.asyncio
async def test_full_say_do_end_lifecycle():
    driver, orch = _driver()
    actions = parse_steps(
        [
            {"say": "Hi, I'm calling about the 2022 Honda CR-V."},
            {"do": "ask"},
            {
                "do": {
                    "behavior": "negotiate",
                    "target": "price",
                    "constraints": {"max_turns": 3, "max_budget": 30000},
                }
            },
            {"say": "Thanks, bye."},
            {"end": True},
        ],
        file="t",
    )
    sink = FakeSink(orch)
    agent = FakeAgent(replies=["Sure, the asking price is $32,000.", "Sure, I can do $30,000."])
    events: list[tuple[str, dict]] = []
    result = await driver.run(
        actions, sink, agent, emit=lambda kind, spec=None: events.append((kind, spec or {}))
    )
    assert result.ended_by == EndedBy.SCENARIO
    assert result.failure is None
    assert result.behaviors_completed == 2
    assert result.turns_spoken == 4  # 2 say + 2 do
    kinds = [k for k, _ in events]
    assert "contract.say_published" in kinds
    assert "contract.turn_published" in kinds
    assert "contract.end" in kinds


@pytest.mark.asyncio
async def test_stale_identity_dropped_never_published():
    driver, orch = _driver()
    actions = parse_steps([{"say": "Hello."}], file="t")
    sink = FakeSink(orch)

    stale = GenerationIdentity(behavior_id="stale", turn_id=0, generation_id=0)
    assert sink.publish(b"\x00" * 10, stale, label="stale") is False
    assert sink.dropped == 1

    agent = FakeAgent()
    result = await driver.run(actions, sink, agent)
    assert result.turns_spoken == 1
    assert len(sink.published) == 1
