"""Relevancy gate — Hamming step A (post-parse; exclude irrelevant criteria)."""

from __future__ import annotations

from dataclasses import replace

from .types import JudgmentResult


def apply_relevancy(result: JudgmentResult) -> JudgmentResult:
    """Drop irrelevant criteria from pass/fail math; recompute verdict if needed.

    If every criterion is irrelevant → maybe + needs_human_review.
    If any relevant criterion unmet → fail (unless original was error/skipped).
    If all relevant met and original pass/maybe → keep pass when all met else maybe.
    """
    if result.verdict in ("skipped", "error"):
        return result
    if not result.criteria:
        return result

    relevant = [c for c in result.criteria if c.relevant]
    if not relevant:
        return replace(
            result,
            verdict="maybe",
            confidence=result.confidence or "low",
            needs_human_review=True,
            notes=(result.notes + " All criteria marked irrelevant.").strip(),
        )

    unmet = [c for c in relevant if not c.met]
    if unmet:
        return replace(
            result,
            verdict="fail",
            needs_human_review=result.needs_human_review
            or (result.confidence == "low"),
        )

    # All relevant criteria met — promote maybe→pass if model was uncertain on noise
    verdict = result.verdict
    needs_human_review = result.needs_human_review
    if verdict == "fail":
        # Model self-labeled the failing criteria as "irrelevant" and flipped
        # the overall verdict to fail→pass on that basis alone. That
        # self-label is not independently verified (see parse_judgment_payload
        # grounding check), so a judge that mistook a topically-related but
        # wrong answer for "irrelevant" could silently launder a real failure
        # into a pass. Keep the promotion (it's usually right), but flag for
        # human review instead of trusting it blindly.
        verdict = "pass"
        needs_human_review = True
    return replace(
        result,
        verdict=verdict if verdict in ("pass", "fail", "maybe") else "pass",
        needs_human_review=needs_human_review,
    )


def relevant_only(criteria: list[CriterionScore]) -> list[CriterionScore]:
    return [c for c in criteria if c.relevant]
