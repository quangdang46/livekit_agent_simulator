"""#29 hold_music_timeout_s — parse/validation + contract hold watchdog."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from livekit_agent_simulator.scenario import ScenarioError, parse_scenario

BASE = """\
{"apiVersion":"agent-sim/v1","kind":"Scenario","metadata":{"id":"hold-test","locale":"en-US"}}
{"kind":"Persona","spec":{"name":"Caller","brief":"Caller waiting on a stalled agent.","goals":["hear greeting"]}}
{"kind":"Simulator","spec":{"max_turns":6,"timeout_s":90,"first_speaker":"agent"}}
"""


def _write(tmp_path, extra: str):
    f = tmp_path / "hold-test.jsonl"
    f.write_text(BASE + extra, encoding="utf-8")
    return f


# ── parse / validation ────────────────────────────────────────────────


def test_parse_execute_hold_timeout(tmp_path) -> None:
    f = _write(tmp_path, '{"kind":"Execute","spec":{"hold_music_timeout_s":20}}\n')
    s = parse_scenario(f)
    assert s.execute is not None
    assert s.execute.hold_music_timeout_s == 20.0
    assert s.hold_music_timeout_s() == 20.0
    assert s.export_dict()["hold_music_timeout_s"] == 20.0


def test_parse_hold_timeout_default_off(tmp_path) -> None:
    f = _write(tmp_path, '{"kind":"Execute","spec":{"max_turns":4}}\n')
    s = parse_scenario(f)
    assert s.hold_music_timeout_s() is None
    assert s.export_dict()["hold_music_timeout_s"] is None


def test_parse_hold_timeout_range(tmp_path) -> None:
    for bad in (1, 0, 4.9, 301, -10):
        f = _write(
            tmp_path, f'{{"kind":"Execute","spec":{{"hold_music_timeout_s":{bad}}}}}\n'
        )
        with pytest.raises(ScenarioError, match="hold_music_timeout_s"):
            parse_scenario(f)


def test_parse_hold_timeout_not_a_number(tmp_path) -> None:
    f = _write(
        tmp_path, '{"kind":"Execute","spec":{"hold_music_timeout_s":"soon"}}\n'
    )
    with pytest.raises(ScenarioError, match="hold_music_timeout_s"):
        parse_scenario(f)


def test_parse_persona_alias(tmp_path) -> None:
    f = tmp_path / "alias.jsonl"
    f.write_text(
        '{"apiVersion":"agent-sim/v1","kind":"Scenario","metadata":{"id":"alias","locale":"en-US"}}\n'
        '{"kind":"Persona","spec":{"name":"Caller","brief":"b","speech_conditions":{"hold_music_timeout_s":30}}}\n',
        encoding="utf-8",
    )
    s = parse_scenario(f)
    assert s.hold_music_timeout_s() == 30.0


def test_execute_wins_over_persona_alias(tmp_path) -> None:
    f = tmp_path / "both.jsonl"
    f.write_text(
        '{"apiVersion":"agent-sim/v1","kind":"Scenario","metadata":{"id":"both","locale":"en-US"}}\n'
        '{"kind":"Persona","spec":{"name":"Caller","brief":"b","speech_conditions":{"hold_music_timeout_s":30}}}\n'
        '{"kind":"Execute","spec":{"hold_music_timeout_s":10}}\n',
        encoding="utf-8",
    )
    s = parse_scenario(f)
    assert s.hold_music_timeout_s() == 10.0


def test_persona_alias_invalid_fails_parse(tmp_path) -> None:
    f = tmp_path / "alias-bad.jsonl"
    f.write_text(
        '{"apiVersion":"agent-sim/v1","kind":"Scenario","metadata":{"id":"alias-bad","locale":"en-US"}}\n'
        '{"kind":"Persona","spec":{"name":"Caller","brief":"b","speech_conditions":{"hold_music_timeout_s":2}}}\n',
        encoding="utf-8",
    )
    with pytest.raises(ScenarioError, match="hold_music_timeout_s"):
        parse_scenario(f)


# ── contract hold watchdog (legacy _conversation_loop removed) ──────────
# The hold timeout now lives in ContractCallerDriver._hold_watchdog, armed by
# AgentTurnWait.last_speech_at_ms evidence. These tests drive the driver
# directly (no LiveKit) with the same scenarios the old loop tests covered.


class _Obs:
    """Minimal AgentTurnWait: scripted speaking evidence via last_speech_at_ms."""

    def __init__(self, last_speech_ms=None, speaking: bool = False) -> None:
        self._last_speech_ms = last_speech_ms
        self._speaking = speaking

    async def wait_agent_turn(self, *, timeout_s: float = 30.0):
        return None

    def is_agent_speaking_now(self) -> bool:
        return self._speaking

    def last_speech_at_ms(self):
        return self._last_speech_ms


class _Sink:
    def __init__(self, orch) -> None:
        self.orch = orch

    async def publish(self, pcm, identity, *, label, gain=1.0):
        return True


def _driver():
    from livekit_agent_simulator.caller_contract.driver import ContractCallerDriver
    from livekit_agent_simulator.caller_contract.dsl import parse_steps
    from livekit_agent_simulator.caller_contract.language_adapter import AILanguageAdapter
    from livekit_agent_simulator.caller_contract.orchestrator import Orchestrator
    from livekit_agent_simulator.caller_contract.semantic import RuleBasedSemanticVerifier
    from livekit_agent_simulator.caller_contract.validator import ContractValidator

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
        synthesize=lambda text: b"\x00\x01" * 10,
    )
    actions = parse_steps([{"wait": 3000}], file="t")
    return driver, orch, actions


def _ago_ms(ms: float) -> float:
    return time.monotonic() * 1000.0 - ms


@pytest.mark.asyncio
async def test_hold_timeout_hangs_up_after_agent_dead_air() -> None:
    from livekit_agent_simulator.caller_contract import EndedBy

    driver, orch, actions = _driver()
    fired: list[bool] = []
    events: list[tuple[str, dict]] = []
    result = await driver.run(
        actions,
        _Sink(orch),
        _Obs(last_speech_ms=_ago_ms(1000)),
        emit=lambda kind, spec=None: events.append((kind, spec or {})),
        hold_timeout_s=0.5,
        on_hold_timeout=lambda: fired.append(True),
    )
    assert result.failure is None
    assert result.ended_by == EndedBy.SCENARIO  # watchdog fires; the run's hang-up is the bridge callback
    assert fired == [True]
    hold_events = [s for k, s in events if k == "sim.hold_timeout"]
    assert hold_events and hold_events[0]["timeout_s"] == 0.5
    assert hold_events[0]["agent_idle_ms"] >= 500


@pytest.mark.asyncio
async def test_hold_timeout_not_armed_before_agent_speaks() -> None:
    driver, orch, actions = _driver()
    fired: list[bool] = []
    result = await driver.run(
        actions,
        _Sink(orch),
        _Obs(last_speech_ms=None),  # never spoke: watchdog never arms
        hold_timeout_s=0.1,
        on_hold_timeout=lambda: fired.append(True),
    )
    assert result.failure is None
    assert fired == []


@pytest.mark.asyncio
async def test_hold_timeout_resets_on_agent_activity() -> None:
    # Fresh speech evidence just before the budget means no fire within a
    # short wait: the watchdog measures from the last evidence, not run start.
    driver, orch, actions = _driver()
    fired: list[bool] = []
    result = await driver.run(
        actions,
        _Sink(orch),
        _Obs(last_speech_ms=_ago_ms(50)),
        hold_timeout_s=30.0,
        on_hold_timeout=lambda: fired.append(True),
    )
    assert result.failure is None
    assert fired == []


@pytest.mark.asyncio
async def test_hold_timeout_beats_silence_wait_when_armed() -> None:
    # Armed watchdog fires even while a long wait: is in progress — the
    # watchdog is independent of the action being executed.
    from livekit_agent_simulator.caller_contract import EndedBy

    driver, orch, actions = _driver()
    fired: list[bool] = []
    result = await driver.run(
        actions,
        _Sink(orch),
        _Obs(last_speech_ms=_ago_ms(2000)),
        hold_timeout_s=0.3,
        on_hold_timeout=lambda: fired.append(True),
    )
    assert result.failure is None
    assert result.ended_by == EndedBy.SCENARIO
    assert fired == [True]


@pytest.mark.asyncio
async def test_hold_timeout_ignores_caller_silence() -> None:
    # Caller quiet (wait action) does not disarm the watchdog: only agent
    # speech evidence matters, matching the legacy agent-only measure.
    driver, orch, actions = _driver()
    fired: list[bool] = []
    result = await driver.run(
        actions,
        _Sink(orch),
        _Obs(last_speech_ms=_ago_ms(1000)),
        hold_timeout_s=0.4,
        on_hold_timeout=lambda: fired.append(True),
    )
    assert result.failure is None
    assert fired == [True]


@pytest.mark.asyncio
async def test_no_hold_timeout_disables_watchdog() -> None:
    driver, orch, actions = _driver()
    fired: list[bool] = []
    result = await driver.run(
        actions,
        _Sink(orch),
        _Obs(last_speech_ms=_ago_ms(10_000)),
        hold_timeout_s=None,
        on_hold_timeout=lambda: fired.append(True),
    )
    assert result.failure is None
    assert fired == []
