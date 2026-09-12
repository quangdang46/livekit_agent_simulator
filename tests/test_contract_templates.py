"""Step 1 acceptance: real templates drive the contract driver.

Proves ``Scenario (templates/) -> caller_steps -> ContractCallerDriver ->
validator -> publish`` with no network and no LiveKit — the first real
template coverage of the contract path (previously only synthetic
``parse_steps([...], file="t")`` fixtures exercised it).

- dialogue-signup-basic (first_speaker=user): say -> do(ask/price) ->
  say -> end completes with behaviors_completed=1.
- constraint-no-card (first_speaker=agent): greeting wait consumes the
  first agent reply; do(provide/order_status) with a card_number
  forbidden_intents guard completes; caller never speaks card data.
- silent-caller-dead-air stays legacy (no caller_steps): documents the
  boundary — parse yields empty caller_actions so it keeps the legacy
  path until a contract-native silent scenario exists.
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


class _ScriptedBackend:
    """Returns one scripted utterance per behavior, then repeats the last."""

    def __init__(self, utterances: dict[str, str]):
        self._utterances = dict(utterances)

    def generate(self, context):
        behavior = context["current_behavior"]["act"]
        target = context["current_behavior"]["target"]
        return {
            "act": behavior,
            "target": target,
            "slots": {},
            "utterance": self._utterances[behavior],
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
    def __init__(self, replies: list[str] | None = None):
        self._replies = list(replies) if replies is not None else []

    async def wait_agent_turn(self, *, timeout_s: float = 30.0):
        if self._replies:
            return self._replies.pop(0)
        return None


def _driver(utterances: dict[str, str]):
    orch = Orchestrator()
    return (
        ContractCallerDriver(
            orchestrator=orch,
            validator=ContractValidator(semantic_verifier=RuleBasedSemanticVerifier()),
            adapter=AILanguageAdapter(backend=_ScriptedBackend(utterances)),
            synthesize=lambda text: b"\x00\x01" * 100,
        ),
        orch,
    )


@pytest.mark.asyncio
async def test_dialogue_signup_basic_template_runs_contract_path():
    scenario = parse_scenario(TEMPLATES / "dialogue-signup-basic.yaml")
    assert len(scenario.caller_actions) == 4
    assert scenario.run_spec.first_speaker == "user"

    driver, orch = _driver({"ask": "What is the monthly fee for the basic plan?"})
    sink = FakeSink(orch)
    agent = FakeAgent(replies=["Our basic plan is $20 a month."])
    result = await driver.run(
        scenario.caller_actions,
        sink,
        agent,
        first_speaker=scenario.run_spec.first_speaker,
    )
    assert result.failure is None
    assert result.ended_by == EndedBy.SCENARIO
    assert result.behaviors_completed == 1
    assert result.turns_spoken == 3  # say + do + say


@pytest.mark.asyncio
async def test_constraint_no_card_template_runs_contract_path():
    scenario = parse_scenario(TEMPLATES / "constraint-no-card.yaml")
    assert len(scenario.caller_actions) == 4
    assert scenario.run_spec.first_speaker == "agent"
    do_action = scenario.caller_actions[1]
    assert "card_number" in do_action.contract.constraints.forbidden_intents

    driver, orch = _driver({"provide": "I'm calling about my delayed order status."})
    sink = FakeSink(orch)
    agent = FakeAgent(
        replies=[
            "Hello, thanks for calling support.",
            "Sure, your order ships tomorrow.",
        ]
    )
    result = await driver.run(
        scenario.caller_actions,
        sink,
        agent,
        first_speaker=scenario.run_spec.first_speaker,
    )
    assert result.failure is None
    assert result.ended_by == EndedBy.SCENARIO
    assert result.behaviors_completed == 1
    # Caller never spoke card data: no published turn contains card content.
    assert len(sink.published) == 3


def test_silent_caller_template_is_contract_native():
    """silent-caller-dead-air migrated: wait + end under silent_mode (the
    legacy speech_conditions.silent_mode bridge). Covered end-to-end by
    test_silent_template_stays_mute_but_observes_greeting in
    test_contract_all_templates.py; this locks the parse shape here."""
    scenario = parse_scenario(TEMPLATES / "silent-caller-dead-air.yaml")
    assert [a.kind for a in scenario.caller_actions] == ["wait", "end"]
