"""Preflight must say something about the router — including when it is off.

`lks init` deliberately does NOT scaffold a `router:` block: a shipped-but-dead
`api_key` placeholder is the "knob nobody runs" smell AGENTS.md forbids. So
absence is the NORMAL state, and that is exactly why it has to be stated
rather than omitted — a silent omission is indistinguishable from preflight
not knowing the feature exists.

The same reasoning applies to `text_planner`: it is opt-in, and its absence is
meaningful (routed lines publish verbatim), not a gap.
"""

from __future__ import annotations

import pytest

from livekit_agent_simulator.preflight import _check_router_config, PreflightResult
from livekit_agent_simulator.config import (
    RouterConfig,
    SimConfig,
    TextPlannerConfig,
    _build_simulator_config,
)


def _cfg(**kw) -> SimConfig:
    from pathlib import Path

    from livekit_agent_simulator.config import LiveKitConfig

    sim = _build_simulator_config({"api_key": "sk-" + "x" * 24}, name="test")
    return SimConfig(
        project_root=Path("."),
        livekit=LiveKitConfig(
            url="wss://x.livekit.cloud", api_key="k", api_secret="s", agent_name="w"
        ),
        simulator=sim,
        router=kw.get("router"),
        text_planner=kw.get("text_planner"),
    )


def _names(result: PreflightResult) -> dict[str, str]:
    return {c["name"]: c["status"] for c in result.checks}


# --------------------------------------------------------------------------
# the normal state: not configured
# --------------------------------------------------------------------------


def test_no_router_is_reported_as_absent_not_ignored() -> None:
    """The load-bearing case, because it is the default after `lks init`."""
    r = PreflightResult(ok=True)
    _check_router_config(_cfg(), r)
    assert _names(r)["router"] == "info", (
        "an unconfigured router must be STATED; a silent omission reads as "
        "'preflight does not know about routers'"
    )


def test_an_absent_router_does_not_fail_preflight() -> None:
    """It is opt-in, so it must not turn a healthy project red."""
    r = PreflightResult(ok=True)
    _check_router_config(_cfg(), r)
    assert r.ok is True


def test_no_text_planner_says_lines_are_verbatim() -> None:
    r = PreflightResult(ok=True)
    _check_router_config(_cfg(), r)
    detail = next(c["detail"] for c in r.checks if c["name"] == "text_planner")
    assert "verbatim" in detail, (
        "absence of text_planner has a MEANING - routed lines publish verbatim"
    )


# --------------------------------------------------------------------------
# configured
# --------------------------------------------------------------------------


def test_a_configured_router_with_a_key_passes() -> None:
    r = PreflightResult(ok=True)
    _check_router_config(_cfg(router=RouterConfig(provider="openai", api_key="sk-" + "y" * 24)), r)
    n = _names(r)
    assert n["router.api_key[openai]"] == "pass"
    assert "router.model[openai]" in n
    assert r.ok is True


def test_a_router_without_a_key_fails() -> None:
    """Unlike `text_planner.enabled: false`, a missing credential is fatal —
    every routed turn would fail with a 401."""
    r = PreflightResult(ok=True)
    _check_router_config(_cfg(router=RouterConfig(provider="gemini", api_key="")), r)
    assert _names(r)["router.api_key[gemini]"] == "fail"
    assert r.ok is False


def test_an_empty_model_reports_the_adapter_default_not_a_pin() -> None:
    """A pinned version 404s on the working Gemini key.

    Reporting `model` as if it were set would hide that the operator never
    chose one.
    """
    r = PreflightResult(ok=True)
    _check_router_config(_cfg(router=RouterConfig(provider="gemini", api_key="sk-" + "z" * 24)), r)
    detail = next(c["detail"] for c in r.checks if c["name"] == "router.model[gemini]")
    assert "default" in detail and "alias" in detail


def test_an_explicit_model_is_reported_verbatim() -> None:
    r = PreflightResult(ok=True)
    _check_router_config(
        _cfg(router=RouterConfig(provider="openai", api_key="sk-" + "q" * 24, model="gpt-4.1-nano")),
        r,
    )
    detail = next(c["detail"] for c in r.checks if c["name"] == "router.model[openai]")
    assert detail == "gpt-4.1-nano"


# --------------------------------------------------------------------------
# text_planner: the config key that had no reader
# --------------------------------------------------------------------------


def test_a_disabled_planner_is_a_valid_mode_not_a_failure() -> None:
    """`enabled: false` is byte-exact mode, not a misconfiguration.

    Treating it as a warning would push operators to enable paraphrase just to
    silence a check, which is backwards.
    """
    r = PreflightResult(ok=True)
    _check_router_config(
        _cfg(
            router=RouterConfig(provider="openai", api_key="sk-" + "w" * 24),
            text_planner=TextPlannerConfig(enabled=False, provider="openai"),
        ),
        r,
    )
    assert _names(r)["text_planner"] == "pass"
    assert r.ok is True
    detail = next(c["detail"] for c in r.checks if c["name"] == "text_planner")
    assert "byte-exact" in detail


def test_an_enabled_planner_names_its_provider() -> None:
    r = PreflightResult(ok=True)
    _check_router_config(
        _cfg(
            router=RouterConfig(provider="openai", api_key="sk-" + "e" * 24),
            text_planner=TextPlannerConfig(enabled=True, provider="gemini"),
        ),
        r,
    )
    detail = next(c["detail"] for c in r.checks if c["name"] == "text_planner")
    assert "gemini" in detail and "paraphrased" in detail


def test_the_planner_is_reported_even_without_a_router() -> None:
    """Config load accepts the two blocks independently.

    Reporting the planner only when a router exists would hide a half-configured
    project, which is precisely the shape that produced the `_run_scenario`
    TypeError earlier today.
    """
    r = PreflightResult(ok=True)
    _check_router_config(_cfg(text_planner=TextPlannerConfig(enabled=True, provider="openai")), r)
    assert "text_planner" in _names(r)
