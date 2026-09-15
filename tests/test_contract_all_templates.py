"""Migration gate: every bundled template drives the contract path.

Parses every ``templates/*.yaml`` + ``templates/examples/*.yaml`` and
asserts non-empty ``caller_actions`` — i.e. no template still depends on
the legacy persona+ScriptRunner path in run_orchestrator. Then spot-drives
representative templates through the real ContractCallerDriver:

- persona-thuần (multi-judge-smoke): say -> do -> say -> end.
- hold/timeout (hold-timeout-agent-stall): say -> wait -> say -> wait -> end.
- silent (silent-caller-dead-air): silent_mode drops everything but
  wait/end; the caller stays mute (no publishes) while the greeting is
  still observed.
- ambient bed (ambient-loop-office): play_audio resolves against the real
  asset catalog (builtin:noise.ambient exists) with a stub player.

No network, no LiveKit.
"""

from __future__ import annotations

import glob
from pathlib import Path

import pytest

from livekit_agent_simulator.caller_contract import EndedBy
from livekit_agent_simulator.caller_contract.driver import ContractCallerDriver
from livekit_agent_simulator.caller_contract.dsl import parse_steps
from livekit_agent_simulator.caller_contract.language_adapter import AILanguageAdapter
from livekit_agent_simulator.caller_contract.orchestrator import Orchestrator
from livekit_agent_simulator.caller_contract.semantic import RuleBasedSemanticVerifier
from livekit_agent_simulator.caller_contract.validator import ContractValidator
from livekit_agent_simulator.scenario import parse_scenario

ROOT = Path(__file__).resolve().parents[1] / "templates"
EXAMPLES = ROOT / "examples"


def _all_templates() -> list[Path]:
    return sorted(
        [*(ROOT.glob("*.yaml")), *(EXAMPLES.glob("*.yaml"))],
        key=lambda p: p.name,
    )


def test_every_template_has_caller_steps():
    # scenario-scaffold.yaml is the authoring template (placeholder id,
    # all sections commented out) — it documents caller_steps: but carries
    # none itself. Everything else must drive the contract path.
    missing = []
    for path in _all_templates():
        if path.name in ("config.yaml", "scenario-scaffold.yaml"):
            continue
        scenario = parse_scenario(path)
        if not scenario.caller_actions:
            missing.append(path.name)
    assert missing == [], f"templates still on legacy path: {missing}"


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


class FakeAssets:
    def __init__(self):
        self.played: list[tuple[str, float, bool]] = []

    async def play_asset(self, asset, *, gain, loop, label):
        self.played.append((asset, gain, loop))
        return True


def _driver(utterances: dict[str, str] | None = None, orch=None):
    utterances = utterances or {}
    orch = orch or Orchestrator()

    class _Backend:
        def generate(self, context):
            behavior = context["current_behavior"]["act"]
            target = context["current_behavior"]["target"]
            return {
                "act": behavior,
                "target": target,
                "slots": {},
                "utterance": utterances.get(behavior, "Could you tell me more?"),
            }

    return (
        ContractCallerDriver(
            orchestrator=orch,
            validator=ContractValidator(semantic_verifier=RuleBasedSemanticVerifier()),
            adapter=AILanguageAdapter(backend=_Backend()),
            synthesize=lambda text: b"\x00\x01" * 10,
        ),
        orch,
    )


@pytest.mark.asyncio
async def test_persona_template_runs_contract_path():
    scenario = parse_scenario(EXAMPLES / "multi-judge-smoke.yaml")
    driver, orch = _driver({"ask": "What is my current status?"})
    sink = FakeSink(orch)
    agent = FakeAgent(replies=["Hello! Your status is active.", "Sure thing."])
    result = await driver.run(
        scenario.caller_actions, sink, agent,
        first_speaker=scenario.run_spec.first_speaker,
    )
    assert result.failure is None
    assert result.ended_by == EndedBy.SCENARIO
    assert result.behaviors_completed == 1


@pytest.mark.asyncio
async def test_hold_timeout_template_waits_then_ends():
    scenario = parse_scenario(EXAMPLES / "hold-timeout-agent-stall.yaml")
    driver, orch = _driver()
    sink = FakeSink(orch)
    agent = FakeAgent(replies=["Hello, one moment please."])
    result = await driver.run(
        scenario.caller_actions, sink, agent,
        first_speaker=scenario.run_spec.first_speaker,
    )
    assert result.failure is None
    assert result.ended_by == EndedBy.SCENARIO
    assert len(sink.published) == 2  # two say: turns, waits publish nothing


@pytest.mark.asyncio
async def test_silent_template_stays_mute_but_observes_greeting():
    from livekit_agent_simulator.behavior_compile import silent_mode_enabled

    scenario = parse_scenario(EXAMPLES / "silent-caller-dead-air.yaml")
    assert silent_mode_enabled(scenario.persona) is True
    driver, orch = _driver()
    sink = FakeSink(orch)
    agent = FakeAgent(replies=["Hello? Anyone there?"])
    result = await driver.run(
        scenario.caller_actions, sink, agent,
        first_speaker=scenario.run_spec.first_speaker,
        silent_mode=silent_mode_enabled(scenario.persona),
    )
    assert result.failure is None
    assert result.ended_by == EndedBy.SCENARIO
    assert len(sink.published) == 0
    assert agent._replies == []  # greeting was consumed


@pytest.mark.asyncio
async def test_ambient_template_plays_bed_then_talks():
    scenario = parse_scenario(EXAMPLES / "ambient-loop-office.yaml")
    assert scenario.caller_actions[0].kind == "play_audio"
    assets = FakeAssets()
    driver, orch = _driver()
    sink = FakeSink(orch)
    agent = FakeAgent(replies=["Hello, welcome!"])
    # Shrink the 1500ms bed trigger for test speed; kind/order untouched.
    scenario.caller_actions[0].trigger.delay_ms = 5
    result = await driver.run(
        scenario.caller_actions, sink, agent,
        first_speaker=scenario.run_spec.first_speaker,
        assets=assets,
    )
    assert result.failure is None
    assert result.ended_by == EndedBy.SCENARIO
    assert assets.played == [("builtin:noise.ambient", 0.3, True)]
    assert len(sink.published) == 2


def test_play_audio_asset_resolves_in_catalog():
    """The ambient asset ref must exist — otherwise the template's bed
    would refuse at runtime (TRANSPORT_ERROR by design, never silent)."""
    from livekit_agent_simulator.audio.pcm_cue import resolve_cue_asset

    path = resolve_cue_asset("builtin:noise.ambient")
    assert Path(path).is_file()


def test_interrupt_rate_template_uses_explicit_interrupt():
    """interrupt-rate-medium no longer depends on the legacy rate runner:
    its caller_steps carry an explicit interrupt: action."""
    scenario = parse_scenario(EXAMPLES / "interrupt-rate-medium.yaml")
    kinds = [a.kind for a in scenario.caller_actions]
    assert "interrupt" in kinds
