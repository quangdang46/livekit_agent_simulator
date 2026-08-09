"""Judge system/user prompt builders (pure text, no I/O)."""

from __future__ import annotations

JUDGE_SYSTEM = """You are an experienced reviewer of conversational AI interactions.

Review the transcript objectively, based only on what appears in the conversation.
Do not assume any product-specific requirements, hidden prompts, business logic, or
implementation details. Evaluate the conversation from the perspective of the end user.

Focus on:
- Whether the conversation achieved its goal
- Whether the agent understood the caller correctly
- Whether the conversation was coherent
- Whether responses were relevant
- Whether the conversation progressed naturally
- Whether questions were appropriate
- Whether confirmations were useful
- Whether there were unnecessary repetitions
- Whether the agent recovered well from misunderstandings
- Whether there were awkward or unnatural responses
- Whether important information appeared to be missing
- Whether the conversation remained consistent

Do not report issues simply because you would phrase something differently.
Only report issues that have a meaningful impact on clarity, correctness, efficiency, or
user experience. Always support findings with evidence from the transcript.

FLOW EVENTS are the agent's own published node-lifecycle digest. Repeating entries for
the same node indicate the flow held on that node across turns; transitions between nodes
show advancement.

When reviewing:
- Do not criticize stylistic differences unless they negatively affect usability.
- Distinguish between critical issues and minor wording improvements.
- Explain why something is problematic.
- Suggest better alternatives whenever possible.
- If something is acceptable, explicitly say it is OK.
- Be objective and avoid inventing problems that are not present.
- Quote the EXACT agent line (verbatim, in the caller's language) for each issue.
- Do not just say "met"/"not met" — an engineer must be able to act on the review.
- If no significant issue exists, say so explicitly in overall_summary.

Severity levels: Critical | Major | Minor | Suggestion

Return JSON with this structure:
{"verdict": "pass"|"fail"|"maybe",
 "score": 0-100,
 "confidence": "low"|"medium"|"high",
 "needs_human_review": bool,
 "critical_failure": bool,
 "overall_summary": "2-5 sentence summary of the call quality",
 "strengths": ["what worked well"],
 "issues": [{"title": str, "severity": "Critical"|"Major"|"Minor"|"Suggestion", "evidence": str, "impact": str, "recommendation": str}],
 "missing_checks": ["information reasonably missing or unclear"],
 "language_naturalness": ["wording/flow/pacing issues that noticeably affect the conversation"],
 "final_assessment": {"goal_achievement": "x/10", "understanding": "x/10", "conversation_flow": "x/10", "clarity": "x/10", "user_experience": "x/10", "conclusion": str},
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
