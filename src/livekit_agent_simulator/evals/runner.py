"""Judge runner — evidence → relevancy → backend → normalize."""

from __future__ import annotations

import json
import re
from typing import Any

from ..config import JudgeConfig
from .aggregate import aggregate_judges
from .backend import JudgeBackend, backend_from_config
from .evidence import build_evidence_packet
from .presets import expand_criteria, expand_judge_group
from .prompt import JUDGE_SYSTEM, build_user_prompt
from .relevancy import apply_relevancy
from .resolve import resolve_judge
from .types import JudgmentResult, parse_judgment_payload


def _strip_json_fence(text: str) -> str:
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.IGNORECASE)
        s = re.sub(r"\s*```$", "", s)
    return s.strip()


def _try_parse(s: str) -> dict[str, Any] | None:
    """Parse s as a JSON object, returning None on failure."""
    try:
        raw = json.loads(s)
    except json.JSONDecodeError:
        return None
    return raw if isinstance(raw, dict) else None


def _json_string_end(s: str, i: int) -> int | None:
    """Given s[i] == '"', return the index just past the closing quote (or None).

    Handles \" escapes and skips a trailing backslash so a truncated string
    like ``"The conversation i`` still yields a recoverable end point.
    """
    n = len(s)
    j = i + 1
    while j < n:
        c = s[j]
        if c == "\\":
            j += 2  # skip the escaped char (backslash alone at EOF is fine)
            continue
        if c == '"':
            return j + 1
        j += 1
    return None


def _repair_truncated_json(text: str) -> str | None:
    """Best-effort repair of an LLM JSON object that is truncated or wrapped in prose.

    Handles:
    1. JSON fences, leading prose (trim to first '{'), trailing prose after the
       top-level object.
    2. An unterminated trailing string (the tail is kept and the string closed).
    3. Trailing commas before '}' / ']'.
    4. Missing closing braces/brackets — only containers still open at the point
       of truncation are closed (no rebalancing of nested containers).

    Returns the repaired JSON, or None if it cannot be recovered.
    """
    s = _strip_json_fence(text)
    start = s.find("{")
    if start < 0:
        return None
    s = s[start:]

    stack: list[str] = []
    truncated_in_string = False
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c == '"':
            end = _json_string_end(s, i)
            if end is None:
                # Unterminated trailing string: keep the partial text and close
                # the string after the loop.
                truncated_in_string = True
                break
            i = end
            continue
        if c in "{[":
            stack.append(c)
        elif c == "}":
            if not stack or stack[-1] != "{":
                # Stray closer / already closed: drop everything from here.
                s = s[:i]
                break
            stack.pop()
            if not stack:
                # Top-level object closed — drop any trailing prose.
                s = s[: i + 1]
                break
        elif c == "]":
            if not stack or stack[-1] != "[":
                s = s[:i]
                break
            stack.pop()
        i += 1

    if truncated_in_string:
        # A trailing backslash would escape the closing quote — drop it first.
        if s.endswith("\\"):
            s = s[:-1].rstrip()
        s += '"'
    # Close any containers still open at the point of truncation.
    while stack:
        opener = stack.pop()
        if s.endswith(","):
            s = s[:-1].rstrip()
        s += "}" if opener == "{" else "]"

    # Final cleanup: trailing commas (incl. inside) and stray trailing backslash.
    s = re.sub(r",\s*([}\]])", r"\1", s)
    s = s.rstrip()
    while s.endswith(","):
        s = s[:-1].rstrip()
    if s.endswith("\\"):
        s = s[:-1].rstrip()

    # Dangling key repair: an unterminated string may have been a KEY cut off
    # before its `: value` (e.g. `"needs_huma` at EOF). After closing the quote
    # and containers it becomes `"key"}` — an invalid dangling key. Drop the
    # dangling key (and its preceding comma) if a closing container follows.
    # Only match a COMMA-preceded string (a key position) — a `: "value"}` is a
    # legitimate truncated value and must stay.
    m = re.search(r',\s*"[^"]*"\s*([}\]])$', s)
    if m:
        # Cut from the comma that precedes the dangling key; keep the closer.
        s = s[: m.start(0)].rstrip() + m.group(1)

    return s if _try_parse(s) is not None else None


def _parse_llm_json(text: str) -> JudgmentResult:
    stripped = _strip_json_fence(text)
    raw = _try_parse(stripped)
    if raw is None:
        raw = _try_parse(_repair_truncated_json(stripped) or "")
    if raw is None:
        return JudgmentResult(
            verdict="error",
            notes=f"Judge returned non-JSON: {text[:500]}",
        )
    return parse_judgment_payload(raw)


async def _judge(
    backend: JudgeBackend,
    pass_criteria: list[str],
    turns: list[dict[str, Any]],
    tool_events: list[dict[str, Any]],
    *,
    flow_events: list[dict[str, Any]] | None = None,
    goals_met: bool | None = None,
) -> dict[str, Any]:
    if not pass_criteria:
        return JudgmentResult(verdict="skipped", notes="No criteria.").to_dict()

    try:
        criteria = expand_criteria([str(c) for c in pass_criteria])
    except KeyError as e:
        return JudgmentResult(verdict="error", notes=str(e)).to_dict()

    packet = build_evidence_packet(turns, tool_events, flow_events or [])
    user = build_user_prompt(
        pass_criteria=criteria,
        transcript=packet["transcript"],
        tool_spans=packet["tool_spans"],
        flow_digest=packet["flow_digest"],
        goals_met=goals_met,
    )
    try:
        text = await backend.complete_json(system=JUDGE_SYSTEM, user=user)
    except Exception as e:
        return JudgmentResult(
            verdict="error",
            notes=f"{type(e).__name__}: {e}",
        ).to_dict()

    result = apply_relevancy(_parse_llm_json(text))
    return result.to_dict()


async def judge_run(
    judge_cfg: JudgeConfig | None,
    sim_api_key: str,
    pass_criteria: list[str],
    turns: list[dict[str, Any]],
    tool_events: list[dict[str, Any]],
    flow_events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    resolved = resolve_judge(judge_cfg, sim_api_key=sim_api_key)
    if not resolved.ready:
        return {
            "verdict": "skipped",
            "notes": resolved.skip_reason
            or "Judge not ready (check judge.base_url/api_key or simulator key).",
        }
    try:
        backend = backend_from_config(judge_cfg or JudgeConfig(), sim_api_key)
    except Exception as e:
        return {
            "verdict": "error",
            "notes": f"Judge backend setup failed: {type(e).__name__}: {e}",
        }
    if backend is None:
        return {
            "verdict": "skipped",
            "notes": resolved.skip_reason or "Judge backend unavailable.",
        }
    return await _judge(
        backend, pass_criteria, turns, tool_events,
        flow_events=flow_events, goals_met=None,
    )


async def judge_goals(
    judge_cfg: JudgeConfig | None,
    sim_api_key: str,
    goals: list[str],
    min_goals: int,
    turns: list[dict[str, Any]],
) -> dict[str, Any]:
    """Judge whether the simulated caller stated/pursued at least min_goals goals."""
    if judge_cfg is None:
        return {
            "verdict": "skipped",
            "notes": "goals_met skipped: no judge config.",
            "score": 0,
        }
    resolved = resolve_judge(judge_cfg, sim_api_key=sim_api_key)
    if not resolved.ready:
        return {"verdict": "skipped", "notes": resolved.skip_reason, "score": 0}
    try:
        backend = backend_from_config(judge_cfg, sim_api_key)
    except Exception as e:
        return {
            "verdict": "error",
            "notes": f"Judge backend setup failed: {type(e).__name__}: {e}",
            "score": 0,
        }
    if backend is None:
        return {"verdict": "skipped", "notes": resolved.skip_reason, "score": 0}

    criteria = [
        "The simulated caller stated or pursued the following goal(s) before the "
        f"call ended: {goals}. Verify at least {min_goals} of {len(goals)} goals "
        "were explicitly mentioned or pursued."
    ]
    return await _judge(backend, criteria, turns, [], goals_met=True)


async def judge_run_multi(
    judge_cfg: JudgeConfig | None,
    sim_api_key: str,
    judges: list[dict[str, Any]],
    mode: str,
    turns: list[dict[str, Any]],
    tool_events: list[dict[str, Any]],
    flow_events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run one LLM judge per group; aggregate by mode all|majority|any."""
    if not judges:
        return {"verdict": "skipped", "notes": "No judges."}

    resolved = resolve_judge(judge_cfg, sim_api_key=sim_api_key)
    if not resolved.ready:
        return {"verdict": "skipped", "notes": resolved.skip_reason}
    try:
        backend = backend_from_config(judge_cfg or JudgeConfig(), sim_api_key)
    except Exception as e:
        return {
            "verdict": "error",
            "notes": f"Judge backend setup failed: {type(e).__name__}: {e}",
        }
    if backend is None:
        return {"verdict": "skipped", "notes": resolved.skip_reason}

    results: list[dict[str, Any]] = []
    for j in judges:
        try:
            group = expand_judge_group(j)
        except KeyError as e:
            results.append(
                {
                    "verdict": "error",
                    "notes": str(e),
                    "judge_id": str(j.get("id") or "judge"),
                }
            )
            continue
        jid = str(group.get("id") or "judge")
        criteria = list(group.get("criteria") or [])
        one = await _judge(
            backend, criteria, turns, tool_events,
            flow_events=flow_events, goals_met=None,
        )
        one = dict(one or {})
        one["judge_id"] = jid
        results.append(one)

    return aggregate_judges(results, mode)
