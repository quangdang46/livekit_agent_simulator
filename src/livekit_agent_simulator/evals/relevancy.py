"""Relevancy gate — Hamming step A (post-parse; exclude irrelevant criteria)."""

from __future__ import annotations

from .types import CriterionScore, JudgmentResult


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
        return JudgmentResult(
            verdict="maybe",
            score=result.score,
            criteria=result.criteria,
            confidence=result.confidence or "low",
            needs_human_review=True,
            critical_failure=result.critical_failure,
            notes=(result.notes + " All criteria marked irrelevant.").strip(),
            judge_id=result.judge_id,
        )

    unmet = [c for c in relevant if not c.met]
    if unmet:
        return JudgmentResult(
            verdict="fail",
            score=result.score,
            criteria=result.criteria,
            confidence=result.confidence,
            needs_human_review=result.needs_human_review
            or (result.confidence == "low"),
            critical_failure=result.critical_failure,
            notes=result.notes,
            judge_id=result.judge_id,
        )

    # All relevant criteria met — promote maybe→pass if model was uncertain on noise
    verdict = result.verdict
    if verdict == "fail":
        # Model said fail but all relevant met (irrelevant fails) → pass
        verdict = "pass"
    return JudgmentResult(
        verdict=verdict if verdict in ("pass", "fail", "maybe") else "pass",
        score=result.score,
        criteria=result.criteria,
        confidence=result.confidence,
        needs_human_review=result.needs_human_review,
        critical_failure=result.critical_failure,
        notes=result.notes,
        judge_id=result.judge_id,
    )


def relevant_only(criteria: list[CriterionScore]) -> list[CriterionScore]:
    return [c for c in criteria if c.relevant]
