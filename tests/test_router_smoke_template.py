"""The router, run through the package's OWN scenario template.

Everything else about this feature was exercised two ways: unit tests that
inject ``driver.router`` by hand, and hand-run E2E in a target repo. Neither
runs inside this package, and that is not a stylistic gap — it had a
concrete cost. Nothing in the run path attached the router, so a real
``responses:`` scenario took the legacy ``caller_steps`` path, emitted no
``contract.router_decision``, and reported as a clean run while every router
test was green.

So this drives ``run_contract_driver_path`` — the function a real run calls —
with ``templates/examples/router-smoke.yaml``, the scenario the package
ships for exactly this purpose.

What each test pins:

* both verdicts appear, so attribution is observable in both directions. A
  correct agent drawing a false ``off_script`` and a deviating agent drawing
  ``matched`` each corrupt a suite silently, and each satisfies a one-sided
  assertion.
* ``text_planner.enabled: false`` is byte-reproducible across runs, which is
  the property D12's record/replay ban rests on. With the planner on, this
  does NOT hold, and that is the documented cost, not a bug.
* the template parses and round-trips through export.

No network: the router call is scripted and the one legacy text-backend call
is mocked, exactly as in ``test_contract_live_wiring``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from livekit_agent_simulator.scenario import parse_scenario
from livekit_agent_simulator.scenario_yaml import scenario_to_dict

from test_contract_live_wiring import (
    FakeBridge,
    FakeObserver,
    FakeWriter,
    ScriptedAgentWait,
    _mock_openai_reply,
    run_contract_driver_path,
)

TEMPLATE = Path(__file__).resolve().parents[1] / "templates" / "examples" / "router-smoke.yaml"

AGENT_LINES = [
    "Which company are you with?",
    "That is well over our budget.",
    "We cannot go any higher.",
    "Then we will pass.",
]

# The legacy turn 0 goes through the text backend and the semantic verifier;
# this is the payload test_contract_driver's _ScriptedBackend emits for
# negotiate/price, which the verifier accepts.
LEGACY_REPLY = {
    "act": "negotiate",
    "target": "price",
    "slots": {"max_budget": 30000},
    "utterance": "Would you be able to come down to $30,000?",
}


def _cfg(*, planner_enabled: bool):
    return SimpleNamespace(
        simulator=SimpleNamespace(provider="openai", api_key="sk-test"),
        router=SimpleNamespace(
            provider="openai",
            model="",
            api_key="sk-test",
            timeout_ms=1_500,
            temperature=0.0,
            timeout_s=1.5,
        ),
        text_planner=SimpleNamespace(
            enabled=planner_enabled, provider="openai", model="", api_key="sk-test"
        ),
    )


class _ScriptedRouter:
    """Real construction (the seam builds this class); scripted decisions.

    What is under test is the attach and the attribution, not the HTTP call.
    """

    def __init__(self, decisions: list[str], **kw):
        self._decisions = list(decisions)
        self.kw = kw
        self.seen: list[str] = []

    async def route(self, *, agent_transcript: str, catalog):
        self.seen.append(agent_transcript)
        return self._make(self._decisions[min(len(self.seen) - 1, len(self._decisions) - 1)])

    def _make(self, response_id: str):
        from livekit_agent_simulator.caller_contract.router import RouteDecision

        return RouteDecision(
            response_id=response_id, backend="scripted", latency_ms=1
        )


async def _run(decisions: list[str], *, planner_enabled: bool):
    """Drive the shipped template through the real path. Returns the writer."""
    import livekit_agent_simulator.caller_contract.live_wiring as _lw
    import livekit_agent_simulator.caller_contract.router_openai as _ro

    scenario = parse_scenario(TEMPLATE)
    observer = FakeObserver(replies=list(AGENT_LINES))
    writer = FakeWriter()
    router = _ScriptedRouter(decisions)

    _lw._SHERPA_DEAD = True  # sherpa model download shares urlopen
    try:
        with patch.object(_ro, "OpenAIResponseRouter", lambda **kw: router), patch(
            "urllib.request.urlopen", return_value=_mock_openai_reply(LEGACY_REPLY)
        ), patch(
            "livekit_agent_simulator.caller_contract.live_wiring.ObserverAgentWait",
            return_value=ScriptedAgentWait(observer),
        ):
            try:
                await run_contract_driver_path(
                    scenario, None, observer, FakeBridge(), writer, _cfg(planner_enabled=planner_enabled)
                )
            except Exception:  # noqa: BLE001 — the behavior is unsatisfiable by design;
                # the routing decisions are recorded BEFORE the ending, which is what this asserts
                pass
    finally:
        _lw._SHERPA_DEAD = False
    return writer, router


def _decisions(writer: FakeWriter) -> list[dict]:
    return [spec for kind, spec in writer.events if kind == "contract.router_decision"]


# --------------------------------------------------------------------------
# the template itself
# --------------------------------------------------------------------------


def test_the_shipped_template_parses_and_carries_a_valid_catalog():
    scenario = parse_scenario(TEMPLATE)
    assert scenario.caller_actions, "templates must drive the contract path"
    assert scenario.responses is not None, "this template exists to exercise the router"
    # The system entry is what makes "the router always returns a valid
    # responseId" structural rather than aspirational.
    assert sum(1 for s in scenario.responses.responses.values() if s.system) == 1


def test_the_template_round_trips_through_export():
    once = scenario_to_dict(parse_scenario(TEMPLATE))
    twice = scenario_to_dict(parse_scenario(TEMPLATE))
    assert once == twice
    assert "responses" in once, "export must not silently drop the catalog (D13)"


def test_smoke_hello_is_untouched():
    """The regression net .16 depends on. This template must not disturb it."""
    hello = parse_scenario(
        Path(__file__).resolve().parents[1] / "templates" / "smoke-hello.yaml"
    )
    assert hello.responses is None, "smoke-hello must stay caller_steps-only"


# --------------------------------------------------------------------------
# both verdicts, through the real path
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_matched_turn_and_an_off_script_turn_are_both_recorded():
    writer, router = await _run(["company_name", "off_script"], planner_enabled=False)
    decisions = _decisions(writer)

    assert decisions, (
        "the router was never attached: the run took the legacy caller_steps "
        "path and emitted no routing decision"
    )
    assert [d["response_id"] for d in decisions[:2]] == ["company_name", "off_script"]
    assert [d["off_script"] for d in decisions[:2]] == [False, True]
    # The router must be asked what the agent ACTUALLY said, not a marker.
    assert router.seen and router.seen[0].strip()


@pytest.mark.asyncio
async def test_the_off_script_turn_publishes_the_system_response():
    writer, _ = await _run(["off_script"], planner_enabled=False)
    assert [d["response_id"] for d in _decisions(writer)][:1] == ["off_script"]


# --------------------------------------------------------------------------
# byte-reproducibility — the property D12's ban rests on
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verbatim_mode_is_byte_identical_across_runs():
    first, _ = await _run(["company_name", "off_script"], planner_enabled=False)
    second, _ = await _run(["company_name", "off_script"], planner_enabled=False)
    assert _decisions(first) == _decisions(second), (
        "text_planner.enabled: false must make routed decisions reproducible"
    )


@pytest.mark.asyncio
async def test_verbatim_mode_publishes_the_authored_text_exactly():
    from livekit_agent_simulator.scenario import parse_scenario as _p

    catalog = _p(TEMPLATE).responses
    writer, _ = await _run(["company_name", "off_script"], planner_enabled=False)
    spoken = [
        spec.get("text")
        for kind, spec in writer.events
        if kind == "contract.turn_published"
    ]
    assert catalog.get("company_name").text in spoken, (
        "verbatim mode must publish catalog text byte-for-byte, not a paraphrase"
    )
    assert catalog.get("off_script").text in spoken, (
        "the system response must publish its authored text too"
    )
