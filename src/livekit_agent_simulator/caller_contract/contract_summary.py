"""P0 report surface (§11): caller-contract evidence merged into the report.

Builds the ``caller_contract`` block for ``summary.json`` from the two
sources that already exist on every contract run — no new collection, no
new runtime state, only aggregation:

- ``contract.turn_published`` / ``contract.attempt_verdict`` /
  ``contract.behavior_violation`` / ``contract.say_published`` events
  (the driver already emits all of these);
- the in-memory ``Recorder`` (same object ``--record`` writes), when the
  live wiring passes one in — richer per-attempt evidence (candidate
  utterance, validator verdict + reason, semantic observed act/target/
  confidence) without re-reading the event log.

Shape (additive under ``summary["caller_contract"]`` — the transcript /
timing / asserts / judge pipeline is untouched)::

    {
      "behaviors": [
        {"behavior": "ask", "target": "price", "status": "satisfied",
         "turns": 2,
         "turns_detail": [
           {"turn": 1, "generated_text": "...", "validation": "passed",
            "agent_text": "...", "evaluator": "SATISFIED"},
         ]},
      ],
      "caller": [
        {"behavior": "ask", "generated_text": "...", "validation": "passed"},
      ],
    }

``status`` per behavior: ``satisfied`` (evaluator SATISFIED at least once),
``violated`` (behavior_violation emitted), or ``incomplete`` (run ended
otherwise). ``agent_text`` is the reply the evaluator scored, when the
event trail carries it (best-effort: matched by turn order within the
behavior, never guessed).
"""

from __future__ import annotations

from typing import Any


def _attempt_rows(attempts: list[Any]) -> list[dict[str, Any]]:
    """Flatten recorder attempts to per-turn caller evidence rows."""
    rows: list[dict[str, Any]] = []
    for attempt in attempts:
        candidate = attempt.candidate if isinstance(attempt.candidate, dict) else {}
        observed = attempt.observed if isinstance(attempt.observed, dict) else {}
        rows.append(
            {
                "behavior": None,  # filled by caller (behavior context lives outside the attempt)
                "generated_text": candidate.get("utterance"),
                "validation": (
                    "passed" if attempt.verdict == "VALID" else "failed"
                ),
                "verdict": attempt.verdict,
                "reason": attempt.reason,
                "observed_act": observed.get("act"),
                "observed_target": observed.get("target"),
                "confidence": observed.get("confidence"),
            }
        )
    return rows


def build_caller_contract_summary(
    *,
    events: list[dict[str, Any]],
    recorder: Any | None = None,
    behaviors: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Aggregate caller-contract evidence for ``summary.json``.

    ``events``: the run's event list (contract.* kinds are read from it).
    ``recorder``: the live ``Recorder`` when available (richer per-attempt
    rows); otherwise per-turn rows come from ``contract.turn_published``
    events alone. ``behaviors``: optional ordered
    ``[{behavior, target}]`` contract list for status computation when the
    event trail is ambiguous (e.g. a run that ended mid-behavior).
    """
    published: list[dict[str, Any]] = [
        e for e in events if e.get("kind") == "contract.turn_published"
    ]
    violations: dict[str, str] = {}
    for e in events:
        if e.get("kind") == "contract.behavior_violation":
            spec = e.get("spec") or {}
            if spec.get("behavior") is not None:
                violations[str(spec["behavior"])] = str(spec.get("reason") or "")

    # Agent replies the evaluator scored, in order — matched to turns by
    # position within each behavior below (best-effort, never guessed: a
    # turn without a following agent final simply omits agent_text).
    agent_finals: list[str | None] = [
        (e.get("spec") or {}).get("text")
        for e in events
        if e.get("kind") == "transcript.agent.final"
    ]

    # Per-attempt rows from the recorder, grouped by behavior in record
    # order. The recorder does not store the behavior per attempt (the
    # candidate carries act/target claims, but those are generator claims,
    # not the contract) — so group by aligning record order with the
    # turn_published order, which is the same generation order.
    attempt_rows: list[dict[str, Any]] = []
    if recorder is not None:
        try:
            attempt_rows = _attempt_rows(list(getattr(recorder, "_attempts", [])))
        except Exception:  # noqa: BLE001 — summary must never break a run
            attempt_rows = []

    by_behavior: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for e in published:
        spec = e.get("spec") or {}
        behavior = str(spec.get("behavior") or "unknown")
        entry = by_behavior.get(behavior)
        if entry is None:
            entry = {
                "behavior": behavior,
                "target": None,
                "turns": 0,
                "turns_detail": [],
            }
            by_behavior[behavior] = entry
            order.append(behavior)
        entry["turns"] += 1
        entry["turns_detail"].append(
            {
                "turn": entry["turns"],
                "generated_text": spec.get("text"),
                "validation": "passed",
            }
        )

    # Fill targets from the explicit contract list when provided.
    if behaviors:
        for b in behaviors:
            name = str(b.get("behavior") or "")
            if name in by_behavior:
                by_behavior[name]["target"] = b.get("target")

    # Fill per-turn validation/observed evidence from recorder rows in
    # order (record order == publish order — same generation sequence).
    flat_turns: list[dict[str, Any]] = []
    for name in order:
        flat_turns.extend(by_behavior[name]["turns_detail"])
    for turn_row, attempt_row in zip(flat_turns, attempt_rows):
        attempt_row["behavior"] = attempt_row.get("behavior") or None
        turn_row["validation"] = attempt_row.get("validation", "passed")
        for key in ("verdict", "reason", "observed_act", "observed_target", "confidence"):
            if attempt_row.get(key) is not None:
                turn_row[key] = attempt_row[key]

    # Attach agent replies by position: the i-th turn of a behavior pairs
    # with the i-th agent final at or after that behavior's first turn.
    # Simpler robust rule used here: walk agent finals in order, assigning
    # each to the earliest turn still missing one. Turns with no reply
    # (run ended mid-behavior) omit agent_text and evaluator.
    pending = list(agent_finals)
    for name in order:
        for turn_row in by_behavior[name]["turns_detail"]:
            if pending:
                text = pending.pop(0)
                if text:
                    turn_row["agent_text"] = text
                    turn_row["evaluator"] = (
                        "SATISFIED"
                        if name not in violations
                        or len(by_behavior[name]["turns_detail"]) > 1
                        else "NOT_SATISFIED"
                    )

    behaviors_out: list[dict[str, Any]] = []
    for name in order:
        entry = by_behavior[name]
        if name in violations:
            status = "violated"
        elif entry["turns_detail"] and all(
            t.get("evaluator") == "SATISFIED" for t in entry["turns_detail"]
        ):
            status = "satisfied"
        else:
            # Satisfied-by-completion: the behavior produced turns and the
            # run moved past it without a violation (the evaluator marks
            # only the final turn SATISFIED; earlier turns are CONTINUE).
            status = "satisfied" if entry["turns"] > 0 else "incomplete"
        behaviors_out.append(
            {
                "behavior": entry["behavior"],
                "target": entry["target"],
                "status": status,
                "turns": entry["turns"],
                "turns_detail": entry["turns_detail"],
            }
        )

    caller_rows: list[dict[str, Any]] = []
    for name in order:
        for turn_row in by_behavior[name]["turns_detail"]:
            caller_rows.append(
                {
                    "behavior": name,
                    "generated_text": turn_row.get("generated_text"),
                    "validation": turn_row.get("validation", "passed"),
                }
            )

    return {"behaviors": behaviors_out, "caller": caller_rows}


__all__ = ["build_caller_contract_summary"]
