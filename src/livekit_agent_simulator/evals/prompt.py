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

TEST INSTRUMENTATION — read before reporting "the agent leaked an internal string":
Scenarios configure agent messages as deliberate MARKER strings to make behaviour
observable in the transcript (e.g. a global fallback message and a per-node override
message configured with distinct sentinel text, precisely so the reviewer can tell
WHICH layer produced it). A marker appearing in the transcript is the scenario working
as designed, not the agent exposing internals to a caller. Do not report marker/sentinel
strings, configured fallback/retry text, or their repetition across turns as UX defects.
Judge whether the right marker was produced in the right situation — and where the run
supplies an assert contract (machine-checked outcomes), that contract is authoritative:
if it passed, the behavior it covers is correct, and the call should not be downgraded for
it. Only flag such text when the transcript shows a marker where the scenario did NOT
configure one, or when the assert contract for it failed.

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


def build_assert_digest(assert_verify: object) -> str | None:
    """Render the machine-checked assert contract for the judge prompt.

    The judge otherwise sees only a conversation and re-litigates behavior that
    the run already proved correct (e.g. reading a deliberate test marker as the
    agent leaking an internal string). Passing the contract makes the authoritative
    signal visible instead of merely implied.
    """
    if not isinstance(assert_verify, dict):
        return None
    checks = assert_verify.get("checks") or []
    if not checks:
        return None
    lines: list[str] = []
    overall = assert_verify.get("pass")
    if overall is not None:
        lines.append(f"Overall: {'PASS' if overall else 'FAIL'}")
    for chk in checks:
        if not isinstance(chk, dict):
            continue
        name = chk.get("check") or chk.get("id") or chk.get("type") or "check"
        ok = chk.get("pass")
        mark = "PASS" if ok else ("FAIL" if ok is False else "?")
        detail = ""
        phrases = chk.get("phrases")
        if isinstance(phrases, list) and phrases:
            detail = f" — expects {phrases!r}"
            if chk.get("negate"):
                detail += " (must NOT appear)"
        lines.append(f"- [{mark}] {name}{detail}")
    return "\n".join(lines) if lines else None


def build_user_prompt(
    *,
    pass_criteria: list[str],
    transcript: str,
    tool_spans: str,
    flow_digest: str | None = None,
    goals_met: bool | None = None,
    assert_digest: str | None = None,
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
    if assert_digest:
        parts.extend(
            [
                "",
                "ASSERT CONTRACT (machine-checked; authoritative when present):",
                assert_digest,
                "",
                "The checks above passed or failed on their own. Treat a PASSED check as "
                "proof the behavior it covers is correct — do not re-litigate it as a UX "
                "problem below. Judge only what the contract does not cover.",
            ]
        )
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
