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
    if verdict == "fail":
        # Model said fail but all relevant met (irrelevant fails) → pass
        verdict = "pass"
    return replace(
        result,
        verdict=verdict if verdict in ("pass", "fail", "maybe") else "pass",
    )


def relevant_only(criteria: list[CriterionScore]) -> list[CriterionScore]:
    return [c for c in criteria if c.relevant]
