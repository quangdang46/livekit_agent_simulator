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


def _parse_llm_json(text: str) -> JudgmentResult:
    try:
        raw = json.loads(_strip_json_fence(text))
    except json.JSONDecodeError:
        return JudgmentResult(
            verdict="error",
            notes=f"Judge returned non-JSON: {text[:500]}",
        )
    if not isinstance(raw, dict):
        return JudgmentResult(verdict="error", notes="Judge JSON was not an object.")
    return parse_judgment_payload(raw)


async def _judge(
    backend: JudgeBackend,
    pass_criteria: list[str],
    turns: list[dict[str, Any]],
    tool_events: list[dict[str, Any]],
    *,
    goals_met: bool | None = None,
) -> dict[str, Any]:
    if not pass_criteria:
        return JudgmentResult(verdict="skipped", notes="No criteria.").to_dict()

    try:
        criteria = expand_criteria([str(c) for c in pass_criteria])
    except KeyError as e:
        return JudgmentResult(verdict="error", notes=str(e)).to_dict()

    packet = build_evidence_packet(turns, tool_events)
    user = build_user_prompt(
        pass_criteria=criteria,
        transcript=packet["transcript"],
        tool_spans=packet["tool_spans"],
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
    google_api_key: str,
    pass_criteria: list[str],
    turns: list[dict[str, Any]],
    tool_events: list[dict[str, Any]],
) -> dict[str, Any]:
    resolved = resolve_judge(judge_cfg, google_api_key=google_api_key)
    if not resolved.ready:
        return {
            "verdict": "skipped",
            "notes": resolved.skip_reason
            or "Judge not ready (check judge.base_url/api_key or Google key).",
        }
    try:
        backend = backend_from_config(judge_cfg or JudgeConfig(), google_api_key)
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
        backend, pass_criteria, turns, tool_events, goals_met=None
    )


async def judge_goals(
    judge_cfg: JudgeConfig | None,
    google_api_key: str,
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
    resolved = resolve_judge(judge_cfg, google_api_key=google_api_key)
    if not resolved.ready:
        return {"verdict": "skipped", "notes": resolved.skip_reason, "score": 0}
    try:
        backend = backend_from_config(judge_cfg, google_api_key)
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
    google_api_key: str,
    judges: list[dict[str, Any]],
    mode: str,
    turns: list[dict[str, Any]],
    tool_events: list[dict[str, Any]],
) -> dict[str, Any]:
    """Run one LLM judge per group; aggregate by mode all|majority|any."""
    if not judges:
        return {"verdict": "skipped", "notes": "No judges."}

    resolved = resolve_judge(judge_cfg, google_api_key=google_api_key)
    if not resolved.ready:
        return {"verdict": "skipped", "notes": resolved.skip_reason}
    try:
        backend = backend_from_config(judge_cfg or JudgeConfig(), google_api_key)
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
            backend, criteria, turns, tool_events, goals_met=None
        )
        one = dict(one or {})
        one["judge_id"] = jid
        results.append(one)

    return aggregate_judges(results, mode)
