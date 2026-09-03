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


def test_outcome_transcript_contains_negate_pass_when_absent():
    events = [
        {"kind": "transcript.agent.final", "spec": {"text": "Thank you, that's everything."}},
    ]
    result = evaluate_asserts(
        events,
        AssertSpec(
            outcomes=[
                OutcomeExpect(
                    id="no_retry",
                    type="transcript_contains",
                    phrases=("again, please",),
                    negate=True,
                )
            ]
        ),
    )
    assert result["pass"] is True


def test_outcome_transcript_contains_negate_fail_when_present():
    events = [
        {"kind": "transcript.agent.final", "spec": {"text": "Could you say that again, please?"}},
    ]
    result = evaluate_asserts(
        events,
        AssertSpec(
            outcomes=[
                OutcomeExpect(
                    id="no_retry",
                    type="transcript_contains",
                    phrases=("again, please",),
                    negate=True,
                )
            ]
        ),
    )
    assert result["pass"] is False


def test_parse_outcome_negate_field():
    spec = parse_assert_spec(
        {
            "outcomes": [
                {
                    "id": "no_retry",
                    "type": "transcript_contains",
                    "phrases": ["again, please"],
                    "negate": True,
                }
            ]
        }
    )
    assert spec.outcomes[0].negate is True


def test_parse_latency_outcome():
    spec = parse_assert_spec(
        {
            "outcomes": [
                {
                    "id": "speed",
                    "type": "latency",
                    "max_turn_p95_ms": 3500,
                    "max_ttfw_ms": 5000,
                    "require_turn_samples": 1,
                }
            ]
        }
    )
    assert spec.outcomes[0].type == "latency"
    assert spec.outcomes[0].max_turn_p95_ms == 3500
    assert spec.outcomes[0].max_ttfw_ms == 5000


def test_parse_latency_requires_threshold():
    try:
        parse_assert_spec({"outcomes": [{"id": "empty", "type": "latency"}]})
        raise AssertionError("expected ValueError")
    except ValueError as e:
        assert "threshold" in str(e).lower() or "latency" in str(e).lower()


def test_outcome_latency_pass_and_fail():
    events = [
        {
            "kind": "transcript.agent.final",
            "ts_mono_ms": 500,
            "spec": {"text": "hi", "turn_taking_ms": 800},
        },
        {
            "kind": "transcript.agent.final",
            "ts_mono_ms": 2000,
            "spec": {"text": "ok", "turn_taking_ms": 1200},
        },
    ]
    ok = evaluate_asserts(
        events,
        AssertSpec(
            outcomes=[
                OutcomeExpect(
                    id="speed",
                    type="latency",
                    max_turn_p95_ms=2000,
                    max_ttfw_ms=1000,
                    require_turn_samples=1,
                )
            ]
        ),
    )
    assert ok["pass"] is True, ok
    lat_check = [c for c in ok["checks"] if c.get("type") == "latency"][0]
    assert lat_check["actual"]["turn_p95_ms"] is not None

    bad = evaluate_asserts(
        events,
        AssertSpec(
            outcomes=[
                OutcomeExpect(
                    id="speed",
                    type="latency",
                    max_turn_p95_ms=500,  # too tight
                )
            ]
        ),
    )
    assert bad["pass"] is False
    reasons = [c for c in bad["checks"] if c.get("type") == "latency"][0]["reasons"]
    assert any("turn_p95" in r for r in reasons)


def test_outcome_latency_barge_recovery_rate():
    events = [
        {"kind": "sim.script.cue", "ts_mono_ms": 1000, "spec": {"barge_in": True}},
        {"kind": "transcript.agent.final", "ts_mono_ms": 2000, "spec": {"text": "ok"}},
    ]
    ok = evaluate_asserts(
        events,
        AssertSpec(
            outcomes=[
                OutcomeExpect(
                    id="rec_rate",
                    type="latency",
                    min_barge_recovery_rate=0.9,
                )
            ]
        ),
    )
    assert ok["pass"] is True

    no_barge = evaluate_asserts(
        [{"kind": "transcript.agent.final", "ts_mono_ms": 100, "spec": {"text": "x"}}],
        AssertSpec(
            outcomes=[
                OutcomeExpect(
                    id="rec_rate",
                    type="latency",
                    min_barge_recovery_rate=0.9,
                )
            ]
        ),
    )
    assert no_barge["pass"] is False


def test_parse_ended_by_outcome():
    spec = parse_assert_spec(
        {
            "outcomes": [
                {"id": "caller_hung", "type": "ended_by", "who": "sim"},
                {"id": "detect_only", "type": "ended_by", "ended_by": "detect"},
            ]
        }
    )
    assert spec.outcomes[0].type == "ended_by"
    assert spec.outcomes[0].ended_by == "sim"
    assert spec.outcomes[1].ended_by == "detect"


def test_parse_ended_by_invalid():
    try:
        parse_assert_spec({"outcomes": [{"id": "bad", "type": "ended_by", "ended_by": "robot"}]})
        assert False, "expected ValueError"
    except ValueError as e:
        assert "ended_by" in str(e).lower()


def test_outcome_ended_by_sim_hang():
    events = [
        {"kind": "sim.hang_up", "spec": {"by": "sim", "source": "script"}},
    ]
    ok = evaluate_asserts(
        events,
        AssertSpec(
            outcomes=[OutcomeExpect(id="h", type="ended_by", ended_by="sim")]
        ),
    )
    assert ok["pass"] is True
    # detect mode also passes
    ok2 = evaluate_asserts(
        events,
        AssertSpec(outcomes=[OutcomeExpect(id="h", type="ended_by", ended_by="detect")]),
    )
    assert ok2["pass"] is True


def test_outcome_ended_by_agent():
    events = [
        {"kind": "run.end_condition", "spec": {"reason": "agent_disconnected"}},
    ]
    ok = evaluate_asserts(
        events,
        AssertSpec(
            outcomes=[OutcomeExpect(id="ag", type="ended_by", ended_by="agent")]
        ),
    )
    assert ok["pass"] is True
    fail = evaluate_asserts(
        events,
        AssertSpec(
            outcomes=[OutcomeExpect(id="ag", type="ended_by", ended_by="sim")]
        ),
    )
    assert fail["pass"] is False



# ---------------------------------------------------------------- audio asserts


def _audio_events(agent_onsets: list[int], user_sources: list[int] | None = None):
    evs = []
    for m in (user_sources or []):
        evs.append({"kind": "sim.caller.audio_source_start", "ts_mono_ms": m, "spec": {}})
    for m in agent_onsets:
        evs.append({"kind": "sim.agent.audio_onset", "ts_mono_ms": m, "spec": {}})
    return evs


def test_agent_must_respond_passes_with_audio_onset() -> None:
    spec = parse_assert_spec({"outcomes": [{"id": "a", "type": "agent_must_respond"}]})
    res = evaluate_asserts(_audio_events([1000]), spec)
    assert res["pass"] is True


def test_agent_must_respond_fails_without_audio() -> None:
    spec = parse_assert_spec({"outcomes": [{"id": "a", "type": "agent_must_respond"}]})
    # Agent has a transcript final but NO audio onset → must FAIL (no transcript fallback).
    events = [{"kind": "transcript.agent.final", "ts_mono_ms": 1000, "spec": {"text": "hi"}}]
    res = evaluate_asserts(events, spec)
    assert res["pass"] is False
    c = res["checks"][0]
    assert c["type"] == "agent_must_respond"
    assert c["agent_audio_onsets"] == 0


def test_ttfa_skips_when_no_sample() -> None:
    spec = parse_assert_spec(
        {"outcomes": [{"id": "a", "type": "ttfa", "max_ttfa_p95_ms": 1000}]}
    )
    res = evaluate_asserts([], spec)
    # Missing sample → SKIP, not fail.
    assert res["pass"] is True
    c = res["checks"][0]
    assert c["skipped"] is True


def test_ttfa_fails_when_slow_with_sample() -> None:
    events = _audio_events(agent_onsets=[2000], user_sources=[500])
    spec = parse_assert_spec(
        {"outcomes": [{"id": "a", "type": "ttfa", "max_ttfa_p95_ms": 1000}]}
    )
    res = evaluate_asserts(events, spec)
    # ttfa_run_ms = 2000 (first agent onset) > 1000 → fail.
    assert res["pass"] is False
    c = res["checks"][0]
    assert c["skipped"] is False


def test_ttfa_require_audio_samples_fails_when_short() -> None:
    spec = parse_assert_spec(
        {"outcomes": [{"id": "a", "type": "ttfa", "require_audio_samples": 2}]}
    )
    res = evaluate_asserts(_audio_events(agent_onsets=[1000]), spec)
    assert res["pass"] is False  # 1 < 2, explicitly configured → fail


def test_turn_taking_audio_skips_when_no_sample() -> None:
    spec = parse_assert_spec(
        {"outcomes": [{"id": "a", "type": "turn_taking_audio", "max_turn_audio_p95_ms": 1000}]}
    )
    res = evaluate_asserts([], spec)
    assert res["pass"] is True
    assert res["checks"][0]["skipped"] is True


def test_turn_taking_audio_fails_when_slow() -> None:
    # user source 1000 → agent onset 3000 = 2000ms latency, p95 gate 1000ms → fail.
    events = _audio_events(agent_onsets=[3000], user_sources=[1000])
    spec = parse_assert_spec(
        {"outcomes": [{"id": "a", "type": "turn_taking_audio", "max_turn_audio_p95_ms": 1000}]}
    )
    res = evaluate_asserts(events, spec)
    assert res["pass"] is False


def test_turn_taking_audio_passes_when_fast() -> None:
    events = _audio_events(agent_onsets=[1500], user_sources=[1000])
    spec = parse_assert_spec(
        {"outcomes": [{"id": "a", "type": "turn_taking_audio", "max_turn_audio_p95_ms": 1000}]}
    )
    res = evaluate_asserts(events, spec)
    assert res["pass"] is True
