"""Judgment result types — LiveKit/Hamming-shaped, no I/O."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Verdict = Literal["pass", "fail", "maybe", "skipped", "error"]
Confidence = Literal["low", "medium", "high"]


@dataclass
class CriterionScore:
    criterion: str
    met: bool
    evidence: str = ""
    relevant: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ConversationFeedback:
    issue: str = ""
    severity: str = "low"
    agent_line: str = ""
    why: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ReviewIssue:
    """A finding in the free-style human review.

    Framework-agnostic: the LLM fills ``title``/``severity``/``evidence``/
    ``impact``/``recommendation`` from the generic rubric.
    """

    title: str = ""
    severity: str = "Minor"
    evidence: str = ""
    impact: str = ""
    recommendation: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class JudgmentResult:
    verdict: Verdict
    score: float | None = None
    criteria: list[CriterionScore] = field(default_factory=list)
    confidence: Confidence | None = None
    needs_human_review: bool = False
    critical_failure: bool = False
    notes: str = ""
    judge_id: str | None = None
    conversation_feedback: list[ConversationFeedback] = field(default_factory=list)
    # Free-style human review (generic rubric — not framework-specific)
    overall_summary: str = ""
    strengths: list[str] = field(default_factory=list)
    issues: list[ReviewIssue] = field(default_factory=list)
    missing_checks: list[str] = field(default_factory=list)
    language_naturalness: list[str] = field(default_factory=list)
    final_assessment: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "verdict": self.verdict,
            "score": self.score,
            "criteria": [c.to_dict() for c in self.criteria],
            "notes": self.notes,
        }
        if self.conversation_feedback:
            d["conversation_feedback"] = [
                f.to_dict() for f in self.conversation_feedback
            ]
        if self.overall_summary:
            d["overall_summary"] = self.overall_summary
        if self.strengths:
            d["strengths"] = list(self.strengths)
        if self.issues:
            d["issues"] = [i.to_dict() for i in self.issues]
        if self.missing_checks:
            d["missing_checks"] = list(self.missing_checks)
        if self.language_naturalness:
            d["language_naturalness"] = list(self.language_naturalness)
        if self.final_assessment:
            d["final_assessment"] = dict(self.final_assessment)
        if self.confidence is not None:
            d["confidence"] = self.confidence
        if self.needs_human_review:
            d["needs_human_review"] = True
        if self.critical_failure:
            d["critical_failure"] = True
        if self.judge_id is not None:
            d["judge_id"] = self.judge_id
        return d


def parse_judgment_payload(raw: dict[str, Any]) -> JudgmentResult:
    """Normalize LLM JSON into JudgmentResult (tolerant of partial payloads)."""
    verdict_raw = str(raw.get("verdict") or "error").strip().lower()
    if verdict_raw not in ("pass", "fail", "maybe", "skipped", "error"):
        verdict_raw = "error"

    criteria: list[CriterionScore] = []
    ungrounded_criteria = False
    for item in raw.get("criteria") or []:
        if not isinstance(item, dict):
            continue
        met = item.get("met")
        if met is None:
            met = bool(item.get("pass"))
        met = bool(met)
        relevant = item.get("relevant")
        if relevant is None:
            relevant = True
        evidence = str(item.get("evidence") or item.get("rationale") or "").strip()
        if met and not evidence:
            # An ungrounded "met" claim — the judge didn't cite any transcript
            # evidence for it. Don't trust it blindly (this is how a
            # topically-related-but-wrong agent reply slips past as a pass);
            # flip to unmet and flag the whole judgment for human review.
            met = False
            ungrounded_criteria = True
        criteria.append(
            CriterionScore(
                criterion=str(item.get("criterion") or item.get("id") or ""),
                met=met,
                evidence=evidence,
                relevant=bool(relevant),
            )
        )

    score: float | None
    try:
        score = float(raw["score"]) if raw.get("score") is not None else None
    except (TypeError, ValueError):
        score = None

    conf_raw = raw.get("confidence")
    confidence: Confidence | None = None
    if isinstance(conf_raw, str) and conf_raw.lower() in ("low", "medium", "high"):
        confidence = conf_raw.lower()  # type: ignore[assignment]

    conversation_feedback: list[ConversationFeedback] = []
    for item in raw.get("conversation_feedback") or []:
        if not isinstance(item, dict):
            continue
        conversation_feedback.append(
            ConversationFeedback(
                issue=str(item.get("issue") or item.get("criterion") or ""),
                severity=str(item.get("severity") or "low"),
                agent_line=str(item.get("agent_line") or item.get("quote") or ""),
                why=str(item.get("why") or item.get("impact") or ""),
            )
        )

    issues: list[ReviewIssue] = []
    for item in raw.get("issues") or []:
        if not isinstance(item, dict):
            continue
        issues.append(
            ReviewIssue(
                title=str(item.get("title") or item.get("issue") or ""),
                severity=str(item.get("severity") or "Minor"),
                evidence=str(item.get("evidence") or item.get("agent_line") or ""),
                impact=str(item.get("impact") or item.get("why") or ""),
                recommendation=str(
                    item.get("recommendation")
                    or item.get("improvement")
                    or item.get("how_to_improve")
                    or ""
                ),
            )
        )

    def _str_list(key: str) -> list[str]:
        out: list[str] = []
        for item in raw.get(key) or []:
            if isinstance(item, dict):
                out.append(str(item.get("point") or item.get("item") or item.get("issue") or ""))
            elif item is not None:
                out.append(str(item))
        return [s for s in out if s]

    return JudgmentResult(
        verdict=verdict_raw,  # type: ignore[arg-type]
        score=score,
        criteria=criteria,
        confidence=confidence,
        needs_human_review=bool(raw.get("needs_human_review")) or ungrounded_criteria,
        critical_failure=bool(raw.get("critical_failure")),
        notes=str(raw.get("notes") or raw.get("reasoning") or ""),
        judge_id=str(raw["judge_id"]) if raw.get("judge_id") else None,
        conversation_feedback=conversation_feedback,
        overall_summary=str(raw.get("overall_summary") or ""),
        strengths=_str_list("strengths") or _str_list("works"),
        issues=issues,
        missing_checks=_str_list("missing_checks"),
        language_naturalness=_str_list("language_naturalness"),
        final_assessment=dict(raw.get("final_assessment") or {}),
    )
