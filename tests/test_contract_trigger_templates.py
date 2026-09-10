"""Slice 4 acceptance: migrated script templates run the contract path.

Loads the real YAML templates (with their new caller_steps:) and drives
them through the real ContractCallerDriver with a scripted speaking
timeline — proving ``template -> caller_steps -> trigger-wait -> publish``.

- people-pleaser-hangup-threat: barge threat (agent_speaking + barge_in)
  then timed bye (time) then end. first_speaker=agent consumes the greeting.
- interrupt-correction: silence-gated open, barge correction
  (agent_speaking + barge_in), silence-gated bye, end. first_speaker=user
  starts immediately.

No network, no LiveKit. Timing hermetic: the templates' real delays
(800ms-12s) are overridden to small values after parse so the tests run
in milliseconds while asserting the same trigger kinds/order.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from livekit_agent_simulator.caller_contract import EndedBy
from livekit_agent_simulator.caller_contract.driver import ContractCallerDriver
from livekit_agent_simulator.caller_contract.language_adapter import AILanguageAdapter
from livekit_agent_simulator.caller_contract.orchestrator import Orchestrator
from livekit_agent_simulator.caller_contract.semantic import RuleBasedSemanticVerifier
from livekit_agent_simulator.caller_contract.validator import ContractValidator
from livekit_agent_simulator.scenario import parse_scenario

TEMPLATES = Path(__file__).resolve().parents[1] / "templates" / "examples"


class FakeAgent:
    def __init__(
        self,
        replies: list[str] | None = None,
        speaking: list[bool] | None = None,
    ):
        self._replies = list(replies) if replies is not None else []
        self._speaking = list(speaking) if speaking is not None else []

    async def wait_agent_turn(self, *, timeout_s: float = 30.0):
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


def _shrink_delays(scenario) -> None:
    """Override real-world delays to test-scale; kinds/order untouched."""
    for action in scenario.caller_actions:
        if action.trigger is not None:
            action.trigger.delay_ms = min(action.trigger.delay_ms, 20)
            action.trigger.min_agent_active_ms = min(
                action.trigger.min_agent_active_ms, 40
            )


@pytest.mark.asyncio
async def test_hangup_threat_template_barge_then_timed_bye():
    scenario = parse_scenario(TEMPLATES / "people-pleaser-hangup-threat.yaml")
    assert len(scenario.caller_actions) == 3
    assert scenario.caller_actions[0].barge_in is True
    assert scenario.caller_actions[0].trigger is not None
    assert scenario.caller_actions[0].trigger.kind == "agent_speaking"
    assert scenario.caller_actions[1].trigger is not None
    assert scenario.caller_actions[1].trigger.kind == "time"
    _shrink_delays(scenario)

    driver, orch = _driver()
    sink = FakeSink(orch)
    # Greeting (first_speaker=agent) + sustained speech for the barge trigger.
    agent = FakeAgent(
        replies=["Hello, let me pull up the main menu for you."],
        speaking=[True] * 60,
    )
    texts: list[str] = []
    orig_speak = driver._speak
    driver._speak = lambda text: (texts.append(text), orig_speak(text))[1]
    result = await driver.run(
        scenario.caller_actions,
        sink,
        agent,
        first_speaker=scenario.run_spec.first_speaker,
    )
    assert result.failure is None
    assert result.ended_by == EndedBy.SCENARIO
    assert texts == [
        "If you restart the main menu I will hang up.",
        "I am hanging up now.",
    ]


@pytest.mark.asyncio
async def test_interrupt_correction_template_silence_barge_silence():
    scenario = parse_scenario(TEMPLATES / "interrupt-correction.yaml")
    assert len(scenario.caller_actions) == 4
    kinds = [a.trigger.kind if a.trigger else None for a in scenario.caller_actions[:3]]
    assert kinds == ["silence", "agent_speaking", "silence"]
    assert scenario.caller_actions[1].barge_in is True
    _shrink_delays(scenario)

    driver, orch = _driver()
    sink = FakeSink(orch)
    # Silent opening, sustained speech for the barge, silence for the bye.
    agent = FakeAgent(speaking=[False] * 10 + [True] * 60 + [False] * 60)
    texts: list[str] = []
    orig_speak = driver._speak
    driver._speak = lambda text: (texts.append(text), orig_speak(text))[1]
    result = await driver.run(
        scenario.caller_actions,
        sink,
        agent,
        first_speaker=scenario.run_spec.first_speaker,
    )
    assert result.failure is None
    assert result.ended_by == EndedBy.SCENARIO
    assert texts == [
        "Hi, I want to sign up for a basic plan.",
        "Wait — how much is the monthly fee?",
        "Thanks, that's all for now. Bye.",
    ]
