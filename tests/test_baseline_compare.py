"""P1.D — golden baseline compare hard gate (portable thresholds)."""

from __future__ import annotations

from livekit_agent_simulator.ops import evaluate_baseline_gate


def test_gate_pass_similar():
    base = {
        "status": "done",
        "assert_pass": True,
        "ttfw_ms": 1000,
        "turn_taking_p95": 2000,
        "duration_ms": 10000,
        "tool_errors": 0,
        "barge_recovery_rate": 1.0,
    }
    cand = {
        "status": "done",
        "assert_pass": True,
        "ttfw_ms": 1200,
        "turn_taking_p95": 2100,
        "duration_ms": 11000,
        "tool_errors": 0,
        "barge_recovery_rate": 1.0,
    }
    g = evaluate_baseline_gate(base, cand)
    assert g["ok"] is True


def test_gate_fail_ttfw_regression():
    base = {"status": "done", "ttfw_ms": 1000, "turn_taking_p95": 2000, "duration_ms": 10000}
    cand = {"status": "done", "ttfw_ms": 5000, "turn_taking_p95": 2000, "duration_ms": 10000}
    g = evaluate_baseline_gate(base, cand, max_ttfw_regression_ms=1500)
    assert g["ok"] is False
    assert any("ttfw" in r for r in g["reasons"])


def test_gate_fail_status():
    base = {"status": "done"}
    cand = {"status": "failed"}
    g = evaluate_baseline_gate(base, cand)
    assert g["ok"] is False


def test_gate_fail_tool_errors_up():
    base = {"status": "done", "tool_errors": 0, "ttfw_ms": 1, "turn_taking_p95": 1, "duration_ms": 1}
    cand = {"status": "done", "tool_errors": 3, "ttfw_ms": 1, "turn_taking_p95": 1, "duration_ms": 1}
    g = evaluate_baseline_gate(base, cand)
    assert g["ok"] is False


def test_gate_fail_assert_pass():
    base = {"status": "done", "assert_pass": True}
    cand = {"status": "done", "assert_pass": False}
    g = evaluate_baseline_gate(base, cand)
    assert g["ok"] is False
    assert any("assert" in r for r in g["reasons"])


def test_gate_fail_barge_recovery_drop():
    base = {
        "status": "done",
        "ttfw_ms": 1,
        "turn_taking_p95": 1,
        "duration_ms": 1,
        "barge_recovery_rate": 1.0,
    }
    cand = {
        "status": "done",
        "ttfw_ms": 1,
        "turn_taking_p95": 1,
        "duration_ms": 1,
        "barge_recovery_rate": 0.4,
    }
    g = evaluate_baseline_gate(base, cand, max_barge_recovery_drop=0.0)
    assert g["ok"] is False
    assert any("barge_recovery" in r for r in g["reasons"])


def test_gate_skips_missing_latency():
    base = {"status": "done", "ttfw_ms": None}
    cand = {"status": "done", "ttfw_ms": None}
    g = evaluate_baseline_gate(base, cand)
    assert g["ok"] is True
    skipped = [c for c in g["checks"] if c.get("skipped")]
    assert skipped


def test_gate_audio_metrics_skip_when_baseline_missing():
    """baseline=None + candidate=value → skip, NOT regression (spec decision 11)."""
    base = {"status": "done", "ttfa_ms": None, "turn_taking_audio_p95": None}
    cand = {"status": "done", "ttfa_ms": 1800, "turn_taking_audio_p95": 2200}
    g = evaluate_baseline_gate(base, cand)
    assert g["ok"] is True
    for key in ("ttfa_ms", "turn_taking_audio_p95"):
        c = next(c for c in g["checks"] if c.get("check") == f"regression:{key}")
        assert c["skipped"] is True


def test_gate_audio_metrics_regress_when_both_present():
    base = {"status": "done", "ttfa_ms": 1200, "turn_taking_audio_p95": 1500}
    cand = {"status": "done", "ttfa_ms": 5000, "turn_taking_audio_p95": 9000}
    g = evaluate_baseline_gate(base, cand, max_ttfa_regression_ms=2000, max_turn_audio_p95_regression_ms=2500)
    assert g["ok"] is False
    assert any("ttfa_ms" in r for r in g["reasons"])
    assert any("turn_taking_audio_p95" in r for r in g["reasons"])


def test_gate_audio_metrics_ok_when_within_limit():
    base = {"status": "done", "ttfa_ms": 1200, "turn_taking_audio_p95": 1500}
    cand = {"status": "done", "ttfa_ms": 1300, "turn_taking_audio_p95": 1600}
    g = evaluate_baseline_gate(base, cand)
    assert g["ok"] is True
