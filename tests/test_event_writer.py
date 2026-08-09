import json

from livekit_agent_simulator.logging.event_writer import EventWriter


def make_writer(tmp_path):
    return EventWriter("r-test", tmp_path / "reports" / "r-test", timezone_name="Asia/Ho_Chi_Minh")


def test_envelope_fields(tmp_path):
    w = make_writer(tmp_path)
    e = w.emit("run.started", spec={"scenario_id": "s1"}, include_dialogue=False)
    assert e["seq"] == 1
    assert e["run_id"] == "r-test"
    assert e["event_id"].startswith("evt_")
    assert e["datetime_utc"].endswith("Z")
    assert "+07:00" in e["datetime_local"]
    assert e["ts_mono_ms"] >= 0
    assert "dialogue" not in e


def test_dialogue_snapshot_lifecycle(tmp_path):
    w = make_writer(tmp_path)
    w.update_dialogue("user", "予約を確認してください", final=True, at_ms=1000)
    w.begin_turn(1)
    e = w.emit("tool.start", spec={"name": "check_booking"}, source="app.flow")
    assert e["dialogue"]["user"]["text"] == "予約を確認してください"
    assert e["dialogue"]["agent"]["text"] is None
    assert "note" in e["dialogue"]["agent"]

    w.update_dialogue("agent", "確認しました", final=True, at_ms=2000)
    e2 = w.emit("transcript.agent.final", spec={"text": "確認しました"})
    assert e2["dialogue"]["agent"]["text"] == "確認しました"

    # New turn clears the stale agent reply but keeps the fresh user utterance flow.
    w.update_dialogue("user", "ありがとう", final=True, at_ms=3000)
    w.begin_turn(2)
    e3 = w.emit("transcript.user.final", spec={"text": "ありがとう"})
    assert e3["dialogue"]["agent"]["text"] is None
    assert e3["turn"] == 2


def test_jsonl_appended_and_flushed(tmp_path):
    w = make_writer(tmp_path)
    w.emit("a", include_dialogue=False)
    w.emit("b", include_dialogue=False)
    lines = (tmp_path / "reports" / "r-test" / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["kind"] == "a"
    assert json.loads(lines[1])["seq"] == 2


def test_finalize_writes_artifacts_and_metrics(tmp_path):
    w = make_writer(tmp_path)
    w.emit("run.started", include_dialogue=False)

    w.update_dialogue("user", "hello", final=True)
    w.begin_turn(1)
    w.emit("transcript.user.final", spec={"text": "hello"})
    start = w.emit("tool.start", spec={"name": "check_booking", "call_id": "tc1"}, source="app.flow")
    w.emit(
        "tool.error",
        spec={"name": "check_booking", "call_id": "tc1", "error": "SERVICE_UNAVAILABLE", "duration_ms": 2600},
        source="app.flow",
        parent_event_id=start["event_id"],
    )
    w.emit(
        "session.agent_state",
        spec={"old_state": "LISTENING", "new_state": "THINKING"},
        source="lk.agent.session",
        include_dialogue=False,
    )
    w.emit("transcript.agent.final", spec={"text": "sorry, error", "turn_taking_ms": 3100})

    summary = w.finalize("done", meta={"run_id": "r-test", "scenario_id": "s1"})

    assert summary["turn_count"] == 1
    assert summary["tool_errors"] == 1
    assert summary["tool_calls"] == 1
    assert summary["turn_taking_ms"]["p50"] == 3100
    assert "metrics" in summary
    assert summary["metrics"]["turn_taking_ms"]["p50"] == 3100
    assert summary["metrics"]["ttfw_ms"] is not None
    turn = summary["turns"][0]
    assert turn["user_text"] == "hello"
    assert turn["agent_text"] == "sorry, error"
    assert turn["tool_errors"] == 1

    report_dir = tmp_path / "reports" / "r-test"
    assert (report_dir / "summary.json").exists()
    assert (report_dir / "meta.json").exists()
    timeline = (report_dir / "timeline.md").read_text(encoding="utf-8")
    assert "tool.error" in timeline
    assert "LISTENING → THINKING" in timeline
    assert "⚠ slow" in timeline  # 3100ms > default 2500ms warn

    events = [json.loads(l) for l in (report_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert events[-1]["kind"] == "run.ended"


# ──────────────────────────── review.md rendering tests


def _make_writer_and_finalize(tmp_path, verdict):
    """Helper: create a writer, write one turn, finalize with the given verdict, return report_dir."""
    w = make_writer(tmp_path)
    w.emit("run.started", include_dialogue=False)
    w.update_dialogue("user", "hello", final=True)
    w.begin_turn(1)
    w.emit("transcript.user.final", spec={"text": "hello"})
    w.emit("transcript.agent.final", spec={"text": "hi there"})
    w.finalize("done", meta={"run_id": "r-test", "scenario_id": "s1"}, verdict=verdict)
    return tmp_path / "reports" / "r-test"


def test_review_md_rich_verdict(tmp_path):
    verdict = {
        "verdict": "fail",
        "score": 65,
        "confidence": "medium",
        "overall_summary": "The call had issues with natural flow.",
        "strengths": ["Polite greeting", "Good closing"],
        "issues": [
            {
                "title": "Two questions stacked in one turn",
                "severity": "Critical",
                "evidence": "名前と折り返し時間をお願いします",
                "impact": "Caller loses track of which to answer.",
                "recommendation": "Ask one question at a time.",
            },
        ],
        "missing_checks": ["No callback time agreed"],
        "language_naturalness": ["Turn 3 felt rushed"],
        "final_assessment": {
            "goal_achievement": "6/10",
            "understanding": "7/10",
            "conversation_flow": "5/10",
            "clarity": "6/10",
            "user_experience": "5/10",
            "conclusion": "Needs improvement.",
        },
        "criteria": [
            {"criterion": "greets", "met": True, "relevant": True, "evidence": "ok"},
        ],
    }
    report_dir = _make_writer_and_finalize(tmp_path, verdict)
    md = (report_dir / "review.md").read_text(encoding="utf-8")

    assert "# Overall" in md
    assert "natural flow" in md
    assert "# Strengths" in md
    assert "Polite greeting" in md
    assert "# Findings" in md
    assert "Two questions stacked" in md
    assert "Severity: Critical" in md
    assert "名前と折り返し時間" in md
    assert "Ask one question at a time" in md
    assert "# Missing or Unclear Information" in md
    assert "No callback time" in md
    assert "# Language and Conversation Quality" in md
    assert "# Final Assessment" in md
    assert "Goal Achievement" in md
    assert "6/10" in md
    assert "Needs improvement" in md


def test_review_md_legacy_feedback_fallback(tmp_path):
    verdict = {
        "verdict": "fail",
        "conversation_feedback": [
            {
                "issue": "Stacked questions",
                "severity": "high",
                "agent_line": "名前と時間をお願いします",
                "why": "Caller confused.",
            }
        ],
    }
    report_dir = _make_writer_and_finalize(tmp_path, verdict)
    md = (report_dir / "review.md").read_text(encoding="utf-8")

    assert "# Overall" in md
    assert "# Issues" in md
    assert "Stacked questions" in md
    assert "> Agent:" in md


def test_review_md_multi_judge(tmp_path):
    verdict = {
        "verdict": "pass",
        "score": 88,
        "mode": "all",
        "overall_summary": "Overall the call was successful.",
        "judges": [
            {
                "judge_id": "task",
                "overall_summary": "Task was completed.",
                "strengths": ["Result was correct"],
                "issues": [
                    {
                        "title": "Slow TTFW",
                        "severity": "Minor",
                        "evidence": "Agent took 6s to respond",
                        "impact": "Caller waited.",
                        "recommendation": "Optimize first response.",
                    }
                ],
                "final_assessment": {
                    "goal_achievement": "9/10",
                    "understanding": "8/10",
                    "conversation_flow": "7/10",
                    "clarity": "8/10",
                    "user_experience": "7/10",
                    "conclusion": "Solid.",
                },
            },
            {
                "judge_id": "tone",
                "overall_summary": "Tone was professional.",
                "strengths": ["No rudeness"],
            },
        ],
    }
    report_dir = _make_writer_and_finalize(tmp_path, verdict)
    md = (report_dir / "review.md").read_text(encoding="utf-8")

    assert "# Review" in md
    assert "Mode: all" in md
    assert "## Judge: task" in md
    assert "Task was completed" in md
    assert "## Judge: tone" in md
    assert "Tone was professional" in md
    assert "# Strengths" in md
    assert "Result was correct" in md
    assert "No rudeness" in md


def test_review_md_empty_when_no_content(tmp_path):
    report_dir = _make_writer_and_finalize(tmp_path, {"verdict": "pass", "score": 95})
    assert not (report_dir / "review.md").exists()
