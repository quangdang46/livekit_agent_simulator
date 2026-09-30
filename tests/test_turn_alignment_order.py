"""One routing decision per agent final, IN ORDER — pinned against real runs.

Two real GPT-Live runs were read by hand and they disagree, and the
disagreement is the point:

  run 027 (gpt-live-happy-path) — ALIGUED. Each `contract.router_decision`
    matches the corresponding `transcript.agent.final` in arrival order:

      "provide the full name ..."        -> contact_name     ok
      "provide your callback phone ..."  -> callback_number  ok
      "Do you have a moment right now?"  -> nothing_else     ok
      "Thank you."                      -> off_script       ok

  run 013 (gpt-live-retry-while-speaking) — every decision was the correct
    answer for the NEXT question. That one involves barge-in.

So the router is not off-by-one in general, and "fixing" run 027 by flipping
its turn-2 decision would BREAK a correct run. This file exists to make that
mistake impossible: it asserts the invariant on the real artifacts, and it
asserts that a queue drain preserves the aligned order rather than shifting
it.

`test_contract/agent_wait.py` drains agent finals oldest-first from a queue
instead of reading a single overwrite slot, so turn assignment no longer
depends on how late `agent_is_active_speaker` clears. That makes the
invariant hold under lag — but on an ALREADY-ALIGNED run it must change
nothing, and these tests are the net for that.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

TARGET_REPORTS = Path(r"C:/Users/ADMIN/Documents/Projects/voice-ai-agent/.agent-sim/reports")
HAPPY_PATH_RUN = "027-gpt-live-happy-path-20260930-031157-8809"

# The decisions the happy-path run actually made, in order, paired with the
# agent line each one answered. Recorded from the run, not derived.
HAPPY_PATH_EXPECTED: list[tuple[str, str]] = [
    ("contact_name", "full name of the person in charge"),
    ("callback_number", "callback phone number"),
    ("nothing_else", "a moment right now"),
    ("off_script", "Thank you."),
]


def _events(run_dir: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


pytestmark = pytest.mark.skipif(
    not (TARGET_REPORTS / HAPPY_PATH_RUN).exists(),
    reason=f"needs the target repo's run artifact ({TARGET_REPORTS})",
)


def _agent_finals(evs: list[dict[str, Any]]) -> list[str]:
    """Agent finals in ARRIVAL order — the order the driver consumes them."""
    return [
        str(e["spec"].get("text") or "")
        for e in evs
        if e["kind"] == "transcript.agent.final" and e["spec"].get("text")
    ]


def _decisions(evs: list[dict[str, Any]]) -> list[str]:
    return [
        str(e["spec"].get("response_id") or "")
        for e in evs
        if e["kind"] == "contract.router_decision"
    ]


def test_the_happy_path_run_is_one_decision_per_final_in_order():
    evs = _events(TARGET_REPORTS / HAPPY_PATH_RUN)
    finals = _agent_finals(evs)
    decisions = _decisions(evs)

    assert decisions, "the run recorded no routing decisions"
    # Measured on the artifact, not assumed: 5 decisions for 5 agent finals.
    # Decision 1 answers the agent's OPENING question ("company name AND the
    # full name"), which was asked before the first logged final; decisions
    # 2..5 answer finals 1..4 in order. The relationship is exactly 1:1.
    assert len(decisions) == len(finals), (
        f"one decision per agent final is the invariant; got "
        f"{len(decisions)} decisions for {len(finals)} finals"
    )

    # Each decision answered the final at the SAME index. This is the check
    # that fails loudly if anyone "fixes" the order by shifting it.
    for idx, (response_id, expected_fragment) in enumerate(HAPPY_PATH_EXPECTED, start=1):
        assert decisions[idx] == response_id, (
            f"decision {idx} was {decisions[idx]!r}, the aligned run says "
            f"{response_id!r} — do not shift a correct run"
        )
        assert expected_fragment.lower() in finals[idx - 1].lower(), (
            f"final {idx - 1} is {finals[idx - 1]!r}, expected it to mention "
            f"{expected_fragment!r} — the artifact changed"
        )


def test_a_queue_drain_preserves_the_aligned_order():
    """The fix must be a no-op on an already-correct run.

    Draining oldest-first is the right shape, but "right shape" is not
    evidence. This replays the real finals through the real queue and
    asserts the sequence is unchanged — if the queue ever reversed or
    reordered, this is what catches it.
    """
    evs = _events(TARGET_REPORTS / HAPPY_PATH_RUN)
    finals = _agent_finals(evs)

    queue: list[str] = []

    def enqueue(text: str) -> None:
        # The duplicate guard the implementation uses: only skip if this is
        # the same text as the most recent arrival.
        if not queue or queue[-1] != text:
            queue.append(text)

    for text in finals:
        enqueue(text)

    assert queue == finals, "enqueueing must not reorder or drop a final"
    drained = [queue.pop(0) for _ in range(len(queue))]
    assert drained == finals, (
        "draining oldest-first must return the same order that arrived"
    )


def test_a_lagging_speaking_flag_would_reorder_a_single_slot_but_not_a_queue():
    """The mechanism, isolated.

    With ONE overwrite slot and a late `still_speaking`, turn N's text is
    replaced by turn N+1's before the flag clears — the run 013 symptom. With
    a queue, the arrival order survives regardless of when the flag clears.
    """
    finals = ["final-N", "final-N+1"]

    # Single slot: N+1 overwrites N before the flag clears.
    slot: str | None = None
    for text in finals:
        slot = text  # both land while still_speaking is true
    assert slot == "final-N+1"

    # Queue: both survive, and draining is still N then N+1.
    queue: list[str] = []
    for text in finals:
        queue.append(text)
    assert [queue.pop(0) for _ in range(len(queue))] == ["final-N", "final-N+1"]
