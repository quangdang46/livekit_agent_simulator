from livekit_agent_simulator.asserts import (
    AssertSpec,
    OutcomeExpect,
    ToolExpect,
    TranscriptExpect,
    evaluate_asserts,
    parse_assert_spec,
)


def test_parse_assert_spec():
    spec = parse_assert_spec(
        {
            "tools": [{"name": "end_call", "min_count": 1}],
            "transcript": [{"role": "agent", "contains_any": ["hello"]}],
            "outcomes": [
                {"id": "resolved", "type": "transcript_contains", "phrases": ["bye"]},
                {"id": "helpful", "type": "llm_bool", "prompt": "Was the agent helpful?"},
            ],
        }
    )
    assert len(spec.tools) == 1
    assert spec.tools[0].name == "end_call"
    assert len(spec.outcomes) == 2


def test_tool_assert_pass():
    events = [
        {"kind": "tool.start", "spec": {"name": "end_call", "payload": {}}},
    ]
    result = evaluate_asserts(events, AssertSpec(tools=[ToolExpect(name="end_call")]))
    assert result["pass"] is True


def test_tool_assert_fail_missing():
    result = evaluate_asserts([], AssertSpec(tools=[ToolExpect(name="end_call")]))
    assert result["pass"] is False


def test_tool_args_contains():
    events = [
        {
            "kind": "tool.start",
            "spec": {"name": "book", "payload": {"args": {"date": "2026-07-11", "party": 2}}},
        }
    ]
    ok = evaluate_asserts(
        events,
        AssertSpec(tools=[ToolExpect(name="book", args_contains={"date": "2026-07-11"})]),
    )
    bad = evaluate_asserts(
        events,
        AssertSpec(tools=[ToolExpect(name="book", args_contains={"date": "2099-01-01"})]),
    )
    assert ok["pass"] is True
    assert bad["pass"] is False


def test_transcript_contains_and_forbidden():
    events = [
        {"kind": "transcript.agent.final", "spec": {"text": "Hello, how can I help?"}},
    ]
    good = evaluate_asserts(
        events,
        AssertSpec(transcript=[TranscriptExpect(role="agent", contains_any=("help",))]),
    )
    bad = evaluate_asserts(
        events,
        AssertSpec(
            transcript=[TranscriptExpect(role="agent", must_not_match=r"help")]
        ),
    )
    assert good["pass"] is True
    assert bad["pass"] is False


def test_outcome_recovery():
    events = [
        {
            "kind": "sim.script.cue",
            "ts_mono_ms": 1000,
            "spec": {"barge_in": True, "step_id": "cut"},
        },
        {"kind": "interruption", "ts_mono_ms": 1000, "spec": {"by": "sim", "barge_in": True}},
        {"kind": "transcript.agent.final", "ts_mono_ms": 2500, "spec": {"text": "Sorry, go on."}},
    ]
    ok = evaluate_asserts(
        events,
        AssertSpec(
            outcomes=[
                OutcomeExpect(
                    id="rec",
                    type="recovery",
                    min_agent_finals_after_barge_in=1,
                    min_interruptions=1,
                    max_ms_after_barge_to_agent_final=2000,
                )
            ]
        ),
    )
    assert ok["pass"] is True
    slow = evaluate_asserts(
        events,
        AssertSpec(
            outcomes=[
                OutcomeExpect(
                    id="rec",
                    type="recovery",
                    min_agent_finals_after_barge_in=1,
                    max_ms_after_barge_to_agent_final=500,
                )
            ]
        ),
    )
    assert slow["pass"] is False


def test_outcome_transcript_contains():
    events = [
        {"kind": "transcript.agent.final", "spec": {"text": "Your booking is confirmed."}},
    ]
    result = evaluate_asserts(
        events,
        AssertSpec(
            outcomes=[
                OutcomeExpect(id="booked", type="transcript_contains", phrases=("confirmed",))
            ]
        ),
    )
    assert result["pass"] is True
