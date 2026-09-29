"""The two prompts, pinned.

Bead livekit-agent-simulator-response-router-v2-13-ltq. These are the only two
places a routed call's words are decided, so a prompt edit with no test is a
production behaviour change nobody sees. Snapshots here are the tripwire.

The load-bearing assertion is NOT the golden text — it is
``test_canonical_text_never_reaches_the_router_prompt``. The router is given
intent+instruction and must never be handed ``text``; if the authored answer
reaches the router, it pattern-matches the answer instead of the question.
"""

from __future__ import annotations

import json

import pytest

from livekit_agent_simulator.caller_contract.driver import build_routed_context
from livekit_agent_simulator.caller_contract import BehaviorContract, ContractConstraints
from livekit_agent_simulator.caller_contract.responses import ResponseCatalog
from livekit_agent_simulator.caller_contract import router_gemini, router_openai
from livekit_agent_simulator.caller_contract import text_backends

CATALOG = {
    "company": {
        "intent": "ask_company",
        "instruction": "Provide the company name",
        "text": "It's Bluebird Property Management.",
    },
    "price": {
        "intent": "ask_price",
        "instruction": "Ask what the monthly price is",
        "text": "How much is it per month?",
    },
    "system": {
        "intent": "off_script",
        "instruction": "Keep the call on track",
        "text": "Could we stay focused?",
        "system": True,
    },
}

AGENT_SAID = "Which company is this?"


def _catalog() -> ResponseCatalog:
    return ResponseCatalog.from_dict(CATALOG, file="fixture")


def _routed_context() -> dict:
    contract = BehaviorContract(
        behavior="ask",
        target=None,
        constraints=ContractConstraints(max_turns=3, max_budget=1),
    )
    return build_routed_context(
        contract=contract,
        response=_catalog().get("company"),
        turn=1,
        agent_latest=AGENT_SAID,
        recent_turns=[],
    )


# ------------------------------------------------------------- router prompt

ROUTER_PROMPT_GOLDEN = (
    "You route a simulated caller's next reply. The caller speaks for the "
    "company receiving the call; the agent is on the line. Read what the agent "
    "just said and choose the ONE response that answers it. Return only the "
    "responseId. Choose the system entry ONLY when nothing else fits."
)

# Enum ORDER is part of the contract: it is the option order the model sees.
ROUTER_USER_PROMPT_GOLDEN = {
    "agent_said": AGENT_SAID,
    "responses": [
        {"responseId": "company", "intent": "ask_company",
         "instruction": "Provide the company name"},
        {"responseId": "price", "intent": "ask_price",
         "instruction": "Ask what the monthly price is"},
        {"responseId": "system", "intent": "off_script",
         "instruction": "Keep the call on track"},
    ],
}


@pytest.mark.parametrize("mod", [router_openai, router_gemini], ids=["openai", "gemini"])
def test_router_system_prompt_is_pinned(mod):
    assert mod._SYSTEM == ROUTER_PROMPT_GOLDEN


@pytest.mark.parametrize("mod", [router_openai, router_gemini], ids=["openai", "gemini"])
def test_router_user_prompt_is_pinned_including_option_order(mod):
    rendered = json.loads(mod._user_prompt(AGENT_SAID, _catalog()))
    assert rendered == ROUTER_USER_PROMPT_GOLDEN
    # Explicit about the thing a reordering would silently break.
    assert [r["responseId"] for r in rendered["responses"]] == [
        "company", "price", "system"
    ]


# ---------------------------------------------------------- text backend prompt

TEXT_SYSTEM_PROMPT_GOLDEN_TAIL = (
    "If the context carries `canonical_text`, that is the line to speak: "
    "phrase it naturally, but do not add any fact, name, number, or offer "
    "that is not already in it, and do not drop any part of it."
)


def test_text_backend_prompt_carries_the_canonical_text_clause():
    assert TEXT_SYSTEM_PROMPT_GOLDEN_TAIL in text_backends._SYSTEM_PROMPT


def test_text_backend_user_prompt_carries_canonical_text():
    ctx = _routed_context()
    assert json.loads(text_backends._build_user_prompt(ctx))["canonical_text"] == (
        "It's Bluebird Property Management."
    )


# ------------------------------------------------- the load-bearing assertion

@pytest.mark.parametrize("mod", [router_openai, router_gemini], ids=["openai", "gemini"])
def test_canonical_text_never_reaches_the_router_prompt(mod):
    """The router is shown intent+instruction only. If the authored `text`
    ever leaks into its prompt, the model pattern-matches the answer instead
    of matching the question — which ResponseSpec's docstring names as the
    reason the router is never given the text in the first place."""
    blob = mod._SYSTEM + mod._user_prompt(AGENT_SAID, _catalog())
    for secret in ("Bluebird", "How much is it per month?", "Could we stay focused?"):
        assert secret not in blob, f"authored text leaked into the router prompt: {secret!r}"


def test_the_router_prompt_never_carries_a_canonical_text_key():
    for mod in (router_openai, router_gemini):
        assert "canonical_text" not in mod._SYSTEM
        assert "canonical_text" not in mod._user_prompt(AGENT_SAID, _catalog())


# ------------------------------------------------------------- terminology

@pytest.mark.parametrize("mod", [router_openai, router_gemini], ids=["openai", "gemini"])
def test_router_facing_prompt_has_no_legacy_terminology(mod):
    """The router speaks in responses/intents, not steps or behaviors. The text
    backend legitimately says "current_behavior" — that vocabulary belongs to
    the free-generation path and must not be asserted away here."""
    blob = (mod._SYSTEM + mod._user_prompt(AGENT_SAID, _catalog())).lower()
    for stale in ("caller step", "next step", "caller_steps", "behavior"):
        assert stale not in blob, f"legacy term {stale!r} still in the router prompt"
