"""The router must be ATTACHED, not just present on the driver class.

Unit tests for the routed branch inject ``driver.router`` by hand, so they
pass whether or not any production code path ever assigns it. This file
covers the seam those tests bypass: ``_attach_response_router``, the only
place ``router``/``response_catalog`` are set on a driver that a real run
will use.

Two directions are load-bearing, and both have been satisfied by a broken
implementation on its own:

* a missing attach means an authored ``responses:`` catalog is silently
  ignored and the run behaves exactly like the legacy one;
* an attach that ignores half-configuration turns the same authoring mistake
  into that silent legacy run instead of a named ConfigError.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from livekit_agent_simulator.caller_contract.driver import ContractCallerDriver
from livekit_agent_simulator.caller_contract.live_wiring import (
    _attach_response_router,
)
from livekit_agent_simulator.caller_contract.responses import ResponseCatalog
from livekit_agent_simulator.config import ConfigError


def _spec(intent: str, text: str, **kw: object) -> dict[str, object]:
    return {"intent": intent, "instruction": f"Provide {intent}.", "text": text, **kw}


def _catalog() -> ResponseCatalog:
    return ResponseCatalog.from_dict(
        {
            "company_name": _spec("company_name", "It's Bluebird Property Management."),
            "off_script": {
                "intent": "off_script",
                "instruction": "Select only when no other response fits.",
                "text": "I'm sorry, could we stay focused?",
                "system": True,
            },
        }
    )


def _driver() -> ContractCallerDriver:
    return ContractCallerDriver.__new__(ContractCallerDriver)


@dataclass
class _Cfg:
    router: object = None
    text_planner: object = None


def _cfg(provider: str = "openai", planner: object = None, **router: object) -> _Cfg:
    # Build through the REAL RouterConfig so `timeout_s` (a property, not a
    # field) and any future derived field come along — a hand-rolled
    # namespace here would drift from what load_config actually produces.
    from livekit_agent_simulator.config import RouterConfig

    base = {"provider": provider, "model": "", "api_key": "sk-test", "timeout_ms": 1_500, "temperature": 0.0}
    base.update(router)
    return _Cfg(router=RouterConfig(**base), text_planner=planner)


def _planner(enabled: bool = True, provider: str = "openai", model: str = "", api_key: str = "sk-x"):
    return SimpleNamespace(enabled=enabled, provider=provider, model=model, api_key=api_key)


def _scenario(responses: object = "default", sid: str = "r1") -> SimpleNamespace:
    return SimpleNamespace(id=sid, responses=_catalog() if responses == "default" else responses)


# --------------------------------------------------------------------------
# attach: the seam itself
# --------------------------------------------------------------------------


def test_attaching_builds_the_router_and_hands_it_the_catalog():
    d = _driver()
    _attach_response_router(d, _cfg(), _scenario())
    assert d.router is not None, "the router must be attached, not left for a test to inject"
    assert d.response_catalog is not None
    assert d.planner_enabled is True


def test_the_attached_router_is_a_real_provider_adapter():
    d = _driver()
    _attach_response_router(d, _cfg(provider="gemini"), _scenario())
    from livekit_agent_simulator.caller_contract.router_gemini import GeminiResponseRouter

    assert isinstance(d.router, GeminiResponseRouter)


def test_an_empty_model_selects_the_adapter_default_alias():
    # A pinned version 404s on the working Gemini key (text_backends.py:155-157);
    # an empty `model:` must fall through to the adapter's alias, not to "".
    d = _driver()
    _attach_response_router(d, _cfg(provider="gemini", model=""), _scenario())
    assert d.router._model == "gemini-flash-latest"


# --------------------------------------------------------------------------
# half-configuration: fails loud, never degrades to legacy
# --------------------------------------------------------------------------


def test_responses_without_a_router_block_is_a_named_config_error():
    # Unreachable via load_config (parse-time ConfigError), but a directly
    # constructed Scenario must not slip past into a legacy-looking run.
    with pytest.raises(ConfigError) as err:
        _attach_response_router(_driver(), _Cfg(router=None), _scenario())
    msg = str(err.value)
    assert "responses:" in msg and "router:" in msg
    assert "r1" in msg, "the message must name the offending scenario"


def test_router_without_a_catalog_is_silent_and_legacy():
    # The D13 opt-in model: an archived caller_steps scenario runs unchanged
    # even when the operator configures a router globally. Not an error.
    d = _driver()
    _attach_response_router(d, _cfg(), _scenario(responses=None))
    assert d.router is None


# --------------------------------------------------------------------------
# text_planner: the config key has a consumer
# --------------------------------------------------------------------------


def test_disabling_the_planner_publishes_catalog_text_verbatim():
    d = _driver()
    _attach_response_router(
        d,
        _cfg(planner=_planner(enabled=False)),
        _scenario(),
    )
    assert d.planner_enabled is False
    assert d.routed_adapter is None, "verbatim mode must make no backend call"


def test_an_enabled_planner_gets_its_own_backend():
    d = _driver()
    _attach_response_router(
        d,
        _cfg(planner=_planner(model="m-1")),
        _scenario(),
    )
    assert d.planner_enabled is True
    assert d.routed_adapter is not None
    assert getattr(d.routed_adapter, "_model", "") == "m-1" or d.routed_adapter.backend._model == "m-1"


# --------------------------------------------------------------------------
# end of the seam is covered in test_router_driver_path.py, which owns the
# real driver harness. Duplicating a hand-rolled driver here would prove
# nothing — a stub satisfies a gate a stub also sets.
# --------------------------------------------------------------------------

