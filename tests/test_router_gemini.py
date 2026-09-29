"""Tests for the Gemini decision-router adapter (response-router v2-7).

The three degenerate Gemini bodies are the reason this file exists. They all
present as "no usable text" but they are different failures, and collapsing
them into one generic error sends whoever reads the report to the wrong place.
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
from livekit_agent_simulator.caller_contract.router_gemini import (
    GeminiResponseRouter,
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
    m = mock.MagicMock()
    m.return_value.__enter__.return_value.read.return_value = json.dumps(payload).encode()
    return m


def _route(responses, catalog=None, transcript="What is your company name?"):
    router = GeminiResponseRouter(api_key="k")
    with mock.patch("urllib.request.urlopen", responses):
        return asyncio.run(router.route(agent_transcript=transcript, catalog=catalog or _catalog())), responses


def _candidates(text: str | None, **extra) -> dict:
    parts = [{"text": text}] if text else []
    return {"candidates": [{"content": {"parts": parts}, **extra}]}


# ------------------------------------------------------------------- prompt


def test_prompt_never_carries_the_authored_answer_text():
    body = _user_prompt("What is your company name?", _catalog())
    assert ANSWER not in body and "Bluebird" not in body
    for option in json.loads(body)["responses"]:
        assert "text" not in option


def test_prompt_does_not_restate_the_schema():
    # Gemini specifically penalises a schema restated in the prompt.
    body = _user_prompt("x", _catalog())
    assert "responseSchema" not in body
    assert '"required"' not in body


# ------------------------------------------------------------------ request


def test_request_pins_required_and_the_schema():
    _, urlopen = _route(_respond(_candidates(json.dumps({RESPONSE_KEY: "company"}))))
    cfg = json.loads(urlopen.call_args[0][0].data)["generationConfig"]
    assert cfg["responseMimeType"] == "application/json"
    schema = cfg["responseSchema"]
    # Absent `required` is exactly what makes an empty {} reachable on Gemini.
    assert schema["required"] == [RESPONSE_KEY]
    assert cfg["maxOutputTokens"] > 0, "a thinking budget must not starve the reply"


def test_valid_reply_routes_and_records_telemetry():
    decision, _ = _route(_respond(_candidates(json.dumps({RESPONSE_KEY: "company"}))))
    assert decision.response_id == "company"
    assert decision.backend == "gemini"
    assert decision.latency_ms is not None


# --------------------------------------------------- the three degenerate bodies


def test_a_blocked_prompt_is_named_as_a_block_not_a_generic_failure():
    with pytest.raises(RouterFault, match="prompt blocked"):
        _route(_respond({"promptFeedback": {"blockReason": "SAFETY"}, "candidates": []}))


def test_empty_candidates_is_named():
    with pytest.raises(RouterFault, match="empty candidates"):
        _route(_respond({"candidates": []}))


def test_the_empty_object_body_is_rejected():
    # {} means `required` was dropped upstream; parsing it as a decision would
    # be inventing one.
    with pytest.raises(RouterFault, match="not one of the offered options|no 'responseId'"):
        _route(_respond(_candidates("{}")))


def test_empty_parts_is_rejected():
    with pytest.raises(RouterFault, match="no text content"):
        _route(_respond(_candidates(None)))


def test_a_finish_reason_is_named_when_there_is_no_text():
    # A budget eaten by thinking looks identical to a routing failure unless
    # the reason travels with it.
    with pytest.raises(RouterFault, match="finishReason=MAX_TOKENS"):
        _route(_respond(_candidates(None, finishReason="MAX_TOKENS")))


def test_non_json_text_is_rejected():
    with pytest.raises(RouterFault, match="not JSON"):
        _route(_respond(_candidates("company")))


def test_out_of_set_label_is_still_a_fault():
    with pytest.raises(RouterFault, match="not one of the offered options"):
        _route(_respond(_candidates(json.dumps({RESPONSE_KEY: "invented"}))))


# --------------------------------------------------------------------- retry


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("u", code, "m", {}, None)


def test_a_400_is_never_retried():
    with mock.patch("urllib.request.urlopen", side_effect=_http_error(400)) as m:
        with pytest.raises(RouterError, match="HTTP 400"):
            asyncio.run(GeminiResponseRouter(api_key="k").route(agent_transcript="x", catalog=_catalog()))
    assert m.call_count == 1


def test_a_429_is_retried_exactly_once():
    with mock.patch("urllib.request.urlopen", side_effect=_http_error(429)) as m:
        with pytest.raises(RouterError, match="HTTP 429"):
            asyncio.run(GeminiResponseRouter(api_key="k").route(agent_transcript="x", catalog=_catalog()))
    assert m.call_count == 2


def test_an_unreachable_transport_is_retried_once():
    with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("down")) as m:
        with pytest.raises(RouterError, match="unreachable"):
            asyncio.run(GeminiResponseRouter(api_key="k").route(agent_transcript="x", catalog=_catalog()))
    assert m.call_count == 2
