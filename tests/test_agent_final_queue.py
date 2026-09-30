"""Agent finals are queued on arrival, not read from a single overwrite slot.

`observer.last_agent_final_text` is one slot that every new final overwrites,
and `wait_agent_turn` only returns a final once `agent_is_active_speaker` is
false — a VAD-derived flag that lags the agent's real audio by ~2.4s
(PROBLEMS.md §1). So a final that lands while the agent is still flagged
speaking is held, and the next turn's final overwrites it before the flag
clears:

    1. turn N's final lands, agent still flagged speaking -> held
    2. turn N+1's final lands -> overwrites the slot
    3. the flag clears -> the wait returns turn N+1's text

On run 013 (gpt-live-retry-while-speaking, which barges in) that put the
router exactly one turn behind on every decision. Run 027 (happy path, no
barge) was unaffected and stayed aligned — pinned in
tests/test_turn_alignment_order.py against the real artifacts.

These tests drive the real `wait_agent_turn` against a fake observer that
models BOTH observer mechanisms (the overwriting slot and the arrival
queue), rather than reimplementing the logic.
"""

from __future__ import annotations

import asyncio
import time

from livekit_agent_simulator.caller_contract.agent_wait import ObserverAgentWait


class _FakeObserver:
    """Models Observer's two mechanisms: the overwriting slot, and the
    arrival-time queue that `_push_agent_final` fills.

    `stop()` clears `agent_is_active_speaker`, which is the flag that lags.
    """

    def __init__(self) -> None:
        self.last_agent_final_mono: float | None = None
        self.last_agent_final_text: str | None = None
        self.agent_is_active_speaker = True
        self.agent_has_spoken = True
        self.agent_disconnected = None
        self._queue: list[str] = []
        self._seen_segments: set[str] = set()

    def land(self, text: str, segment_id: str | None = None) -> None:
        self.last_agent_final_mono = time.monotonic()
        self.last_agent_final_text = text
        # Dedup by SEGMENT ID, never by text — mirrors the real observer.
        if segment_id is not None:
            if segment_id in self._seen_segments:
                return
            self._seen_segments.add(segment_id)
        self._queue.append(text)
        self.agent_is_active_speaker = True

    def stop(self) -> None:
        self.agent_is_active_speaker = False

    def take_agent_finals(self) -> list[str]:
        out = list(self._queue)
        self._queue.clear()
        return out


def _waiter(observer) -> ObserverAgentWait:
    # One instance across waits: `_last_returned_text` and the queue are
    # per-instance state, exactly as in production.
    #
    # snapshot_fraction=2.0 pushes the tier-2 session-snapshot probe past the
    # deadline. That is a SEPARATE evidence source which bypasses the arrival
    # queue, and these tests are about the queue; testing both at once would
    # attribute either failure to the wrong mechanism.
    return ObserverAgentWait(  # type: ignore[arg-type]
        observer=observer, poll_s=0.01, snapshot_fraction=2.0
    )


def _wait(w: ObserverAgentWait, *, timeout_s: float = 2.0) -> str | None:
    return asyncio.run(w.wait_agent_turn(timeout_s=timeout_s))


# --------------------------------------------------------------------------


def test_two_finals_landing_under_a_late_flag_come_back_in_order():
    """The run 013 mechanism, isolated.

    Both finals land while the agent is still flagged speaking, and the flag
    clears only afterwards. Reading the slot would return only the second.
    """
    obs = _FakeObserver()
    obs.land("turn-N text", "SG_N")
    obs.land("turn-N+1 text", "SG_N1")
    obs.stop()

    w = _waiter(obs)
    first = _wait(w)
    second = _wait(w)

    assert first == "turn-N text", (
        f"turn N got {first!r}; a single slot returns turn N+1's text, which is "
        "the one-turn-behind symptom"
    )
    assert second == "turn-N+1 text", f"turn N+1 got {second!r}"


def test_a_refinalised_segment_is_not_served_as_a_new_turn():
    """Run 039's invariant, now enforced on segment id rather than text."""
    obs = _FakeObserver()
    obs.land("only one", "SG_1")
    obs.stop()

    w = _waiter(obs)
    assert _wait(w) == "only one"

    # The provider re-finalises the SAME segment. No new turn exists.
    obs.agent_has_spoken = False
    obs.land("only one", "SG_1")
    obs.stop()
    obs.agent_has_spoken = False
    assert _wait(w, timeout_s=0.15) is None


def test_two_turns_that_genuinely_say_the_same_thing_both_come_back():
    """The reason dedup is not on text.

    "yes." twice, on two different segments, is two turns. Collapsing them
    would leave the driver waiting for a reply that already arrived.
    """
    obs = _FakeObserver()
    obs.land("same", "SG_1")
    obs.land("same", "SG_2")
    obs.stop()

    w = _waiter(obs)
    assert _wait(w) == "same"
    assert _wait(w) == "same", (
        "identical wording on two segments must not be collapsed — the "
        "discriminator is the segment, not the text"
    )


def test_a_single_final_is_unaffected_by_the_queue():
    """The queue must be a no-op on the ordinary path."""
    obs = _FakeObserver()
    obs.land("the only final", "SG_1")
    obs.stop()
    assert _wait(_waiter(obs)) == "the only final"


def test_an_observer_without_a_queue_still_works():
    """Older doubles and test stubs only set the two slot fields.

    The drain helper returns nothing for them, and the original slot path is
    still there, so nothing that works today can break.
    """
    class _SlotOnlyObserver:
        """What most existing test doubles look like: the two slot fields and
        nothing else."""

        def __init__(self) -> None:
            self.last_agent_final_mono: float | None = None
            self.last_agent_final_text: str | None = None
            self.agent_is_active_speaker = True
            self.agent_has_spoken = True
            self.agent_disconnected = None

    obs = _SlotOnlyObserver()
    obs.last_agent_final_mono = time.monotonic()
    obs.last_agent_final_text = "slot only"
    obs.agent_is_active_speaker = False
    assert _wait(_waiter(obs)) == "slot only"


# --------------------------------------------------------------------------
# tiers 2 and 3 are the same door: they must not bypass the queue, and they
# must record what they return.
# --------------------------------------------------------------------------


def test_the_untranscribed_marker_is_returned_once_not_every_turn():
    """Run 013 turn 3: `[untranscribed agent speech]` reached the router.

    The tier-3 floor returned that marker WITHOUT recording it, so two
    consecutive untranscribed turns each got one — the driver saw the same
    marker twice and the router routed a line it had already routed. Every
    other return path records; this one did not.
    """
    obs = _FakeObserver()
    obs.agent_has_spoken = True
    obs.agent_is_active_speaker = False

    w = _waiter(obs)
    first = _wait(w, timeout_s=0.1)
    assert first == "[untranscribed agent speech]"

    # The agent talks again but still nothing is transcribed.
    obs.agent_has_spoken = True
    second = _wait(w, timeout_s=0.1)
    assert second is None, (
        f"the marker was handed back a second time ({second!r}); two "
        "consecutive untranscribed turns must not both look like new evidence"
    )


def test_tier_2_does_not_preempt_a_queued_final():
    """A session snapshot is the NEWEST agent message.

    If it is used while an arrival-queued final is still pending, it returns
    the wrong turn — the one-turn-behind symptom through a different door.
    Tier 2 now drains the queue first.
    """
    obs = _FakeObserver()
    obs.land("queued turn-N", "SG_N")
    obs.land("queued turn-N+1", "SG_N1")
    obs.stop()

    w = _waiter(obs)
    # snapshot_fraction=0.0 makes tier 2 eligible on the first poll.
    w._snapshot_attempted = False
    w.snapshot_fraction = 0.0

    first = _wait(w, timeout_s=0.3)
    assert first == "queued turn-N", (
        f"tier 2 preempted the queue and returned {first!r}"
    )
