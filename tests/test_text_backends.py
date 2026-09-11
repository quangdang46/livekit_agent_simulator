"""Slice 2 acceptance: OpenAITextBackend / GeminiTextBackend generate ONLY a
candidate dict via a stateless HTTP call — they carry no publish/flow-control
capability at all (no room, no mixer, no orchestrator reference).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from livekit_agent_simulator.caller_contract.dsl import parse_steps
from livekit_agent_simulator.caller_contract.language_adapter import (
    AILanguageAdapter,
    build_context,
)
from livekit_agent_simulator.caller_contract.orchestrator import Orchestrator
from livekit_agent_simulator.caller_contract.semantic import RuleBasedSemanticVerifier
from livekit_agent_simulator.caller_contract.text_backends import (
    GeminiTextBackend,
    LanguageBackendError,
    OpenAITextBackend,
)
from livekit_agent_simulator.caller_contract.validator import ContractValidator


def _fake_http_response(body: bytes):
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return body

    return _Resp()


def test_openai_text_backend_returns_only_candidate_dict():
    """The backend has no room/mixer/orchestrator attribute — structurally it
    cannot publish audio or control flow (boundary check via introspection)."""
    backend = OpenAITextBackend(api_key="sk-test")
    assert not hasattr(backend, "room")
    assert not hasattr(backend, "publish")
    assert not hasattr(backend, "mixer")
    assert not hasattr(backend, "orchestrator")

    payload = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "act": "negotiate",
                            "target": "price",
                            "slots": {"max_budget": 30000},
                            "utterance": "Would you be able to come down to $30,000?",
                        }
                    )
                }
            }
        ]
    }
    with patch("urllib.request.urlopen", return_value=_fake_http_response(json.dumps(payload).encode())):
        result = backend.generate({"current_behavior": {"act": "negotiate", "target": "price"}})

    assert result == {
        "act": "negotiate",
        "target": "price",
        "slots": {"max_budget": 30000},
        "utterance": "Would you be able to come down to $30,000?",
    }


def test_gemini_text_backend_returns_only_candidate_dict():
    backend = GeminiTextBackend(api_key="AQ.test")
    assert not hasattr(backend, "room")
    assert not hasattr(backend, "publish")

    payload = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "text": json.dumps(
                                {
                                    "act": "ask",
                                    "target": None,
                                    "slots": {},
                                    "utterance": "Could you tell me the asking price?",
                                }
                            )
                        }
                    ]
                }
            }
        ]
    }
    with patch("urllib.request.urlopen", return_value=_fake_http_response(json.dumps(payload).encode())):
        result = backend.generate({"current_behavior": {"act": "ask", "target": None}})

    assert result["act"] == "ask"
    assert result["utterance"] == "Could you tell me the asking price?"


def test_malformed_backend_response_raises_language_backend_error():
    backend = OpenAITextBackend(api_key="sk-test")
    payload = {"choices": [{"message": {"content": "not json at all"}}]}
    with patch("urllib.request.urlopen", return_value=_fake_http_response(json.dumps(payload).encode())):
        with pytest.raises(LanguageBackendError):
            backend.generate({"current_behavior": {"act": "ask", "target": None}})


def test_text_backend_wired_through_adapter_and_validator_end_to_end():
    """Full boundary chain: TextBackend -> AILanguageAdapter -> CandidateUtterance
    -> ContractValidator. The backend never sees the validator or contract
    beyond the context dict it was given; validation still gates publish."""
    orch = Orchestrator()
    orch.start_behavior()
    orch.advance_caller_turn()
    identity = orch.new_generation()

    actions = parse_steps(
        [{"do": {"behavior": "negotiate", "target": "price", "constraints": {"max_budget": 30000}}}],
        file="t",
    )
    contract = actions[0].contract
    context = build_context(contract=contract, turn=0, agent_latest=None, relevant_facts=[], recent_turns=[])

    backend = OpenAITextBackend(api_key="sk-test")
    payload = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "act": "negotiate",
                            "target": "price",
                            "slots": {"max_budget": 30000},
                            "utterance": "Would you be able to come down to $30,000?",
                        }
                    )
                }
            }
        ]
    }
    with patch("urllib.request.urlopen", return_value=_fake_http_response(json.dumps(payload).encode())):
        adapter = AILanguageAdapter(backend=backend)
        candidate = adapter.generate_candidate(contract, context, identity)

    validator = ContractValidator(semantic_verifier=RuleBasedSemanticVerifier())
    result = validator.validate(candidate, contract)
    assert result.is_valid()


def test_system_prompt_requires_naming_the_target_topic():
    """Run-025 regression: the generator dropped the topic word entirely
    ("I'm asking about the 2022 Honda CR-V." for target=price) and the
    validator correctly failed it closed as TARGET_UNVERIFIED. The prompt
    must tell the model the target has to be LEXICALLY present — shaping
    generation, not loosening validation."""
    from livekit_agent_simulator.caller_contract.text_backends import _SYSTEM_PROMPT

    assert "current_behavior.target" in _SYSTEM_PROMPT
    assert "MUST name that topic" in _SYSTEM_PROMPT
    # The constraint is lexical presence, not just staying "on-topic".
    assert "paraphrase the topic away" in _SYSTEM_PROMPT
