"""Tests for the OpenAI decision-router adapter (response-router v2-6).

The load-bearing assertion here is negative: the routing prompt must NOT carry
the authored answer text. If it did, the model would pattern-match on the
answer instead of the question, and the router would stop being a router.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import unittest.mock as mock

import pytest

from livekit_agent_simulator.caller_contract.responses import ResponseCatalog
from livekit_agent_simulator.caller_contract.router import (
    RESPONSE_KEY,
    RouterError,
    RouterFault,
    clear_schema_cache,
)
from livekit_agent_simulator.caller_contract.router_openai import (
    OpenAIResponseRouter,
    _user_prompt,
)

ANSWER = "Bluebird Property Management"


def _r(intent: str, text: str, **kw) -> dict:
    return {"intent": intent, "instruction": f"Provide {intent}.", "text": text, **kw}


def _catalog() -> ResponseCatalog:
    clear_schema_cache()
    return ResponseCatalog.from_dict(
        {
            "company": _r("ask_company", ANSWER),
            "sys": _r("off_script", "Could we stay focused?", system=True),
        }
    )


def _respond(payload: dict) -> mock.MagicMock:
    """A urlopen mock returning one Chat Completions body."""
    m = mock.MagicMock()
    m.return_value.__enter__.return_value.read.return_value = json.dumps(payload).encode()
    return m


def _route(responses, catalog=None, transcript="What is your company name?"):
    router = OpenAIResponseRouter(api_key="k")
    with mock.patch("urllib.request.urlopen", responses):
        return asyncio.run(router.route(agent_transcript=transcript, catalog=catalog or _catalog())), responses


# ------------------------------------------------------------------- prompt


def test_prompt_never_carries_the_authored_answer_text():
    # The whole router is "read the question, pick a response". Handed the
    # answer, the model pattern-matches instead, and every decision silently
    # degrades toward guessing.
    body = _user_prompt("What is your company name?", _catalog())
    assert ANSWER not in body
    assert "Bluebird" not in body
    parsed = json.loads(body)
    assert parsed["agent_said"] == "What is your company name?"
    for option in parsed["responses"]:
        assert "text" not in option, "the option carries intent+instruction only"


def test_prompt_includes_the_system_entry_as_a_selection_rule():
    parsed = json.loads(_user_prompt("unrelated", _catalog()))
    ids = [o["responseId"] for o in parsed["responses"]]
    assert "sys" in ids, "the system entry must be offerable, or the router can never return it"


# ------------------------------------------------------------------ request


def test_request_uses_strict_structured_output_and_the_generated_schema():
    _, urlopen = _route(_respond({"choices": [{"message": {"content": json.dumps({RESPONSE_KEY: "company", "confidence": 0.9})}}]}))
    sent = json.loads(urlopen.call_args[0][0].data)
    fmt = sent["response_format"]
    assert fmt["type"] == "json_schema"
    extra = fmt["json_schema"]["schema"]
    assert fmt["json_schema"]["strict"] is True, "without strict this is not Structured Outputs at all"
    assert fmt["json_schema"]["schema"]["required"] == [RESPONSE_KEY, "confidence"]
    assert sent["model"] == "gpt-4.1-nano"
    assert sent["temperature"] == 0


def test_valid_reply_routes_and_records_telemetry():
    decision, _ = _route(_respond({"choices": [{"message": {"content": json.dumps({RESPONSE_KEY: "company", "confidence": 0.9})}}]}))
    assert decision.response_id == "company"
    assert decision.backend == "openai"
    assert decision.latency_ms is not None


# ------------------------------------------------------------------ refusals


def test_a_refusal_becomes_a_fault_not_a_response():
    # A refusal is HTTP 200 with `refusal` set and no content. It must never
    # become a plausible id — the caller would speak the invention out loud.
    body = {"choices": [{"message": {"refusal": "I can't help with that"}}]}
    with pytest.raises(RouterFault, match="declined"):
        _route(_respond(body))


def test_empty_content_is_a_fault():
    body = {"choices": [{"message": {"content": "   "}}]}
    with pytest.raises(RouterFault, match="empty content"):
        _route(_respond(body))


def test_non_json_content_is_a_fault():
    body = {"choices": [{"message": {"content": "company"}}]}
    with pytest.raises(RouterFault, match="not JSON"):
        _route(_respond(body))


def test_out_of_set_label_from_the_provider_is_still_a_fault():
    # Strict mode should make this impossible, but the adapter must not trust
    # that: a contract violation is a contract violation.
    body = {"choices": [{"message": {"content": json.dumps({RESPONSE_KEY: "invented"})}}]}
    with pytest.raises(RouterFault, match="not one of the offered options"):
        _route(_respond(body))


def test_empty_choices_is_a_fault():
    with pytest.raises(RouterFault, match="empty content"):
        _route(_respond({"choices": []}))


# --------------------------------------------------------------------- retry


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("u", code, "m", {}, None)


def test_a_400_is_never_retried():
    # Against our own runtime-generated schema, a 400 is a builder bug.
    # Retrying turns a loud, correct failure into a slow, misleading one.
    with mock.patch("urllib.request.urlopen", side_effect=_http_error(400)) as m:
        with pytest.raises(RouterError, match="HTTP 400"):
            asyncio.run(OpenAIResponseRouter(api_key="k").route(agent_transcript="x", catalog=_catalog()))
    assert m.call_count == 1, "a 400 must be attempted exactly once"


def test_a_429_is_retried_exactly_once_then_surfaces():
    with mock.patch("urllib.request.urlopen", side_effect=_http_error(429)) as m:
        with pytest.raises(RouterError, match="HTTP 429"):
            asyncio.run(OpenAIResponseRouter(api_key="k").route(agent_transcript="x", catalog=_catalog()))
    assert m.call_count == 2, "one try plus one retry"


def test_a_5xx_is_retried_exactly_once():
    with mock.patch("urllib.request.urlopen", side_effect=_http_error(503)) as m:
        with pytest.raises(RouterError):
            asyncio.run(OpenAIResponseRouter(api_key="k").route(agent_transcript="x", catalog=_catalog()))
    assert m.call_count == 2


def test_an_unreachable_transport_is_retried_once():
    with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("down")) as m:
        with pytest.raises(RouterError, match="unreachable"):
            asyncio.run(OpenAIResponseRouter(api_key="k").route(agent_transcript="x", catalog=_catalog()))
    assert m.call_count == 2
