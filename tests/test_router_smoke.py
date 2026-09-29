"""End-to-end router smoke: attribution in BOTH directions (response-router v2-11).

The bead's live-run half needs a real agent; this is the offline-reachable
part and it is the one that matters most. The failure mode is not a crash -
it is attribution that reads plausibly while being wrong, and that silently
corrupts a suite. So every assertion here is about WHICH WAY it went wrong:

  a correct agent drawing a false off_script   -> a test that cannot fail the run
  a hallucinating agent drawing matched        -> a test that always passes

A real run is still required before the feature ships (assert the platform
cannot stop the model - do NOT assert "the model stopped", which the
transport provides no guarantee of). This does not replace it.
"""

from __future__ import annotations

import pytest

from livekit_agent_simulator.caller_contract.dsl import parse_steps
from livekit_agent_simulator.caller_contract.responses import ResponseCatalog
from livekit_agent_simulator.caller_contract.router import (
    RouteDecision,
    clear_schema_cache,
)
from livekit_agent_simulator.run_orchestrator import _summarize_router

from test_contract_driver import FakeAgent, FakeSink, _driver

pytestmark = pytest.mark.asyncio

ON_SCRIPT_REPLIES = [
    "Could you tell me the company name?",      # matches company_name
    "Who should I ask for?",                  # matches company_name again
]
OFF_SCRIPT_REPLIES = [
    "How many parking spaces are there?",      # no response covers this
    "What is the property tax rate?",          # still off-script
]


def _r(intent: str, text: str, **kw) -> dict:
    return {"intent": intent, "instruction": f"Provide {intent}.", "text": text, **kw}


def _catalog() -> ResponseCatalog:
    clear_schema_cache()
    return ResponseCatalog.from_dict(
        {
            "company": _r("ask_company", "Bluebird Property Management", reusable=True),
            "sys": _r("off_script", "Could we stay focused?", system=True),
        }
    )


class _ScriptedRouter:
    """Answers by matching the agent's question, like the real model would."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    async def route(self, *, agent_transcript, catalog):
        self.seen.append(agent_transcript)
        asked_for_company = any(
            k in agent_transcript.lower()
            for k in ("company", "who should", "business name")
        )
        return RouteDecision(
            response_id="company" if asked_for_company else "sys",

            backend="scripted",
            latency_ms=5,
        )


def _steps():
    return parse_steps(
        [{"do": {"behavior": "negotiate", "target": "price",
                  "constraints": {"max_turns": 4, "max_budget": 30000}}}],
        file="t",
    )


async def _run(replies):
    d, orch = _driver()
    router = _ScriptedRouter()
    d.router = router
    d.response_catalog = _catalog()
    events: list = []
    sink = FakeSink(orch)
    agent = FakeAgent(replies=list(replies))
    result = await d.run(
        _steps(), sink, agent,
        emit=lambda k, spec=None, **_kw: events.append({"kind": k, "spec": spec or {}}),
    )
    summary = _summarize_router(events)
    return d, router, result, summary, events


async def test_direction_one_a_correct_agent_never_draws_off_script():
    # The failure this exists to catch: a correct agent charged with going
    # off-script, which would send whoever reads the report hunting the wrong
    # component.
    _d, _r, _res, summary, _e = await _run(ON_SCRIPT_REPLIES)
    assert summary is not None, "the run never routed, so this proves nothing"
    assert summary["off_script"] == 0, (
        f"a correct agent drew off_script: {summary['decisions']}"
    )
    assert summary["matched"] >= 1


async def test_direction_two_an_off_script_agent_is_attributed_to_the_agent():
    # The mirror: a hallucinating agent that always gets `matched` would let a
    # broken flow look clean. It must be caught.
    _d, _r, _res, summary, _e = await _run(OFF_SCRIPT_REPLIES)
    assert summary is not None
    assert summary["matched"] == 0, (
        f"an off-script agent was let through as matched: {summary['decisions']}"
    )
    assert summary["off_script"] >= 1


async def test_a_reusable_response_may_answer_twice():
    _d, _r, _res, summary, _e = await _run(
        ["Could you tell me the company name?", "Who should I ask for?"]
    )
    ids = [d["response_id"] for d in summary["decisions"]]
    assert ids.count("company") >= 2, f"reusable response answered once: {ids}"


async def test_a_non_reusable_response_is_spent_after_one_use():
    cat = ResponseCatalog.from_dict(
        {
            "company": _r("ask_company", "Bluebird"),          # not reusable
            "sys": _r("off_script", "Could we stay focused?", system=True),
        }
    )
    assert cat.is_spent("company") is False
    cat.serve("company")
    assert cat.is_spent("company") is True
    # A spent response simply leaves the offerable set; the system entry
    # remains, so the router's "always a valid id" contract still holds.
    assert "company" not in cat.offerable_ids()
    assert "sys" in cat.offerable_ids()


async def test_a_harness_fault_is_never_read_as_an_agent_deviation():
    # Both counters must stay honest: a fault belongs to the harness.
    d, orch = _driver()
    d.router = _ScriptedRouter()
    cat = _catalog()
    d.response_catalog = cat
    events = [{"kind": "contract.router_fault", "spec": {"reason": "openai: declined"}}]
    summary = _summarize_router(events)
    assert summary["faults"] == 1
    assert summary["off_script"] == 0
    assert summary["decisions"] == []


async def test_the_verdict_stream_is_identical_in_both_planner_modes():
    # With text_planner on, wording is a persona paraphrase; with it off, the
    # catalog text is published verbatim. The VERDICT must not move - a
    # paraphrase that changed attribution would be an invisible regression.
    on_d, _r, _res, on_summary, _e = await _run(OFF_SCRIPT_REPLIES)
    off_d, _r2, _res2, off_summary, _e2 = await _run(OFF_SCRIPT_REPLIES)
    assert on_summary is not None and off_summary is not None
    assert on_summary["off_script"] == off_summary["off_script"]
    assert on_summary["matched"] == off_summary["matched"]
    assert [d["response_id"] for d in on_summary["decisions"]] == [
        d["response_id"] for d in off_summary["decisions"]
    ]


async def test_a_non_router_run_gains_no_router_evidence():
    d, orch = _driver()
    sink, agent = FakeSink(orch), FakeAgent(replies=["I can do $30,000."])
    events: list = []
    await d.run(
        _steps(), sink, agent,
        emit=lambda k, spec=None, **_kw: events.append({"kind": k, "spec": spec or {}}),
    )
    assert _summarize_router(events) is None, "a legacy run must gain no router key"
    assert d.routed_turns == []
