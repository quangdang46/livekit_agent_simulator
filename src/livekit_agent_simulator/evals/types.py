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
class JudgmentResult:
    verdict: Verdict
    score: float | None = None
    criteria: list[CriterionScore] = field(default_factory=list)
    confidence: Confidence | None = None
    needs_human_review: bool = False
    critical_failure: bool = False
    notes: str = ""
    judge_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "verdict": self.verdict,
            "score": self.score,
            "criteria": [c.to_dict() for c in self.criteria],
            "notes": self.notes,
        }
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
    for item in raw.get("criteria") or []:
        if not isinstance(item, dict):
            continue
        met = item.get("met")
        if met is None:
            met = bool(item.get("pass"))
        relevant = item.get("relevant")
        if relevant is None:
            relevant = True
        criteria.append(
            CriterionScore(
                criterion=str(item.get("criterion") or item.get("id") or ""),
                met=bool(met),
                evidence=str(item.get("evidence") or item.get("rationale") or ""),
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

    return JudgmentResult(
        verdict=verdict_raw,  # type: ignore[arg-type]
        score=score,
        criteria=criteria,
        confidence=confidence,
        needs_human_review=bool(raw.get("needs_human_review")),
        critical_failure=bool(raw.get("critical_failure")),
        notes=str(raw.get("notes") or raw.get("reasoning") or ""),
        judge_id=str(raw["judge_id"]) if raw.get("judge_id") else None,
    )
