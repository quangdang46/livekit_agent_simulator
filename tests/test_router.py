"""Tests for the decision-router port (response-router v2-5).

The port's whole job is to return one id from the authored catalog. Most of
these tests are about the ways it could fail to: an out-of-set id, a refusal
dressed as a success, or a router that has quietly degenerated.
"""

from __future__ import annotations

import pytest

from livekit_agent_simulator.caller_contract import router as _router_module

from livekit_agent_simulator.caller_contract.responses import ResponseCatalog
from livekit_agent_simulator.caller_contract.router import (
    DEGENERATE_RUN_LENGTH,
    RESPONSE_KEY,
    DegeneracyGuard,
    RouterFault,
    RouterTerminal,
    build_route_schema,
    clear_schema_cache,
    dump_schema,
    parse_route_body,
    route_schema_for,
    should_retry,
)


def _r(intent: str, text: str = "t", **kw) -> dict:
    return {"intent": intent, "instruction": f"Provide {intent}.", "text": text, **kw}


def _catalog(**extra) -> ResponseCatalog:
    base = {"sys": {**_r("off_script", "stay focused?", system=True)}}
    base.update(extra)
    return ResponseCatalog.from_dict(base)


# ------------------------------------------------------------------- schema


def test_schema_is_wrapped_not_a_bare_enum():
    # OpenAI pins the root of a Structured Outputs schema to an object and
    # forbids anyOf at the root, so a bare top-level enum is invalid. One
    # wrapped shape serves both providers.
    s = build_route_schema(["a", "b"])
    assert s["type"] == "object"
    assert s["properties"][RESPONSE_KEY]["enum"] == ["a", "b"]
    assert s["properties"]["confidence"]["type"] == "number"
    # Both REQUIRED: an optional property is one the provider may omit,
    # which is how the earlier schema-free confidence stayed None forever.
    assert set(s["required"]) == {RESPONSE_KEY, "confidence"}
    assert s["additionalProperties"] is False


def test_required_is_not_optional():
    # OpenAI strict mode refuses to run without `required`; on Gemini its
    # absence is what makes an empty {} reachable, because every property is
    # optional by default there.
    assert "required" in build_route_schema(["a"])


def test_empty_and_duplicate_option_sets_are_rejected():
    with pytest.raises(ValueError, match="no options"):
        build_route_schema([])
    with pytest.raises(ValueError, match="duplicate"):
        build_route_schema(["a", "a"])


def test_schema_is_cached_and_rebuilt_only_when_the_option_set_changes():
    # OpenAI charges a one-time compile per schema. Recomputing per turn pays
    # that on every turn; cached, it is one compile per SPEND.
    clear_schema_cache()
    cat = _catalog(a=_r("ask_a"), b=_r("ask_b", reusable=True))
    first = route_schema_for(cat)
    assert route_schema_for(cat) is first, "stable option set must hit the cache"
    cat.serve("a")
    second = route_schema_for(cat)
    assert second is not first, "a spend changes the enum, so the schema must be rebuilt"
    assert "a" not in second["properties"][RESPONSE_KEY]["enum"]


# -------------------------------------------------------------------- parse


def test_valid_body_becomes_a_decision():
    d = parse_route_body(
        {RESPONSE_KEY: "a", "confidence": 0.9}, options=["a", "b"], backend="t"
    )
    assert d.response_id == "a"
    assert d.backend == "t"


def test_confidence_is_required_because_it_is_the_evidence_for_its_own_removal():
    """The field stayed after the mechanism it fed was removed.

    CONFIDENCE_FLOOR is gone: measured on the model in use, confidence does not
    move between a correct answer (0.900), a wrong one (0.950), and a question
    NO entry covers (0.950). A knob that cannot fire is the same failure as the
    dead `confidence` field this module carried earlier the same day.

    `confidence` STAYS required in the schema, because it is the input to that
    measurement. Without it the finding cannot be reproduced and no future
    provider can be shown to beat it. The distinction is deliberate: this is
    the record of a measured negative result, not a promise of behaviour.
    """
    schema = build_route_schema(["a", "b"])
    assert "confidence" in schema["properties"]
    assert "confidence" in schema["required"], (
        "confidence must stay REQUIRED — it is the evidence the removal rests on"
    )
    assert not hasattr(_router_module, "CONFIDENCE_FLOOR"), (
        "the floor was measured inert on this model; shipping it back is "
        "shipping a knob that provably cannot fire"
    )

    parsed = parse_route_body(
        {RESPONSE_KEY: "a", "confidence": 0.55}, options=["a", "b"], backend="t"
    )
    assert parsed.confidence == 0.55, "the field is still carried per decision"


def test_a_body_without_confidence_is_a_fault_not_a_silent_none():
    """The schema requires it, so its absence means a non-conforming provider.

    Defaulting to None would restore the exact failure this replaced: the
    driver cannot tell "the model is unsure" from "the model did not say", and
    both look like a confident pick.
    """
    with pytest.raises(RouterFault) as exc:
        parse_route_body({RESPONSE_KEY: "a"}, options=["a", "b"], backend="t")
    assert "confidence" in str(exc.value)



def test_an_extra_provider_property_is_still_ignored_not_fatal():
    """A provider that ignores `additionalProperties: False` must not crash.

    Strict mode makes it forbidden, not impossible; a non-compliant backend
    could still send it. The router reads the one key it asked for and ignores
    the rest, so a stray field can never become a silent decision input.
    """
    d = parse_route_body(
        {RESPONSE_KEY: "a", "confidence": 0.4, "why": "because"}, options=["a"], backend="t"
    )
    assert d.response_id == "a"
    assert d.backend == "t"


def test_out_of_set_id_is_a_contract_violation():
    # Never coerce, never argmax over a loose parse — that is exactly the
    # failure the enum exists to prevent.
    with pytest.raises(RouterFault, match="not one of the offered options"):
        parse_route_body({RESPONSE_KEY: "zzz"}, options=["a", "b"], backend="t")


def test_missing_key_is_a_fault_not_a_silent_default():
    # This is how a refusal and a truncated body both present themselves.
    with pytest.raises(RouterFault, match="no 'responseId'"):
        parse_route_body({}, options=["a"], backend="t")


def test_wrong_types_are_faults():
    with pytest.raises(RouterFault, match="not a string"):
        parse_route_body({RESPONSE_KEY: 5}, options=["a"], backend="t")
    with pytest.raises(RouterFault, match="expected a JSON object"):
        parse_route_body("nope", options=["a"], backend="t")


def test_a_refusal_never_becomes_a_plausible_response():
    # The router's contract is "always a valid catalog id". The one thing that
    # must not happen is inventing one when the provider declined — the caller
    # would then speak it out loud.
    refusal = {"type": "refusal", "content": "I can't help with that"}
    with pytest.raises(RouterFault):
        parse_route_body(refusal, options=["a", "b"], backend="openai")


# ---------------------------------------------------------------- degenerate


def test_two_in_a_row_does_not_trip_the_guard():
    g = DegeneracyGuard()
    g.observe("a")
    g.observe("b")
    g.observe("c")
    g.observe("c")
    assert g.run_length == 2


def test_three_in_a_row_raises_and_fails_the_run():
    g = DegeneracyGuard()
    with pytest.raises(RouterTerminal, match="degenerate"):
        for _ in range(DEGENERATE_RUN_LENGTH):
            g.observe("a")


def test_a_varying_router_never_raises():
    g = DegeneracyGuard()
    for x in "abcde":
        g.observe(x)
    assert g.run_length == 1


# -------------------------------------------------------------------- retry


@pytest.mark.parametrize("status,expected", [(400, False), (404, False), (429, True), (500, True), (503, True)])
def test_retry_policy_never_retries_a_4xx(status, expected):
    # A 400 against a runtime-generated schema is a builder bug. Retrying it
    # turns a loud, correct failure into a slow, misleading one.
    assert should_retry(status) is expected


def test_a_transport_failure_with_no_status_is_retryable():
    assert should_retry(None, OSError("connection reset")) is True
