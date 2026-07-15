"""Judge system/user prompt builders (pure text, no I/O)."""

from __future__ import annotations

JUDGE_SYSTEM = """You are a strict QA judge for voice-agent test calls.
Grade ONLY from the provided evidence (transcript + tool spans). Do not invent facts.
If evidence is missing or ambiguous, set needs_human_review=true, lower confidence, and use verdict "maybe".

Evaluate ONLY against the listed criteria. For each criterion set relevant=false if it clearly does not apply to this call (exclude from pass/fail), otherwise relevant=true.

Return JSON only:
{"verdict": "pass"|"fail"|"maybe",
 "score": 0-100,
 "confidence": "low"|"medium"|"high",
 "needs_human_review": bool,
 "critical_failure": bool,
 "criteria": [{"criterion": str, "met": bool, "relevant": bool, "evidence": str}],
 "notes": str}
"""


def build_user_prompt(
    *,
    pass_criteria: list[str],
    transcript: str,
    tool_spans: str,
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
