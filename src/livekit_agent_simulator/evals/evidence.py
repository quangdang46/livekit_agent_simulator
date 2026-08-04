"""Build evidence packet from run turns + tool events (no LLM)."""

from __future__ import annotations

import json
from typing import Any


def format_transcript(turns: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for t in turns:
        lines.append(f"Turn {t.get('turn')}:")
        if t.get("user_text"):
            lines.append(f"  CALLER: {t['user_text']}")
        if t.get("agent_text"):
            lines.append(f"  AGENT: {t['agent_text']}")
        if t.get("tool_errors"):
            lines.append(f"  (tool errors this turn: {t['tool_errors']})")
    return "\n".join(lines) if lines else "(empty)"


def format_tool_spans(tool_events: list[dict[str, Any]]) -> str:
    lines = [
        json.dumps(
            {
                "kind": e.get("kind"),
                "turn": e.get("turn"),
                "name": (e.get("spec") or {}).get("name"),
                "error": (e.get("spec") or {}).get("error"),
                "duration_ms": (e.get("spec") or {}).get("duration_ms"),
            },
            ensure_ascii=False,
        )
        for e in tool_events
    ]
    return "\n".join(lines) if lines else "(none)"


def format_flow_digest(flow_events: list[dict[str, Any]]) -> str:
    """Render a compact, generic digest of flow-lifecycle events.

    ``flow_events`` are opaque payloads the target publishes on the data topics
    configured in ``observe.flow_topics``. Core never interprets the payload
    shape (no hardcoded type names) — each event is rendered as a flat
    ``key=value`` list so the soft LLM judge can reason about node
    hold/advance behavior without the transcript.
    """
    lines: list[str] = []
    for e in flow_events:
        payload = (e.get("spec") or {}).get("payload") or {}
        if isinstance(payload, dict):
            bits = [f"{k}={v}" for k, v in payload.items() if k not in ("_seq", "ts")]
            lines.append(" | ".join(bits) if bits else json.dumps(payload, ensure_ascii=False))
        else:
            lines.append(json.dumps(payload, ensure_ascii=False))
    return "\n".join(lines) if lines else "(none)"


def build_evidence_packet(
    turns: list[dict[str, Any]],
    tool_events: list[dict[str, Any]],
    flow_events: list[dict[str, Any]] | None = None,
) -> dict[str, str]:
    return {
        "transcript": format_transcript(turns),
        "tool_spans": format_tool_spans(tool_events),
        "flow_digest": format_flow_digest(flow_events or []),
    }
