"""Judge system/user prompt builders (pure text, no I/O)."""

from __future__ import annotations

JUDGE_SYSTEM = """You are a strict QA judge for voice-agent test calls.
Grade ONLY from the provided evidence (transcript + tool spans + flow events). Do not invent facts.
If evidence is missing or ambiguous, set needs_human_review=true, lower confidence, and use verdict "maybe".
FLOW EVENTS are the agent's own published node-lifecycle digest (each line a key=value payload from the target's flow data topic). Repeating entries for the same node indicate the flow held on that node across turns; transitions between nodes show advancement.

Evaluate ONLY against the listed criteria. For each criterion set relevant=false if it clearly does not apply to this call (exclude from pass/fail), otherwise relevant=true.

When ANY criterion asks about conversational quality / naturalness (e.g. one-question-per-turn, confirmation strategy, acknowledge-then-redirect, relative-date handling, latency), review the call as a REAL HUMAN would — a product manager listening with a stopwatch and notepad, asking "would a caller feel this is a natural conversation or a scripted bot?". Produce DETAILED conversational feedback in `conversation_feedback`: quote the EXACT agent line (verbatim, in the caller's language) for each rule violated, the human impact ("the caller said X but the agent ignored it"), and a severity. Do NOT just say "met"/"not met" — an engineer must be able to act on the notes to improve conversation quality.

Return JSON only:
{"verdict": "pass"|"fail"|"maybe",
 "score": 0-100,
 "confidence": "low"|"medium"|"high",
 "needs_human_review": bool,
 "critical_failure": bool,
 "criteria": [{"criterion": str, "met": bool, "relevant": bool, "evidence": str}],
 "conversation_feedback": [{"issue": str, "severity": "low"|"medium"|"high", "agent_line": str, "why": str}],
 "notes": str}
"""


def build_user_prompt(
    *,
    pass_criteria: list[str],
    transcript: str,
    tool_spans: str,
    flow_digest: str | None = None,
    goals_met: bool | None = None,
) -> str:
    parts = [
        "PASS CRITERIA:",
        *[f"- {c}" for c in pass_criteria],
        "",
        "TRANSCRIPT:",
        transcript or "(empty)",
        "",
        "TOOL SPANS:",
        tool_spans or "(none)",
    ]
    if flow_digest:
        parts.extend(
            [
                "",
                "FLOW EVENTS (node lifecycle — strong evidence for hold/advance behavior):",
                flow_digest,
            ]
        )
    if goals_met:
        parts.extend(
            [
                "",
                "NOTE: This is a goals_met check. Evaluate whether the CALLER "
                "(simulated human) stated or pursued each listed goal. "
                "Agent responses alone do not satisfy caller goals.",
            ]
        )
    return "\n".join(parts)
