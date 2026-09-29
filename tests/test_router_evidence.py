"""Router evidence in run artifacts (response-router v2-10).

The failure this guards against is not a crash — it is attribution that reads
plausibly while being wrong. A correct agent that draws a false off_script,
and a hallucinating agent that draws matched, each silently corrupt a suite.
So the assertions are on BOTH directions, and on the ABSENCE of a verdict
where a verdict must not exist.
"""

from __future__ import annotations

from livekit_agent_simulator.run_orchestrator import _summarize_router


def _ev(kind: str, **spec) -> dict:
    return {"kind": kind, "spec": spec}


MATCHED = _ev(
    "contract.router_decision", turn=0, response_id="company", off_script=False,
    confidence=0.9, backend="openai", latency_ms=12, agent_text="hi",
    agent_text_sha="abc",
)
OFF = _ev(
    "contract.router_decision", turn=1, response_id="sys", off_script=True,
    confidence=0.4, backend="openai", latency_ms=9, agent_text="what about parking",
    agent_text_sha="def",
)


def test_no_router_events_yields_no_key():
    # A non-router run must gain no `router` key at all (D11).
    assert _summarize_router([]) is None
    assert _summarize_router([_ev("contract.say_published", text="x")]) is None


def test_matched_and_off_script_are_counted_separately():
    s = _summarize_router([MATCHED, OFF])
    assert s is not None
    assert s["matched"] == 1
    assert s["off_script"] == 1
    # Separated deliberately: one collapsed number cannot show which way the
    # attribution went wrong.
    assert len(s["decisions"]) == 2


def test_each_decision_keeps_its_own_identity_and_hash():
    s = _summarize_router([MATCHED, OFF])
    assert [d["response_id"] for d in s["decisions"]] == ["company", "sys"]
    assert s["decisions"][0]["agent_text_sha"] == "abc"
    assert s["decisions"][1]["agent_text_sha"] == "def"
    # confidence is recorded for diagnosis but did NOT decide the verdict
    assert s["decisions"][0]["confidence"] == 0.9
    assert s["decisions"][0]["off_script"] is False


def test_truncated_agent_text_still_carries_a_hash_of_the_full_line():
    s = _summarize_router([
        _ev("contract.router_decision", turn=0, response_id="company",
            off_script=False, agent_text="x" * 200, agent_text_sha="full-hash")
    ])
    # The stored text is short, but the hash of the FULL line is what an auditor
    # uses, so truncation cannot conceal a divergence (D10).
    assert s["decisions"][0]["agent_text_sha"] == "full-hash"


def test_a_fault_is_counted_separately_and_never_as_a_verdict():
    s = _summarize_router([
        MATCHED,
        _ev("contract.router_fault", reason="openai: model declined"),
    ])
    assert s["faults"] == 1
    assert s["matched"] == 1
    assert s["off_script"] == 0
    # A fault is a harness event, not a decision: it must not appear in the
    # decision list, or a reader would count it as the agent going off-script.
    assert all(d["response_id"] == "company" for d in s["decisions"])


def test_an_unroutable_turn_is_never_counted_as_off_script():
    # The point of the unroutable path: a correct agent whose line the harness
    # could not parse must NOT be accused of going off-script.
    s = _summarize_router([
        _ev("contract.router_unroutable", turn=0, reason="untranscribed"),
        MATCHED,
    ])
    assert s["unroutable"] == 1
    assert s["off_script"] == 0
    assert s["matched"] == 1
