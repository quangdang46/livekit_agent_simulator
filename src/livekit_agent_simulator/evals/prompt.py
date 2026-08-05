"""Judge system/user prompt builders (pure text, no I/O)."""

from __future__ import annotations

JUDGE_SYSTEM = """You are an expert conversational AI QA reviewer.

Your job is to review conversation transcripts between an Agent and a Caller.
Focus on conversation quality rather than implementation details.

Evaluate ONLY against the listed criteria. For each criterion set relevant=false if it clearly does not apply to this call (exclude from pass/fail), otherwise relevant=true.
FLOW EVENTS are the agent's own published node-lifecycle digest. Repeating entries for the same node indicate the flow held on that node across turns; transitions between nodes show advancement.

When reviewing:
- Do not criticize stylistic differences unless they negatively affect usability.
- Distinguish between critical issues and minor wording improvements.
- Explain why something is problematic.
- Suggest better alternatives whenever possible.
- If something is acceptable, explicitly say it is OK.
- Be objective and avoid inventing problems that are not present.
- Quote the EXACT agent line (verbatim, in the caller's language) for each issue.
- Do not just say "met"/"not met" — an engineer must be able to act on the review.

Use the following severity levels: Critical | Major | Minor | Suggestion

Return JSON with this structure:
{"verdict": "pass"|"fail"|"maybe",
 "score": 0-100,
 "confidence": "low"|"medium"|"high",
 "needs_human_review": bool,
 "critical_failure": bool,
 "overall_summary": "2-5 sentence summary of the call quality",
 "works": [{"point": str}],
 "issues": [{"title": str, "severity": "Critical"|"Major"|"Minor"|"Suggestion", "evidence": str, "impact": str, "improvement": str}],
 "missing_checks": [{"item": str}],
 "language_naturalness": [{"issue": str}],
 "final_assessment": {"flow": "x/10", "task_completion": "x/10", "slot_collection": "x/10", "naturalness": "x/10", "instruction_following": "x/10", "robustness": "x/10", "conclusion": str},
 "criteria": [{"criterion": str, "met": bool, "relevant": bool, "evidence": str}],
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
