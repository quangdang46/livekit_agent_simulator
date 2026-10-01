"""The flip probe must not measure its own consumption.

`ResponseCatalog` is stateful: `serve()` spends a non-reusable entry for the
rest of the run, and `offerable_ids()` drops it. A probe that reuses one
catalog across its N iterations therefore sees the option set shrink under it,
and reports a rising "flip rate" produced entirely by the harness.

That is not hypothetical. It is the same failure as the three numbers this
script replaced — 55%, 23%, 7/10 — which were all measured on a catalog that
had already spent the correct answer, so the router picked something else
CORRECTLY from what was left and it was recorded as a routing failure.

These tests drive the probe's real functions with a scripted router, so the
freshness guarantee is asserted rather than assumed.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import router_flip_probe as probe  # noqa: E402
from livekit_agent_simulator.caller_contract.responses import ResponseCatalog  # noqa: E402


def _raw(n: int = 3) -> dict[str, dict[str, str]]:
    raw = {
        "r{}".format(i): {
            "intent": "i{}".format(i),
            "instruction": "ask {}".format(i),
            "text": "T{}".format(i),
        }
        for i in range(n)
    }
    raw["fallback"] = {
        "intent": "off_script",
        "instruction": "only when nothing else matches",
        "text": "Sorry, could you repeat that?",
        "system": True,
    }
    return raw


def test_a_spent_catalog_really_does_shrink() -> None:
    """The failure the probe is built to avoid, demonstrated.

    If this ever stops holding — say `offerable_ids` starts returning spent ids
    — the probe's central precaution becomes unnecessary, and its docstring
    would be lying. Worth knowing either way.
    """
    catalog = ResponseCatalog.from_dict(json.loads(json.dumps(_raw())))
    sizes = []
    for _ in range(4):
        sizes.append(len(catalog.offerable_ids()))
        catalog.serve(catalog.offerable_ids()[0])
    assert sizes == [4, 3, 2, 1], f"depletion behaviour changed: {sizes}"
    # The system entry is the one thing that never depletes.
    assert catalog.offerable_ids() == ["fallback"]


def test_every_router_call_gets_a_full_option_set() -> None:
    """The load-bearing property. A scripted router records what it was offered."""
    seen: list[tuple[str, ...]] = []

    class Scripted:
        name = "scripted"

        async def route(self, *, agent_transcript, catalog):  # noqa: ARG002
            from livekit_agent_simulator.caller_contract.router import RouteDecision

            seen.append(tuple(catalog.offerable_ids()))
            return RouteDecision(response_id=seen[-1][0], backend="scripted")

    catalogs = {"s": _raw()}
    inputs = [
        {"scenario": "s", "agent_text": f"question {i}"} for i in range(3)
    ]

    async def _go() -> dict:
        orig = probe._router_for
        probe._router_for = lambda **kw: Scripted()  # type: ignore[attr-defined]
        try:
            return await probe._measure(
                inputs, catalogs, n=4, provider="openai", model="m",
                api_key="k", timeout_s=1.0, temperature=0.0,
            )
        finally:
            probe._router_for = orig  # type: ignore[attr-defined]

    asyncio.run(_go())

    assert len(seen) == 12, "one call per input per iteration"
    assert len(set(seen)) == 1, (
        f"the option set changed between calls: {sorted(set(seen))} — the probe "
        "is measuring its own catalog depletion"
    )
    assert seen[0] == ("r0", "r1", "r2", "fallback")


def test_a_fault_is_not_counted_as_a_flip() -> None:
    """A network blip is not instability.

    Counting a raised call as a differing answer would let one bad network
    minute masquerade as a routing finding. Two inputs so the denominator is
    non-trivial: one faults, one is stable, and the rate must be 0.0 — not 0.5,
    and not None.
    """
    class Flaky:
        name = "flaky"

        def __init__(self) -> None:
            self.seen: dict[str, int] = {}

        async def route(self, *, agent_transcript, catalog):  # noqa: ARG002
            from livekit_agent_simulator.caller_contract.router import RouteDecision

            self.seen[agent_transcript] = self.seen.get(agent_transcript, 0) + 1
            # Per-input, not per-process: a shared counter made this test
            # fault BOTH inputs and it read as a probe bug rather than a
            # test bug. The probe advances input-by-input, so the counter
            # that matters is the one for the current transcript.
            if agent_transcript == "q-fault" and self.seen[agent_transcript] == 2:
                raise RuntimeError("transient transport failure")
            return RouteDecision(response_id="r0", backend="flaky")

    orig = probe._router_for
    probe._router_for = lambda **kw: Flaky()  # type: ignore[attr-defined]
    try:
        report = asyncio.run(
            probe._measure(
                [
                    {"scenario": "s", "agent_text": "q-ok"},
                    {"scenario": "s", "agent_text": "q-fault"},
                ],
                {"s": _raw()},
                n=4, provider="openai", model="m", api_key="k",
                timeout_s=1.0, temperature=0.0,
            )
        )
    finally:
        probe._router_for = orig  # type: ignore[attr-defined]

    assert report["inputs_faulted"] == 1
    assert report["inputs_unstable"] == 0
    assert report["inputs_measured"] == 1
    assert report["flip_rate"] == 0.0, "a fault must not inflate the rate"


def test_a_run_where_everything_faults_reports_no_rate() -> None:
    """Zero measured inputs is not a 0% flip rate.

    `None` and `0.0` are different claims and only one of them is evidence.
    """

    class Dead:
        name = "dead"

        async def route(self, *, agent_transcript, catalog):  # noqa: ARG002
            raise RuntimeError("no network")

    orig = probe._router_for
    probe._router_for = lambda **kw: Dead()  # type: ignore[attr-defined]
    try:
        report = asyncio.run(
            probe._measure(
                [{"scenario": "s", "agent_text": "q"}], {"s": _raw()},
                n=3, provider="openai", model="m", api_key="k",
                timeout_s=1.0, temperature=0.0,
            )
        )
    finally:
        probe._router_for = orig  # type: ignore[attr-defined]

    assert report["inputs_measured"] == 0
    assert report["flip_rate"] is None, (
        "a probe that measured nothing must not report 0% instability"
    )


def test_a_mismatched_sha_is_skipped_rather_than_measured() -> None:
    """A stale input pair is worse than no input.

    It measures a transcript that is no longer the one being claimed, and the
    result is attributed to the wrong run.
    """
    called: list[str] = []

    class Recording:
        name = "recording"

        async def route(self, *, agent_transcript, catalog):  # noqa: ARG002
            from livekit_agent_simulator.caller_contract.router import RouteDecision

            called.append(agent_transcript)
            return RouteDecision(response_id="r0", backend="recording")

    orig = probe._router_for
    probe._router_for = lambda **kw: Recording()  # type: ignore[attr-defined]
    try:
        report = asyncio.run(
            probe._measure(
                [
                    {
                        "scenario": "s",
                        "agent_text": "hello",
                        "agent_text_sha": "deadbeef",  # wrong on purpose
                    }
                ],
                {"s": _raw()},
                n=2, provider="openai", model="m", api_key="k",
                timeout_s=1.0, temperature=0.0,
            )
        )
    finally:
        probe._router_for = orig  # type: ignore[attr-defined]

    assert called == [], "a stale pair must not reach the provider at all"
    assert report["inputs_measured"] == 0
    assert report["flip_rate"] is None


def test_rate_is_none_rather_than_zero_with_nothing_measured() -> None:
    """Zero and unmeasured are different claims.

    `0% flip` from ten inputs and `0% flip` from none are not the same finding,
    and only one of them is evidence.
    """
    report = asyncio.run(
        probe._measure(
            [], {}, n=3, provider="openai", model="m", api_key="k",
            timeout_s=1.0, temperature=0.0,
        )
    )
    assert report["flip_rate"] is None
    assert report["inputs_measured"] == 0
