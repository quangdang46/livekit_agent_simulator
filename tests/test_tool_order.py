"""P1.I — Assert.tool_order required subsequence of tool.start names."""

from __future__ import annotations

import pytest

from livekit_agent_simulator.asserts import evaluate_asserts, parse_assert_spec


def _start(name: str, ms: int = 1) -> dict:
    return {"kind": "tool.start", "ts_mono_ms": ms, "spec": {"name": name}}


def test_parse_tool_order():
    spec = parse_assert_spec({"tool_order": ["lookup", "book"]})
    assert spec.tool_order == ("lookup", "book")
    assert not spec.empty


def test_order_pass_with_extras():
    events = [
        _start("lookup", 1),
        _start("noise_tool", 2),
        _start("book", 3),
    ]
    spec = parse_assert_spec({"tool_order": ["lookup", "book"]})
    out = evaluate_asserts(events, spec)
    assert out["pass"] is True
    c = next(x for x in out["checks"] if x["check"] == "tool_order")
    assert c["pass"] is True


def test_order_fail_wrong_sequence():
    events = [_start("book", 1), _start("lookup", 2)]
    spec = parse_assert_spec({"tool_order": ["lookup", "book"]})
    out = evaluate_asserts(events, spec)
    assert out["pass"] is False


def test_order_fail_missing():
    events = [_start("lookup", 1)]
    spec = parse_assert_spec({"required_order": ["lookup", "book"]})
    out = evaluate_asserts(events, spec)
    assert out["pass"] is False


def test_empty_without_order_still_skips():
    out = evaluate_asserts([], parse_assert_spec({}))
    assert out.get("skipped") is True
