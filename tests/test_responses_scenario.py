"""Scenario parse + export for the response catalog (response-router v2-8).

Parse and export land TOGETHER on purpose: a parse-only change would let
``lks export`` silently drop a catalog the scenarios depend on, and the
round-trip test below is what holds them to each other.
"""

from __future__ import annotations

import inspect

from livekit_agent_simulator.caller_contract.driver import ContractCallerDriver

import pytest

from livekit_agent_simulator.scenario import ScenarioError
from livekit_agent_simulator.scenario_from_dict import (
    export_scenario_dict,
    scenario_from_dict,
)

CATALOG = {
    "company": {"intent": "ask_company", "instruction": "Provide the company.", "text": "Bluebird"},
    "phone": {"intent": "ask_phone", "instruction": "Provide a number.", "text": "555", "reusable": True},
    "sys": {"intent": "off_script", "instruction": "Only when nothing else fits.", "text": "Stay focused?", "system": True},
}

MINIMAL = {
    "apiVersion": "agent-sim/v1",
    "kind": "Scenario",
    "metadata": {"id": "r1", "locale": "en-US"},
    # persona.brief is REQUIRED by the parser (scenario_from_dict.py:116).
    "persona": {"name": "Robin", "brief": "You are calling a switchboard."},
}


def _doc(**over) -> dict:
    d = {**MINIMAL, **over}
    return d


# -------------------------------------------------------------------- parse


def test_responses_parses_into_a_catalog():
    sc = scenario_from_dict(_doc(responses=CATALOG))
    assert sc.responses is not None
    assert sc.responses.system_id == "sys"
    assert sc.responses.get("company").text == "Bluebird"
    assert sc.responses.get("phone").reusable is True


def test_responses_is_optional_and_legacy_scenarios_are_untouched():
    sc = scenario_from_dict(MINIMAL)
    assert sc.responses is None, "a scenario without responses must parse exactly as before"


def test_non_mapping_responses_is_a_scenario_error():
    with pytest.raises(ScenarioError, match="responses must be a mapping"):
        scenario_from_dict(_doc(responses=["a"]))


def test_catalog_errors_surface_as_scenario_errors():
    with pytest.raises(ScenarioError, match="must author a system response"):
        scenario_from_dict(_doc(responses={"a": {"intent": "i", "instruction": "x", "text": "t"}}))


# ---------------------------------------------------- coexistence (plan D13)


def test_responses_coexists_with_caller_steps_and_loses():
    # The migration is transitional: caller_steps stays required and keeps
    # winning. A scenario may carry both while it migrates.
    doc = _doc(
        responses=CATALOG,
        caller_steps=[{"say": "legacy line", "trigger": {"kind": "time", "delay_ms": 100}}],
    )
    sc = scenario_from_dict(doc)
    assert sc.responses is not None
    assert sc.caller_actions, "caller_steps must still produce actions"
    # WHAT THIS ACTUALLY CHECKS, corrected: the catalog is DATA, it does not
    # add or remove actions. It does NOT decide precedence.
    #
    # This assertion's message used to claim "caller_steps wins; the router is
    # not engaged". That was FALSE, and the sweep for it kept finding more
    # copies than the last sweep did — it had reached seven sites (this file,
    # AGENTS.md, README.md, the `lks execute` help, `responses.py`'s module
    # docstring, the migration guide, and two Rust comments), and an earlier
    # commit claimed "all four" having checked four. A count in a commit
    # message is a claim like any other.
    # The driver's gate is only
    # `router is not None and response_catalog is not None and agent_text` —
    # there is no caller_steps check anywhere in driver.py, so with both keys
    # present the ROUTER runs. Every migrated gpt-live-* scenario carries both,
    # which is why they route.
    assert len(sc.caller_actions) == 1, (
        "the catalog is data: it must not add or remove caller actions. It "
        "does not decide precedence between the two paths."
    )


def test_the_router_runs_when_both_keys_are_present():
    """The rule as IMPLEMENTED, asserted where the router is actually chosen.

    Not `driver.py` internals — the gate is
    `self.router is not None and self.response_catalog is not None and
    agent_text`, with no reference to caller_steps. This asserts the
    observable consequence so the real rule has a test, instead of only a
    comment that says something else.
    """
    doc = _doc(
        responses=CATALOG,
        caller_steps=[{"say": "legacy line", "trigger": {"kind": "time", "delay_ms": 100}}],
    )
    sc = scenario_from_dict(doc)
    # Both survive parsing, so both reach the driver, and the driver's gate
    # picks the router. caller_steps still supplies the opening action.
    assert sc.responses is not None
    assert sc.caller_actions
    assert "caller_steps" not in inspect.getsource(
        ContractCallerDriver._run_behavior
    ), (
        "if a caller_steps check appears in the behaviour loop, precedence has "
        "changed and this test plus the docs need revisiting together"
    )


# ------------------------------------------------------------------- export


def test_round_trip_is_identity():
    sc = scenario_from_dict(_doc(responses=CATALOG))
    out = export_scenario_dict(sc)
    again = scenario_from_dict(out)
    assert again.responses is not None
    assert set(again.responses.responses) == set(sc.responses.responses)
    for rid, spec in sc.responses.responses.items():
        got = again.responses.responses[rid]
        assert (got.intent, got.instruction, got.text) == (spec.intent, spec.instruction, spec.text)
        assert (got.system, got.reusable) == (spec.system, spec.reusable)


def test_optional_flags_round_trip_without_inventing_them():
    out = export_scenario_dict(scenario_from_dict(_doc(responses=CATALOG)))
    company = out["responses"]["company"]
    assert "system" not in company, "a false flag must not be emitted"
    assert "reusable" not in company
    assert out["responses"]["phone"]["reusable"] is True
    assert out["responses"]["sys"]["system"] is True


def test_export_omits_responses_entirely_when_absent():
    # This is what keeps every existing scenario's export byte-identical.
    out = export_scenario_dict(scenario_from_dict(MINIMAL))
    assert "responses" not in out


def test_author_order_is_preserved():
    out = export_scenario_dict(scenario_from_dict(_doc(responses=CATALOG)))
    assert list(out["responses"]) == list(CATALOG)
