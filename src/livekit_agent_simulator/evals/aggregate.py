"""Multi-judge aggregate — LiveKit JudgeGroup-shaped (all|majority|any)."""

from __future__ import annotations

from typing import Any

from .types import JudgmentResult


def verdict_points(verdict: str) -> float:
    v = (verdict or "").lower()
    if v == "pass":
        return 1.0
    if v == "maybe":
        return 0.5
    if v == "fail":
        return 0.0
    return 0.0


def aggregate_judges(
    results: list[JudgmentResult | dict[str, Any]],
    mode: str = "all",
) -> dict[str, Any]:
    """Aggregate per-judge results. ``all`` requires every verdict == pass (maybe ≠ pass)."""
    if not results:
        return {"verdict": "skipped", "notes": "No judges."}

    normalized: list[dict[str, Any]] = []
    for r in results:
        if isinstance(r, JudgmentResult):
            normalized.append(r.to_dict())
        else:
            normalized.append(dict(r))

    passes = [r for r in normalized if str(r.get("verdict") or "").lower() == "pass"]
    fails = [r for r in normalized if str(r.get("verdict") or "").lower() == "fail"]
    maybes = [r for r in normalized if str(r.get("verdict") or "").lower() == "maybe"]
    errors = [r for r in normalized if str(r.get("verdict") or "").lower() == "error"]
    n = len(normalized)
    mode_l = (mode or "all").lower()

    # All groups errored / unavailable → soft error aggregate (do not fake a fail)
    if errors and not passes and not fails and not maybes:
        notes = "; ".join(
            str(r.get("notes") or "error") for r in errors
        )
        return {
            "verdict": "error",
            "score": None,
            "mode": mode_l,
            "judges": normalized,
            "passed_count": 0,
            "failed_count": 0,
            "maybe_count": 0,
            "error_count": len(errors),
            "needs_human_review": True,
            "notes": f"multi-judge errors: {notes}"[:500],
        }

    if mode_l == "any":
        ok = len(passes) >= 1
        soft = (not ok) and len(maybes) >= 1 and len(fails) == 0
    elif mode_l == "majority":
        ok = len(passes) > n / 2
        soft = (not ok) and (len(passes) + 0.5 * len(maybes)) > n / 2
    else:  # all
        # Errors are not treated as fail for scoring — require all remaining non-error pass
        scored = [r for r in normalized if str(r.get("verdict") or "").lower() not in ("error", "skipped")]
        if not scored:
            return {
                "verdict": "error",
                "score": None,
                "mode": mode_l,
                "judges": normalized,
                "passed_count": 0,
                "failed_count": 0,
                "maybe_count": 0,
                "error_count": len(errors),
                "needs_human_review": True,
                "notes": "multi-judge: no scorable groups (errors/skips only)",
            }
        ok = all(str(r.get("verdict") or "").lower() == "pass" for r in scored)
        soft = (not ok) and all(
            str(r.get("verdict") or "").lower() in ("pass", "maybe") for r in scored
        )
        n = len(scored)
        passes = [r for r in scored if str(r.get("verdict") or "").lower() == "pass"]
        fails = [r for r in scored if str(r.get("verdict") or "").lower() == "fail"]
        maybes = [r for r in scored if str(r.get("verdict") or "").lower() == "maybe"]

    if ok:
        verdict = "pass"
    elif soft:
        verdict = "maybe"
    else:
        verdict = "fail"

    scores: list[float] = []
    for r in normalized:
        try:
            if r.get("score") is not None:
                scores.append(float(r["score"]))
        except (TypeError, ValueError):
            pass
    # LiveKit-style blended score when numeric scores missing
    if scores:
        avg: float | None = sum(scores) / len(scores)
    else:
        avg = sum(verdict_points(str(r.get("verdict"))) for r in normalized) / n * 100.0

    needs_review = any(bool(r.get("needs_human_review")) for r in normalized) or verdict == "maybe"

    return {
        "verdict": verdict,
        "score": avg,
        "mode": mode_l,
        "judges": normalized,
        "passed_count": len(passes),
        "failed_count": len(fails),
        "maybe_count": len(maybes),
        "needs_human_review": needs_review,
        "notes": f"multi-judge mode={mode_l}: {len(passes)}/{n} passed",
    }
