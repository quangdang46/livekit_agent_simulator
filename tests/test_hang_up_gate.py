"""Script hang_up should not auto-end while dialogue is still open."""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from livekit_agent_simulator.script.hang_up_gate import agent_left_open_turn
from livekit_agent_simulator.script.models import ScriptStep
from livekit_agent_simulator.script.runtime import ScriptRunner


@pytest.mark.parametrize(
    "text,expected",
    [
        ("What's your full name?", True),
        ("Sure, the fee is $49 a month. May I have your name?", True),
        ("Could you please provide your phone number", True),
        ("Alright, thanks for calling. Goodbye!", False),
        ("The fee is forty nine dollars per month.", False),
        ("", False),
        (None, False),
    ],
)
def test_agent_left_open_turn(text, expected):
    assert agent_left_open_turn(text) is expected


def _observer(**kwargs):
    defaults = dict(
        user_has_spoken=True,
        agent_replied_this_turn=True,
        last_agent_final_text="The monthly fee is $49.",
        last_agent_final_mono=time.monotonic() - 5,
        last_user_final_mono=time.monotonic() - 20,
        agent_has_spoken=True,
        agent_is_active_speaker=False,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def test_hang_up_ready_blocks_awaiting_agent_reply():
    runner = ScriptRunner(
        steps=[ScriptStep("bye", "silence", 500, say="Bye", action="hang_up")],
        observer=_observer(agent_replied_this_turn=False),
        bridge=MagicMock(),
        writer=MagicMock(),
    )
    step = runner.steps[0]
    assert runner._hang_up_ready(step) is False
    runner.writer.emit.assert_called()
    kind = runner.writer.emit.call_args.args[0]
    assert kind == "sim.script.hang_up_deferred"


def test_hang_up_ready_blocks_open_question():
    runner = ScriptRunner(
        steps=[ScriptStep("bye", "silence", 500, say="Bye", action="hang_up")],
        observer=_observer(
            last_agent_final_text="What's your name?",
            last_agent_final_mono=time.monotonic() - 1,
            last_user_final_mono=time.monotonic() - 30,
        ),
        bridge=MagicMock(),
        writer=MagicMock(),
    )
    assert runner._hang_up_ready(runner.steps[0]) is False


def test_hang_up_ready_after_defer_budget():
    runner = ScriptRunner(
        steps=[
            ScriptStep(
                "bye",
                "silence",
                500,
                say="Bye",
                action="hang_up",
                open_question_idle_ms=1000,
            )
        ],
        observer=_observer(
            last_agent_final_text="What's your name?",
            last_agent_final_mono=time.monotonic(),
            last_user_final_mono=time.monotonic() - 30,
        ),
        bridge=MagicMock(),
        writer=MagicMock(),
    )
    # Simulate first defer started more than budget ago (wall-clock, not last agent final).
    runner._hang_up_defer_since["bye"] = time.monotonic() - 2.0
    assert runner._hang_up_ready(runner.steps[0]) is True


def test_hang_up_budget_not_reset_by_new_agent_question():
    runner = ScriptRunner(
        steps=[
            ScriptStep(
                "bye",
                "silence",
                500,
                say="Bye",
                action="hang_up",
                open_question_idle_ms=2000,
            )
        ],
        observer=_observer(
            last_agent_final_text="And your email?",
            last_agent_final_mono=time.monotonic(),
            last_user_final_mono=time.monotonic() - 60,
        ),
        bridge=MagicMock(),
        writer=MagicMock(),
    )
    runner._hang_up_defer_since["bye"] = time.monotonic() - 2.5
    # Fresh agent question just arrived — still allow hang once budget elapsed.
    assert runner._hang_up_ready(runner.steps[0]) is True


def test_hang_up_ready_allows_closed_agent_turn():
    runner = ScriptRunner(
        steps=[ScriptStep("bye", "silence", 500, say="Bye", action="hang_up")],
        observer=_observer(),
        bridge=MagicMock(),
        writer=MagicMock(),
    )
    assert runner._hang_up_ready(runner.steps[0]) is True


def test_hang_up_opt_out_open_question_defer():
    runner = ScriptRunner(
        steps=[
            ScriptStep(
                "bye",
                "silence",
                500,
                say="",
                action="hang_up",
                defer_on_open_question=False,
                require_agent_reply_this_turn=False,
            )
        ],
        observer=_observer(
            agent_replied_this_turn=False,
            last_agent_final_text="What's your name?",
            last_agent_final_mono=time.monotonic(),
        ),
        bridge=MagicMock(),
        writer=MagicMock(),
    )
    assert runner._hang_up_ready(runner.steps[0]) is True


def test_parse_hang_up_gate_fields():
    from livekit_agent_simulator.script_parse import parse_script_steps

    steps = parse_script_steps(
        {
            "steps": [
                {
                    "id": "bye",
                    "trigger": "silence",
                    "delay_ms": 1000,
                    "action": "hang_up",
                    "say": "Bye",
                    "defer_on_open_question": False,
                    "open_question_idle_ms": 5000,
                    "require_agent_reply_this_turn": False,
                }
            ]
        },
        "t.jsonl:1",
    )
    assert steps[0].defer_on_open_question is False
    assert steps[0].open_question_idle_ms == 5000
    assert steps[0].require_agent_reply_this_turn is False


@pytest.mark.asyncio
async def test_wait_agent_idle_returns_when_quiet():
    runner = ScriptRunner(
        steps=[ScriptStep("bye", "silence", 500, say="Bye", action="hang_up")],
        observer=_observer(agent_is_active_speaker=False),
        bridge=MagicMock(),
        writer=MagicMock(),
    )
    await runner._wait_agent_idle(timeout_s=0.2)


@pytest.mark.asyncio
async def test_wait_agent_idle_times_out_while_speaking():
    runner = ScriptRunner(
        steps=[ScriptStep("bye", "silence", 500, say="Bye", action="hang_up")],
        observer=_observer(agent_is_active_speaker=True),
        bridge=MagicMock(),
        writer=MagicMock(),
    )
    t0 = time.monotonic()
    await runner._wait_agent_idle(timeout_s=0.15)
    assert time.monotonic() - t0 >= 0.12
