"""Unit tests for the Contract Caller Driver (single path, no LiveKit)."""

from __future__ import annotations

import pytest

from livekit_agent_simulator.caller_contract import (
    EndedBy,
    FailureReason,
    GenerationIdentity,
)
from livekit_agent_simulator.caller_contract.driver import (
    AGENT_SILENCE_WAIT_S,
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

    async def publish(self, pcm, identity, *, label, gain=1.0):
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
async def test_synthesized_text_is_byte_identical_to_validated_text():
    """TTS input must be byte-identical to what the validator approved:
    say synthesizes the EXACT authored line, do synthesizes the EXACT
    validated candidate utterance (never a planner-reshaped variant).
    The planner still runs (timing metadata), but its word tokens must not
    leak into the audio path."""
    driver, orch = _driver()
    actions = parse_steps(
        [
            {'say': 'Hi there.'},
            {'do': {'behavior': 'negotiate', 'target': 'price',
                    'constraints': {'max_turns': 1, 'max_budget': 30000}}},
        ],
        file='t',
    )
    spoken_texts: list[str] = []
    def _capture(text: str) -> bytes:
        spoken_texts.append(text)
        return bytes([0, 1]) * 100
    driver._speak = _capture
    sink = FakeSink(orch)
    agent = FakeAgent(replies=['Sure, I can do $30,000.'])
    result = await driver.run(actions, sink, agent)
    assert result.failure is None
    assert spoken_texts[0] == 'Hi there.'
    assert spoken_texts[1] == 'Would you be able to come down to $30,000?'


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
async def test_behavior_budget_owned_by_orchestrator_gate_not_range():
    """HIGH review finding: the turn budget must have exactly ONE owner --
    Orchestrator.check_max_turns() gating the driver loop. A behavior with
    max_turns=2 must publish exactly 2 caller turns against a never-satisfied
    evaluator (no early exit, no extra turn), then fail BEHAVIOR_TIMEOUT
    through the single canonical exit."""
    driver, orch = _driver()
    actions = parse_steps(
        [{"do": {"behavior": "ask", "constraints": {"max_turns": 2}}}],
        file="t",
    )
    sink = FakeSink(orch)
    agent = FakeAgent(replies=["Hmm.", "Hmm."])  # never satisfies
    events: list[tuple[str, dict]] = []
    result = await driver.run(
        actions, sink, agent, emit=lambda kind, spec=None: events.append((kind, spec or {}))
    )
    assert result.failure is not None
    assert result.failure.reason == FailureReason.BEHAVIOR_TIMEOUT
    assert result.turns_spoken == 2, "budget owner must allow exactly max_turns turns"
    assert len(sink.published) == 2
    violations = [spec for kind, spec in events if kind == "contract.behavior_violation"]
    assert len(violations) == 1 and violations[0]["reason"] == "FAILED_MAX_TURNS"


@pytest.mark.asyncio
async def test_stale_identity_dropped_never_published():
    driver, orch = _driver()
    actions = parse_steps([{"say": "Hello."}], file="t")
    sink = FakeSink(orch)

    stale = GenerationIdentity(behavior_id="stale", turn_id=0, generation_id=0)
    assert await sink.publish(b"\x00" * 10, stale, label="stale") is False
    assert sink.dropped == 1

    agent = FakeAgent()
    result = await driver.run(actions, sink, agent)
    assert result.turns_spoken == 1
    assert len(sink.published) == 1


class _SpeakingAgent(FakeAgent):
    """Agent that reports itself as speaking for the first N silence polls,
    then goes quiet — lets the test observe the do: silence gate.

    The polls and the replies are INDEPENDENT channels, mirroring the real
    Observer: is_agent_speaking_now reads the active-speaker flag while
    wait_agent_turn returns transcript finals. A fake that couples them
    (speaking forever + one reply) models an agent that talks over every
    caller turn forever — no such agent exists, and the seeded policy would
    cut into it rather than wait it out.
    """

    def __init__(self, replies=None, *, speaking_polls: int = 0):
        super().__init__(replies)
        self._speaking_polls = speaking_polls
        self.polls = 0

    def is_agent_speaking_now(self) -> bool:
        self.polls += 1
        return self.polls <= self._speaking_polls


@pytest.mark.asyncio
async def test_do_waits_for_agent_silence_before_publishing():
    """Run-026 regression: the do: branch published OVER agent speech because
    only say: called _wait_agent_silence. A speaking agent must delay the
    do: publish; a silent agent must not delay it."""
    import time as _time

    actions = parse_steps(
        [{"do": {"behavior": "ask", "target": "price", "constraints": {"max_turns": 2}}}],
        file="t",
    )

    # ScriptedBackend's fallback ask line ("Could you tell me more?") has no
    # price word, so script the single generation explicitly (TARGET_UNVERIFIED
    # would otherwise fail the turn for reasons unrelated to this test).
    # Silent agent: no wait.
    # NOTE on _ScriptedBackend: it pops scripted utterances, then falls back
    # to "Could you tell me more?" (no price word → TARGET_UNVERIFIED under
    # ask/price). The reply below DOES satisfy the evaluator ("The price is
    # $30,000" hits the price-statement branch), so the behavior ends after
    # ONE turn — exactly one generation, no fallback, no budget loop.
    satisfy_once = {"act": "ask", "target": "price", "slots": {},
                    "utterance": "Could you tell me the price?"}
    driver, orch = _driver(backend=_ScriptedBackend(utterances=[dict(satisfy_once)]))
    sink = FakeSink(orch)
    agent = _SpeakingAgent(replies=["The price is $30,000."], speaking_polls=0)
    t0 = _time.monotonic()
    result = await driver.run(actions, sink, agent)
    fast_dt = _time.monotonic() - t0
    assert result.failure is None
    assert len(sink.published) == 1

    # Speaking agent (3 polls x 50ms, then quiet): the publish must wait
    # for silence. polls and replies are independent (see _SpeakingAgent):
    # the flag drops after 3 polls while the scripted reply is still there
    # to satisfy the behavior on the first agent answer.
    driver2, orch2 = _driver(backend=_ScriptedBackend(utterances=[dict(satisfy_once)]))
    sink2 = FakeSink(orch2)
    agent2 = _SpeakingAgent(replies=["The price is $30,000."], speaking_polls=3)
    t0 = _time.monotonic()
    result2 = await driver2.run(actions, sink2, agent2)
    slow_dt = _time.monotonic() - t0
    assert result2.failure is None
    assert len(sink2.published) == 1
    assert agent2.polls > 0, "the silence gate never polled the agent"
    assert slow_dt > fast_dt, "speaking agent did not delay the do: publish"


@pytest.mark.asyncio
async def test_do_silence_gate_is_bounded_by_talkative_agent():
    """The gate must not wedge the caller: an agent that never goes silent
    still gets a publish after AGENT_SILENCE_WAIT_S (patched short here).

    NOTE: the constant is read at CALL time (``deadline = ...
    + AGENT_SILENCE_WAIT_S`` inside ``_wait_agent_silence``), so it must be
    patched with monkeypatch.setattr on the module — a stale
    ``from ... import AGENT_SILENCE_WAIT_S`` copy would keep the old value
    and this test would wait the full 6s instead of 0.15s.
    """
    import livekit_agent_simulator.caller_contract.driver as _driver_mod

    actions = parse_steps(
        [{"do": {"behavior": "ask", "target": "price", "constraints": {"max_turns": 1}}}],
        file="t",
    )
    driver, orch = _driver(
        backend=_ScriptedBackend(
            utterances=[
                {
                    "act": "ask",
                    "target": "price",
                    "slots": {},
                    "utterance": "Could you tell me the price?",
                }
            ]
        )
    )
    sink = FakeSink(orch)
    agent = _SpeakingAgent(replies=["The price is $30,000."], speaking_polls=10_000)

    assert _driver_mod.AGENT_SILENCE_WAIT_S == AGENT_SILENCE_WAIT_S == 6.0, (
        "a previous test left the module constant patched — "
        f"got {_driver_mod.AGENT_SILENCE_WAIT_S}"
    )
    _driver_mod.AGENT_SILENCE_WAIT_S = 0.15
    try:
        result = await driver.run(actions, sink, agent)
    finally:
        _driver_mod.AGENT_SILENCE_WAIT_S = AGENT_SILENCE_WAIT_S

    assert result.failure is None
    assert len(sink.published) == 1, "bounded gate must publish anyway on expiry"
