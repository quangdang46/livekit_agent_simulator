"""The judge must see the harness's own routing attribution.

Bead livekit-agent-simulator-response-router-v2-15-szz.

AUDIT RESULT THAT SHAPED THIS FILE: the bead's opening premise, "the judge
indexes steps", is FALSE. Grepping evals/ for step-index vocabulary across
identifiers AND string contents returns nothing; the only hit is a docstring
describing a development step ("Hamming step A"). The judge prompt is already
turn- and node-oriented.

What IS true is the adjacent gap this file pins: the harness computes
matched/off_script per decision, writes it to the run summary, and the judge
never saw any of it. A judge grading a routed call could not tell an authored
on-script reply from a fallback reached because nothing in the catalog fitted.
"""

from __future__ import annotations

import pytest

from livekit_agent_simulator.evals.prompt import (
    build_router_digest,
    build_user_prompt,
)
from livekit_agent_simulator.evals.runner import _judge

SUMMARY = {
    "matched": 1,
    "off_script": 1,
    "decisions": [
        {"response_id": "company", "turn": 0, "off_script": False, "confidence": 0.9},
        {"response_id": "system", "turn": 1, "off_script": True, "confidence": 0.4},
    ],
}


# ----------------------------------------------------------------- the digest


def test_digest_states_both_totals_and_each_decision():
    d = build_router_digest(SUMMARY)
    assert d is not None
    assert "2 responses chosen" in d or "responses chosen: 2" in d
    assert "1 matched" in d
    assert "off-script" in d
    assert "company" in d and "system" in d


def test_digest_marks_which_decision_was_off_script():
    d = build_router_digest(SUMMARY) or ""
    off_line = [ln for ln in d.splitlines() if "system" in ln]
    assert off_line and "off-script" in off_line[0]
    on_line = [ln for ln in d.splitlines() if "company" in ln]
    assert on_line and "matched" in on_line[0] and "off-script" not in on_line[0]



@pytest.mark.parametrize(
    "empty",
    [None, {}, "not-a-dict", {"matched": 0, "off_script": 0}, {"decisions": []}],
)
def test_digest_is_none_without_decisions(empty):
    """A non-router run, or a router that never chose, contributes nothing."""
    assert build_router_digest(empty) is None


def test_digest_survives_a_malformed_decision():
    d = build_router_digest({"decisions": ["nonsense", {"response_id": "x"}]})
    assert d is not None
    assert "x" in d


# ------------------------------------------------------------- the user prompt


def _prompt(**kw) -> str:
    base = dict(
        pass_criteria=["the agent confirmed the company name"],
        transcript="A: hi\nB: Bluebird",
        tool_spans="(none)",
    )
    base.update(kw)
    return build_user_prompt(**base)


def test_router_digest_reaches_the_judge_prompt():
    out = _prompt(router_digest=build_router_digest(SUMMARY))
    assert "RESPONSE ROUTING" in out
    assert "company" in out
    assert "system" in out


def test_the_framing_says_evidence_not_verdict():
    """Asymmetric on purpose. The assert contract is authoritative; routing is
    evidence, because an off-script turn can still have read naturally."""
    out = _prompt(router_digest=build_router_digest(SUMMARY))
    low = out.lower()
    assert "evidence, not a verdict" in low
    assert "authoritative" not in low.split("response routing")[1][:400]


def test_a_non_router_run_prompt_is_byte_identical_to_before():
    """D11 in the judge prompt: no router, no section, not an empty one. A
    consumer diffing judge prompts across the migration must see zero change
    for a non-router run."""
    assert _prompt() == _prompt(router_digest=None)
    assert "RESPONSE ROUTING" not in _prompt()


def test_routing_section_follows_the_flow_digest():
    out = _prompt(flow_digest="- node 1", router_digest=build_router_digest(SUMMARY))
    assert out.index("FLOW EVENTS") < out.index("RESPONSE ROUTING")


class _StubBackend:
    """Captures the user prompt so the test can assert on what the judge is
    actually shown, rather than on a helper it called by hand."""

    def __init__(self):
        self.user = None

    async def complete_json(self, *, system, user):
        self.user = user
        return '{"verdict": "pass", "notes": ""}'


@pytest.mark.asyncio
async def test_the_digest_survives_the_runners_own_threading():
    """The gap the direct-call tests cannot see. Every other case here calls
    build_user_prompt by hand, so all of them stay green if _judge forgets to
    pass router_digest on. Mutation-checked: deleting the forwarding line in
    runner.py makes exactly this test fail."""
    from livekit_agent_simulator.evals.runner import _judge

    backend = _StubBackend()
    await _judge(
        backend,
        ["the agent confirmed the company name"],
        [{"speaker": "agent", "text": "Bluebird"}],
        [],
        router_digest=build_router_digest(SUMMARY),
    )
    assert backend.user is not None
    assert "RESPONSE ROUTING" in backend.user
    assert "company" in backend.user


@pytest.mark.asyncio
async def test_a_non_router_run_reaches_the_judge_with_no_routing_section():
    from livekit_agent_simulator.evals.runner import _judge

    backend = _StubBackend()
    await _judge(
        backend,
        ["the agent confirmed the company name"],
        [{"speaker": "agent", "text": "Bluebird"}],
        [],
    )
    assert backend.user is not None
    assert "RESPONSE ROUTING" not in backend.user
