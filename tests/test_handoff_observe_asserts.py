"""Handoff observe + asserts — agent_handoff chat items → handoff.* events + assert.

LiveKit AgentSession emits ``AgentHandoff`` items in the chat history when
control transfers between agents (tool-based handoff or WarmTransferTask). The
observer surfaces them as ``handoff`` events; asserts can require or forbid
transfers (``handoff`` / ``no_unplanned_handoff``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from livekit.protocol.agent_pb import agent_session as agent_pb

from livekit_agent_simulator.asserts import evaluate_asserts, parse_assert_spec
from livekit_agent_simulator.livekit.agent_session_observer import AgentSessionObserver
from livekit_agent_simulator.logging.event_writer import EventWriter
from livekit_agent_simulator.scenario import parse_scenario

EX = Path(__file__).resolve().parents[1] / "templates" / "examples"


class _SinkWriter:
    """Collect events in-memory instead of writing to disk."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.run_start_mono = 0.0

    def emit(self, kind: str, spec: dict[str, Any], *, source: str = "", **kw: Any) -> dict[str, Any]:
        event = {"kind": kind, "spec": spec, "source": source, "ts_mono_ms": 0}
        self.events.append(event)
        return event


def _make_observer(writer: EventWriter) -> AgentSessionObserver:
    class _Room:
        def __init__(self) -> None:
            self.local_participant = _LocalParticipant()

        def register_byte_stream_handler(self, topic: str, handler: Any) -> None:
            pass

        def unregister_byte_stream_handler(self, topic: str) -> None:
            pass

    class _LocalParticipant:
        def __init__(self) -> None:
            self.calls = []

        async def stream_bytes(self, **kw: Any) -> Any:
            self.calls.append(kw)
            return _ByteWriter()

    class _ByteWriter:
        async def write(self, data: bytes) -> None:
            pass

        async def aclose(self) -> None:
            pass

    return AgentSessionObserver(_Room(), writer, "agent-1")


def _handoff_item(old: str, new: str) -> agent_pb.ChatContext.ChatItem:
    item = agent_pb.ChatContext.ChatItem()
    item.agent_handoff.old_agent_id = old
    item.agent_handoff.new_agent_id = new
    return item


# -------------------------------------------------------------------------- template

def test_handoff_template_parses() -> None:
    s = parse_scenario(EX / "handoff-observe.yaml")
    assert s.id == "handoff-observe"
    assert s.asserts is not None
    assert any(o.type == "handoff" for o in s.asserts.outcomes)


# -------------------------------------------------------------------------- observe

def test_reconcile_history_emits_handoff() -> None:
    writer = _SinkWriter()
    observer = _make_observer(writer)  # type: ignore[arg-type]
    items = [_handoff_item("triage", "billing")]
    observer._reconcile_history(items)
    kinds = [e["kind"] for e in writer.events]
    assert "handoff" in kinds
    h = next(e for e in writer.events if e["kind"] == "handoff")
    assert h["spec"]["old_agent_id"] == "triage"
    assert h["spec"]["new_agent_id"] == "billing"


def test_conversation_item_added_emits_handoff() -> None:
    writer = _SinkWriter()
    observer = _make_observer(writer)  # type: ignore[arg-type]
    added = agent_pb.AgentSessionEvent.ConversationItemAdded()
    added.item.CopyFrom(_handoff_item("a", "b"))
    observer._handle_conversation_item_added(added)
    assert any(e["kind"] == "handoff" for e in writer.events)


def test_handoff_skips_session_bootstrap_false_positive() -> None:
    """Regression: OpenAI Realtime emits an AgentHandoff at session start with
    old_agent_id empty — that is NOT a transfer and must not emit a handoff event."""
    writer = _SinkWriter()
    observer = _make_observer(writer)  # type: ignore[arg-type]
    item = _handoff_item("", "dtmf_agent")  # old_agent_id empty = bootstrap
    observer._reconcile_history([item])
    assert not any(e["kind"] == "handoff" for e in writer.events)


def test_handoff_created_at_is_json_serializable() -> None:
    """Regression: a protobuf Timestamp crashed the event writer (TypeError)."""
    import json
    from google.protobuf.timestamp_pb2 import Timestamp

    writer = _SinkWriter()
    observer = _make_observer(writer)  # type: ignore[arg-type]
    item = _handoff_item("triage", "billing")
    ts = Timestamp()
    ts.seconds = 1_700_000_000
    item.agent_handoff.created_at.CopyFrom(ts)
    observer._reconcile_history([item])
    h = next(e for e in writer.events if e["kind"] == "handoff")
    # The spec must serialize to JSON (the raw Timestamp would raise TypeError).
    json.dumps(h["spec"])
    assert h["spec"]["created_at"] == "2023-11-14T22:13:20Z"


# -------------------------------------------------------------------------- asserts

def test_handoff_outcome_requires_count() -> None:
    spec = parse_assert_spec({"outcomes": [{"id": "h", "type": "handoff", "min_handoffs": 2}]})
    events = [
        {"kind": "handoff", "spec": {"old_agent_id": "a", "new_agent_id": "b"}, "ts_mono_ms": 10},
        {"kind": "handoff", "spec": {"old_agent_id": "b", "new_agent_id": "c"}, "ts_mono_ms": 20},
    ]
    res = evaluate_asserts(events, spec)
    assert res["pass"] is True
    c = res["checks"][0]
    assert c["handoffs"] == 2


def test_handoff_outcome_fails_below_min() -> None:
    spec = parse_assert_spec({"outcomes": [{"id": "h", "type": "handoff", "min_handoffs": 3}]})
    events = [
        {"kind": "handoff", "spec": {"old_agent_id": "a", "new_agent_id": "b"}, "ts_mono_ms": 10},
    ]
    res = evaluate_asserts(events, spec)
    assert res["pass"] is False


def test_no_unplanned_handoff_passes_without_handoffs() -> None:
    spec = parse_assert_spec({"outcomes": [{"id": "n", "type": "no_unplanned_handoff"}]})
    res = evaluate_asserts([], spec)
    assert res["pass"] is True
    assert res["checks"][0]["handoffs"] == 0


def test_no_unplanned_handoff_fails_with_handoff() -> None:
    spec = parse_assert_spec({"outcomes": [{"id": "n", "type": "no_unplanned_handoff"}]})
    events = [{"kind": "handoff", "spec": {"old_agent_id": "a", "new_agent_id": "b"}, "ts_mono_ms": 10}]
    res = evaluate_asserts(events, spec)
    assert res["pass"] is False
    assert res["checks"][0]["reason"]


def test_unknown_handoff_type_rejected() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        parse_assert_spec({"outcomes": [{"id": "x", "type": "not_a_handoff_type"}]})
