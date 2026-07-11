import pytest

from livekit_agent_simulator.script_parse import parse_script_steps, parse_script_verify
from livekit_agent_simulator.script_runner import ScriptStep, ScriptVerifySpec, evaluate_script_log


def test_parse_script_steps():
    steps = parse_script_steps(
        {
            "steps": [
                {
                    "id": "bc",
                    "trigger": "agent_speaking",
                    "delay_ms": 700,
                    "say": "うん",
                    "label": "backchannel",
                },
                {
                    "id": "quiet",
                    "trigger": "silence",
                    "delay_ms": 2000,
                    "action": "wait",
                },
                {
                    "id": "bi",
                    "barge_in": True,
                    "say": "wait—",
                },
            ]
        },
        "test.jsonl:1",
    )
    assert len(steps) == 3
    assert steps[0].say == "うん"
    assert steps[0].delay_ms == 700
    assert steps[1].trigger == "silence"
    assert steps[1].action == "wait"
    assert steps[2].trigger == "agent_speaking"
    assert steps[2].delay_ms == 250


def test_parse_script_verify():
    v = parse_script_verify({"min_agent_finals_after_first_cue": 1, "max_interruptions": 0})
    assert v is not None
    assert v.min_agent_finals_after_first_cue == 1
    assert v.max_interruptions == 0


def test_evaluate_script_log_pass_during_agent():
    steps = [ScriptStep("bc", "agent_speaking", 800, "うん", "backchannel")]
    events = [
        {"kind": "sim.script.cue", "ts_mono_ms": 5000, "spec": {"step_id": "bc", "during_agent_speech": True}},
        {"kind": "transcript.agent.final", "ts_mono_ms": 8000, "spec": {"text": "continued"}},
    ]
    result = evaluate_script_log(
        events,
        steps,
        ScriptVerifySpec(min_agent_finals_after_first_cue=1, max_interruptions=0),
    )
    assert result["pass"] is True
    assert result["agent_finals_after_first_cue"] == 1


def test_evaluate_script_log_fail_not_during_agent():
    steps = [ScriptStep("bc", "agent_speaking", 800, "うん", "backchannel")]
    events = [
        {"kind": "sim.script.cue", "ts_mono_ms": 5000, "spec": {"step_id": "bc", "during_agent_speech": False}},
    ]
    result = evaluate_script_log(events, steps, ScriptVerifySpec())
    assert result["pass"] is False


def test_evaluate_script_log_interrupt_scenario():
    steps = [ScriptStep("ri", "agent_speaking", 800, "wait", "interrupt")]
    events = [
        {"kind": "sim.script.cue", "ts_mono_ms": 4000, "spec": {"step_id": "ri", "during_agent_speech": True}},
        {"kind": "interruption", "ts_mono_ms": 4100, "spec": {}},
    ]
    result = evaluate_script_log(events, steps, ScriptVerifySpec(min_interruptions=1))
    assert result["pass"] is True


def test_evaluate_silence_wait_and_agent_resume():
    steps = [
        ScriptStep("q", "silence", 2000, say="", label="quiet", action="wait"),
    ]
    events = [
        {
            "kind": "sim.script.wait",
            "ts_mono_ms": 5000,
            "spec": {"step_id": "q", "trigger": "silence", "action": "wait"},
        },
        {"kind": "transcript.agent.final", "ts_mono_ms": 8000, "spec": {"text": "Are you still there?"}},
    ]
    result = evaluate_script_log(
        events,
        steps,
        ScriptVerifySpec(min_agent_finals_after_silence=1),
    )
    assert result["pass"] is True
    assert result["agent_finals_after_silence"] == 1
