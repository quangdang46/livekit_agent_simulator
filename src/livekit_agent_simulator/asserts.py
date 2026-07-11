"""Deterministic scenario asserts (tools, transcript phrases, lightweight outcomes).

Portable: no consumer-specific tool names baked into core — scenarios declare expects.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ToolExpect:
    name: str
    min_count: int = 1
    max_count: int | None = None
    # Subset match against tool.start spec.payload (or nested args/arguments).
    args_contains: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TranscriptExpect:
    role: str = "agent"  # agent | user | any
    contains_any: tuple[str, ...] = ()
    must_not_match: str | None = None  # regex


@dataclass(frozen=True)
class OutcomeExpect:
    """Post-run outcome check.

    type:
      - transcript_contains: any agent/user text matches phrases (contains_any)
      - llm_bool: deferred to judge layer (evaluated when judge runs)
      - recovery: agent re-engages after sim barge-in / interruption
    """

    id: str
    type: str  # transcript_contains | llm_bool | recovery
    phrases: tuple[str, ...] = ()
    prompt: str | None = None  # for llm_bool
    role: str = "any"
    min_agent_finals_after_barge_in: int = 1
    min_interruptions: int = 0
    max_ms_after_barge_to_agent_final: int | None = None


@dataclass
class AssertSpec:
    tools: list[ToolExpect] = field(default_factory=list)
    transcript: list[TranscriptExpect] = field(default_factory=list)
    outcomes: list[OutcomeExpect] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.tools or self.transcript or self.outcomes)


def parse_assert_spec(spec: dict[str, Any], path_label: str = "Assert") -> AssertSpec:
    tools: list[ToolExpect] = []
    for i, raw in enumerate(spec.get("tools") or []):
        if not isinstance(raw, dict) or not raw.get("name"):
            raise ValueError(f"{path_label}: tools[{i}] needs name")
        args = raw.get("args_contains") or raw.get("args") or {}
        if not isinstance(args, dict):
            raise ValueError(f"{path_label}: tools[{i}].args_contains must be object")
        tools.append(
            ToolExpect(
                name=str(raw["name"]).strip(),
                min_count=int(raw.get("min_count", 1)),
                max_count=int(raw["max_count"]) if raw.get("max_count") is not None else None,
                args_contains=dict(args),
            )
        )

    transcript: list[TranscriptExpect] = []
    for i, raw in enumerate(spec.get("transcript") or []):
        if not isinstance(raw, dict):
            raise ValueError(f"{path_label}: transcript[{i}] must be object")
        contains = raw.get("contains_any") or raw.get("contains") or []
        if isinstance(contains, str):
            contains = [contains]
        if not isinstance(contains, list):
            raise ValueError(f"{path_label}: transcript[{i}].contains_any must be array/string")
        transcript.append(
            TranscriptExpect(
                role=str(raw.get("role", "agent")),
                contains_any=tuple(str(x) for x in contains),
                must_not_match=str(raw["must_not_match"]) if raw.get("must_not_match") else None,
            )
        )

    outcomes: list[OutcomeExpect] = []
    for i, raw in enumerate(spec.get("outcomes") or []):
        if not isinstance(raw, dict) or not raw.get("id"):
            raise ValueError(f"{path_label}: outcomes[{i}] needs id")
        otype = str(raw.get("type", "transcript_contains"))
        if otype not in ("transcript_contains", "llm_bool", "recovery"):
            raise ValueError(f"{path_label}: outcomes[{i}].type unsupported: {otype}")
        phrases = raw.get("phrases") or raw.get("contains_any") or []
        if isinstance(phrases, str):
            phrases = [phrases]
        max_ms = raw.get("max_ms_after_barge_to_agent_final")
        outcomes.append(
            OutcomeExpect(
                id=str(raw["id"]),
                type=otype,
                phrases=tuple(str(p) for p in phrases),
                prompt=str(raw["prompt"]) if raw.get("prompt") else None,
                role=str(raw.get("role", "any")),
                min_agent_finals_after_barge_in=int(
                    raw.get("min_agent_finals_after_barge_in", 1)
                ),
                min_interruptions=int(raw.get("min_interruptions", 0)),
                max_ms_after_barge_to_agent_final=int(max_ms) if max_ms is not None else None,
            )
        )

    return AssertSpec(tools=tools, transcript=transcript, outcomes=outcomes)


def _tool_args_blob(spec: dict[str, Any]) -> dict[str, Any]:
    payload = spec.get("payload")
    if isinstance(payload, dict):
        for key in ("args", "arguments", "input", "params"):
            if isinstance(payload.get(key), dict):
                return payload[key]
        return payload
    for key in ("args", "arguments"):
        if isinstance(spec.get(key), dict):
            return spec[key]
    return {}


def _dict_contains(hay: dict[str, Any], needle: dict[str, Any]) -> bool:
    for k, v in needle.items():
        if k not in hay:
            return False
        if isinstance(v, dict) and isinstance(hay[k], dict):
            if not _dict_contains(hay[k], v):
                return False
        elif hay[k] != v and str(hay[k]) != str(v):
            return False
    return True


def _transcript_texts(events: list[dict[str, Any]], role: str) -> list[str]:
    texts: list[str] = []
    for e in events:
        kind = str(e.get("kind") or "")
        if not kind.startswith("transcript.") or not kind.endswith(".final"):
            continue
        if role == "agent" and "agent" not in kind:
            continue
        if role == "user" and "user" not in kind:
            continue
        t = (e.get("spec") or {}).get("text")
        if isinstance(t, str) and t.strip():
            texts.append(t.strip())
    return texts


def evaluate_asserts(events: list[dict[str, Any]], asserts: AssertSpec | None) -> dict[str, Any]:
    """Deterministic checks only (llm_bool outcomes marked pending)."""
    if asserts is None or asserts.empty:
        return {"pass": True, "skipped": True, "checks": []}

    checks: list[dict[str, Any]] = []
    tool_starts = [e for e in events if e.get("kind") == "tool.start"]

    for te in asserts.tools:
        matches = []
        for e in tool_starts:
            spec = e.get("spec") or {}
            name = str(spec.get("name") or "")
            if name != te.name:
                continue
            if te.args_contains:
                args = _tool_args_blob(spec)
                if not _dict_contains(args, te.args_contains):
                    continue
            matches.append(e)
        n = len(matches)
        ok = n >= te.min_count and (te.max_count is None or n <= te.max_count)
        checks.append(
            {
                "check": f"tool:{te.name}",
                "pass": ok,
                "expected_min": te.min_count,
                "expected_max": te.max_count,
                "actual": n,
                "args_contains": te.args_contains or None,
            }
        )

    for i, tr in enumerate(asserts.transcript):
        role = tr.role if tr.role in ("agent", "user") else "any"
        if role == "any":
            texts = _transcript_texts(events, "agent") + _transcript_texts(events, "user")
        else:
            texts = _transcript_texts(events, role)
        blob = "\n".join(texts)
        ok = True
        reason = None
        if tr.contains_any:
            ok = any(p.lower() in blob.lower() for p in tr.contains_any)
            if not ok:
                reason = f"none of {list(tr.contains_any)} found in {role} transcript"
        if ok and tr.must_not_match:
            if re.search(tr.must_not_match, blob, re.I):
                ok = False
                reason = f"matched forbidden pattern {tr.must_not_match!r}"
        checks.append(
            {
                "check": f"transcript[{i}]",
                "pass": ok,
                "role": tr.role,
                "reason": reason,
            }
        )

    pending_llm: list[dict[str, Any]] = []
    barge_ms: list[int] = []
    for e in events:
        kind = str(e.get("kind") or "")
        spec = e.get("spec") if isinstance(e.get("spec"), dict) else {}
        try:
            mono = int(e.get("ts_mono_ms") or 0)
        except (TypeError, ValueError):
            mono = 0
        if kind == "sim.script.cue" and spec.get("barge_in"):
            barge_ms.append(mono)
        if kind == "interruption" and (
            spec.get("barge_in") or str(spec.get("by") or "") == "sim"
        ):
            barge_ms.append(mono)
    barge_ms = sorted(set(barge_ms))
    agent_final_ms: list[int] = []
    for e in events:
        if e.get("kind") != "transcript.agent.final":
            continue
        try:
            agent_final_ms.append(int(e.get("ts_mono_ms") or 0))
        except (TypeError, ValueError):
            continue
    interruptions = [e for e in events if e.get("kind") == "interruption"]

    for oc in asserts.outcomes:
        if oc.type == "transcript_contains":
            role = oc.role if oc.role in ("agent", "user") else "any"
            if role == "any":
                texts = _transcript_texts(events, "agent") + _transcript_texts(events, "user")
            else:
                texts = _transcript_texts(events, role)
            blob = "\n".join(texts)
            ok = bool(oc.phrases) and any(p.lower() in blob.lower() for p in oc.phrases)
            checks.append(
                {
                    "check": f"outcome:{oc.id}",
                    "pass": ok,
                    "type": oc.type,
                    "phrases": list(oc.phrases),
                }
            )
        elif oc.type == "recovery":
            after = 0
            first_barge = barge_ms[0] if barge_ms else None
            if first_barge is not None:
                after = sum(1 for t in agent_final_ms if t > first_barge)
            ok = after >= oc.min_agent_finals_after_barge_in
            if oc.min_interruptions and len(interruptions) < oc.min_interruptions:
                ok = False
            timing_ok = True
            recovery_ms = None
            if ok and oc.max_ms_after_barge_to_agent_final is not None and first_barge is not None:
                nxt = next((t for t in agent_final_ms if t > first_barge), None)
                if nxt is None:
                    timing_ok = False
                else:
                    recovery_ms = nxt - first_barge
                    timing_ok = recovery_ms <= oc.max_ms_after_barge_to_agent_final
                ok = ok and timing_ok
            checks.append(
                {
                    "check": f"outcome:{oc.id}",
                    "pass": ok,
                    "type": "recovery",
                    "agent_finals_after_barge_in": after,
                    "expected_min": oc.min_agent_finals_after_barge_in,
                    "interruptions": len(interruptions),
                    "recovery_ms": recovery_ms,
                    "max_ms_after_barge_to_agent_final": oc.max_ms_after_barge_to_agent_final,
                }
            )
        elif oc.type == "llm_bool":
            pending_llm.append({"id": oc.id, "prompt": oc.prompt or oc.id})
            checks.append(
                {
                    "check": f"outcome:{oc.id}",
                    "pass": True,  # does not fail hard assert; judge layer decides
                    "type": "llm_bool",
                    "pending_judge": True,
                    "prompt": oc.prompt,
                }
            )

    hard = [c for c in checks if not c.get("pending_judge")]
    return {
        "pass": all(bool(c.get("pass")) for c in hard) if hard else True,
        "skipped": False,
        "checks": checks,
        "pending_llm_outcomes": pending_llm,
    }
