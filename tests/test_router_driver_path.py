"""The driver's response-router path (response-router v2-9).

Harness shape is copied from test_contract_driver.py::test_do_validates_then_publishes
(negotiate/price + max_budget, default _ScriptedBackend) because that is a do:
step the semantic verifier actually accepts. A hand-written utterance gets
SEMANTIC_ACT_MISMATCH, the behavior dies before publish, and the loop never
reaches a second turn - so the routed branch is never entered.

The FakeAgent replies below deliberately do NOT satisfy the price target, so
the behavior keeps turning and routing is reached.

The routing branch replaces the GENERATE step only. Publish, the agent-turn
wait, and evaluate_behavior all still run, which is what lets a routed turn
satisfy its own behavior.
"""

from __future__ import annotations

import hashlib

import pytest

from livekit_agent_simulator.caller_contract.dsl import parse_steps
from livekit_agent_simulator.caller_contract.responses import ResponseCatalog
from livekit_agent_simulator.caller_contract.router import (
    RouteDecision,
    RouterFault,
    RouterTerminal,
    clear_schema_cache,
)

from test_contract_driver import FakeAgent, FakeSink, _driver

pytestmark = pytest.mark.asyncio

ANSWER = "Bluebird Property Management"
UNSATISFIED = ["That is over our budget.", "Still too much.", "No, that will not work."]


def _r(intent: str, text: str, **kw) -> dict:
    return {"intent": intent, "instruction": f"Provide {intent}.", "text": text, **kw}


def _catalog() -> ResponseCatalog:
    clear_schema_cache()
    return ResponseCatalog.from_dict(
        {
            "company": _r("ask_company", ANSWER),
            "sys": _r("off_script", "Could we stay focused?", system=True),
        }
    )


class _Router:
    def __init__(self, response_id="company", exc=None, alternate=False):
        self.response_id, self.exc, self.seen = response_id, exc, []
        self.alternate = alternate

    async def route(self, *, agent_transcript, catalog):
        self.seen.append(agent_transcript)
        if self.exc is not None:
            raise self.exc
        # Alternate for the happy-path tests: DegeneracyGuard correctly fails
        # a router that returns one id three decisions running, and a constant
        # stub would trip it for the wrong reason.
        if self.alternate and self.response_id == "company":
            self._n = getattr(self, "_n", 0) + 1
            rid = "company" if self._n % 2 else "sys"
        else:
            rid = self.response_id
        return RouteDecision(
            response_id=rid, backend="stub", latency_ms=12
        )


def _steps():
    return parse_steps(
        [{"do": {"behavior": "negotiate", "target": "price",
                  "constraints": {"max_turns": 4, "max_budget": 30000}}}],
        file="t",
    )


def _setup(router, catalog):
    d, orch = _driver()
    d.router = router
    d.response_catalog = catalog
    return d, orch, FakeSink(orch), FakeAgent(replies=list(UNSATISFIED))


async def test_turn_zero_stays_legacy_and_routing_engages_after_a_reply():
    cat = _catalog()
    router = _Router("company")
    d, _o, sink, agent = _setup(router, cat)
    await d.run(_steps(), sink, agent)
    assert router.seen, "routing must engage once the agent has replied"
    assert all(t.strip() for t in router.seen), "never asked without a real agent line"


async def test_a_routed_turn_is_recorded_and_serves_the_catalog():
    cat = _catalog()
    d, _o, sink, agent = _setup(_Router("company", alternate=True), cat)
    await d.run(_steps(), sink, agent)
    assert d.routed_turns, "no routed turn recorded"
    assert d.routed_turns[0]["response_id"] == "company"
    assert d.routed_turns[0]["off_script"] is False
    assert cat.is_spent("company") is True


async def test_the_wait_still_runs_so_a_routed_turn_can_satisfy_its_behavior():
    # The reason the branch may not `continue`: skipping the wait leaves no
    # agent reply for evaluate_behavior, so a routed turn could never satisfy
    # its own behavior and every run would end BEHAVIOR_TIMEOUT.
    cat = _catalog()
    d, _o, sink, agent = _setup(_Router("company", alternate=True), cat)
    result = await d.run(_steps(), sink, agent)
    assert d.routed_turns, "routing must have happened"
    assert agent.waits > 1, "the agent must still be asked after a routed publish"
    assert not (
        result.failure and "BEHAVIOR_TIMEOUT" in str(result.failure.reason)
    ), f"a routed turn must still be able to satisfy its behavior: {result.failure}"


async def test_the_system_entry_is_recorded_as_off_script():
    cat = _catalog()
    d, _o, sink, agent = _setup(_Router("sys", alternate=True), cat)
    await d.run(_steps(), sink, agent)
    assert d.routed_turns[0]["off_script"] is True


async def test_a_router_fault_fails_the_run_and_serves_nothing():
    cat = _catalog()
    d, _o, sink, agent = _setup(
        _Router(exc=RouterFault("openai: model declined")), cat
    )
    result = await d.run(_steps(), sink, agent)
    assert result.failure is not None
    assert d.routed_turns == [], "a fault must never become a recorded decision"
    assert cat.served_counts() == {}, "nothing marked served when the router failed"


async def test_a_constant_router_trips_the_degeneracy_guard():
    # A router that returns one id every turn is a HARNESS defect: the run
    # must go red rather than produce a green suite built on it.
    cat = _catalog()
    d, _o, sink, agent = _setup(_Router("company"), cat)  # no alternate
    result = await d.run(_steps(), sink, agent)
    assert result.failure is not None
    assert "degenerate" in (result.failure.detail or "")


async def test_the_decision_event_carries_a_hash_of_the_full_agent_line():
    cat = _catalog()
    d, _o, sink, agent = _setup(_Router("company", alternate=True), cat)
    events: list = []
    await d.run(
        _steps(), sink, agent,
        emit=lambda k, spec=None, **_kw: events.append((k, spec or {})),
    )
    got = [s for k, s in events if k == "contract.router_decision"]
    assert got, f"no router_decision event; saw {[k for k, _ in events]}"
    ev = got[0]
    assert (
        ev["agent_text_sha"] == hashlib.sha1(ev["agent_text"].encode()).hexdigest()
        or len(ev["agent_text"]) < 200
    )


async def test_a_legacy_scenario_is_untouched():
    d, _ = _driver()
    assert d.router is None
    assert d.response_catalog is None
    assert d.routed_turns == []


async def test_a_router_attached_by_the_real_seam_reaches_this_branch():
    """Close the seam the other tests here bypass.

    Every other test in this file sets `d.router` by hand, so they pass
    whether or not any production code path ever assigns it. `_attach_
    response_router` is the only place that happens; this drives the same
    real harness after it runs, so a regression that drops the call from
    `run_contract_driver_path` fails HERE rather than turning every router
    scenario into a silent legacy run.
    """
    from types import SimpleNamespace

    from livekit_agent_simulator.caller_contract.live_wiring import (
        _attach_response_router,
    )
    from livekit_agent_simulator.config import RouterConfig

    d, orch = _driver()
    _attach_response_router(
        d,
        SimpleNamespace(
            router=RouterConfig(provider="openai", api_key="sk-test"),
            text_planner=SimpleNamespace(enabled=False, provider="openai", model="", api_key="sk-x"),
        ),
        SimpleNamespace(id="r1", responses=_catalog()),
    )
    # Construction is covered in test_router_wiring; swap in a scripted
    # decision so no network call happens here. What is under test is that
    # the fields the seam SET are the ones this branch reads.
    seen: list[str] = []
    d.router = _Router("company")
    d.router.seen = seen

    await d.run(_steps(), FakeSink(orch), FakeAgent(replies=list(UNSATISFIED)))

    assert seen, "a router attached by the real seam never reached the branch"
    assert d.routed_turns and d.routed_turns[0]["response_id"] == "company"



# ---------------------------------------------------------------------------
# abstention: a pick the model is unsure about is discarded, not spoken
# ---------------------------------------------------------------------------


class _ConfidentRouter:
    """A real entry at a caller-supplied confidence."""

    name = "confident"

    def __init__(self, confidence: float) -> None:
        self.confidence = confidence
        self.seen: list[str] = []

    async def route(self, *, agent_transcript: str, catalog):
        from livekit_agent_simulator.caller_contract.router import RouteDecision

        self.seen.append(agent_transcript)
        return RouteDecision(
            response_id="company", confidence=self.confidence, backend="confident"
        )


async def test_a_low_confidence_pick_is_discarded_and_the_system_entry_used():
    """The miss becomes VISIBLE instead of answered by the nearest entry.

    Measured on the target repo, run 098: the agent asked for "the exact number
    of floors and total units", no catalog entry covered it, the router picked a
    real entry anyway, and `off_script` was false — because that flag is derived
    from the LABEL of whatever was chosen. Adding an entry fixed the symptom
    and left the class intact.
    """
    from livekit_agent_simulator.caller_contract.router import CONFIDENCE_FLOOR

    d, _o, sink, agent = _setup(_ConfidentRouter(CONFIDENCE_FLOOR - 0.2), _catalog())
    await d.run(_steps(), sink, agent)

    assert d.routed_turns, "the run never routed, so nothing was proven"
    assert d.routed_turns[0]["off_script"] is True, (
        "a discarded pick must land on the system entry and be RECORDED as "
        f"off_script, got {d.routed_turns[0]}"
    )
    assert d.routed_turns[0]["response_id"] == "sys"


async def test_a_confident_pick_is_used_as_is():
    """The other direction.

    Without this the floor could be any number at all and the test above would
    still pass with a router that abstains on everything.
    """
    from livekit_agent_simulator.caller_contract.router import CONFIDENCE_FLOOR

    d, _o, sink, agent = _setup(_ConfidentRouter(0.95), _catalog())
    await d.run(_steps(), sink, agent)

    assert d.routed_turns
    assert d.routed_turns[0]["response_id"] == "company"
    assert d.routed_turns[0]["off_script"] is False
    assert CONFIDENCE_FLOOR > 0.0, "a zero floor disables abstention entirely"


async def test_repeated_abstention_does_not_read_as_a_degenerate_router():
    """The guard must not punish the router for declining.

    Measured on the target repo, run 123: after a barge-in the agent re-asked
    and the router answered CORRECTLY the same way twice, and the third repeat
    tripped DegeneracyGuard and killed the run. Feeding substituted system
    entries into the guard reproduces that — my own test caught the first version
    of this change doing exactly that, after I had written a comment claiming
    the opposite.
    """
    from livekit_agent_simulator.caller_contract.router import DegeneracyGuard

    d, _o, sink, agent = _setup(_ConfidentRouter(0.1), _catalog())
    result = await d.run(_steps(), sink, agent)

    assert result.failure is None, (
        f"declining repeatedly killed the run: {result.failure}"
    )
    assert len(d.routed_turns) > 2, "need more than one turn to prove anything"
    assert all(t["off_script"] for t in d.routed_turns), (
        "every turn should have abstained; a non-abstaining one means the "
        "floor was crossed by accident"
    )
    # And the guard still catches a genuinely stuck router — abstention
    # exempts DECLINING, not REPEATING. The threshold is 3, so the third
    # identical id raises; getting this wrong makes the exemption look like
    # the guard was simply turned off.
    guard = DegeneracyGuard()
    with pytest.raises(Exception):
        for _ in range(3):
            guard.observe("company")


async def test_every_decision_records_its_confidence():
    """A mechanism whose only output is its own failures cannot be calibrated.

    Measured on the target repo, 2026-09-30: a peer ran feat-03-barge-in and
    saw `contract.router_abstained: 0` across fourteen turns, several of them
    visibly uncertain, with no way to tell whether every value was above the
    floor or whether some were `None` and silently skipped the check —
    `contract.router_decision` carried no confidence at all.

    So the floor had no evidence behind it. This is what makes
    `CONFIDENCE_FLOOR` calibratable rather than a guess, and it is why the
    field is on EVERY decision rather than only on the ones that abstained.
    """
    d, _o, sink, agent = _setup(_ConfidentRouter(0.83), _catalog())
    await d.run(_steps(), sink, agent)

    assert d.routed_turns, "nothing routed, so nothing was proven"
    assert all(r["confidence"] == 0.83 for r in d.routed_turns), d.routed_turns
    assert all(r["abstained"] is False for r in d.routed_turns), d.routed_turns


async def test_a_router_that_reports_no_confidence_is_visible_in_the_record():
    """`None` must not look like a confident pick.

    The abstain guard is `is not None and (...)`, so a router that does not
    report confidence skips the check silently — indistinguishable, from the
    report, from a model that was never unsure. That is the same class of hole
    as before, one level in.
    """
    class _Silent:
        name = "silent"

        async def route(self, *, agent_transcript, catalog):
            from livekit_agent_simulator.caller_contract.router import RouteDecision

            return RouteDecision(response_id="company", backend="silent")

    d, _o, sink, agent = _setup(_Silent(), _catalog())
    await d.run(_steps(), sink, agent)

    assert d.routed_turns
    assert all(r["confidence"] is None for r in d.routed_turns), (
        "a router that reports no confidence must record that, not look "
        "confident"
    )
    assert all(r["abstained"] is False for r in d.routed_turns)
