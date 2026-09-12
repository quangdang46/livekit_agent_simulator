"""Slice 2 prototype: Tier-3 LLM judge (mocked HTTP, no network, no caller path).

Benchmarks the exact capabilities golden_cases.json locks: TARGET evidence,
paraphrased forbidden-intent generalization, evidence-capped confidence, and
transport-failure mapping. All HTTP is mocked via urllib.request.urlopen.
"""

from __future__ import annotations

import io
import json
from unittest.mock import patch

import pytest
import urllib.error

from livekit_agent_simulator.caller_contract import BehaviorContract, ContractConstraints
from livekit_agent_simulator.caller_contract.semantic_llm import (
    LLMSemanticVerifier,
    SemanticJudgeError,
    build_observed_act,
)
from livekit_agent_simulator.caller_contract.validator import ContractValidator


def _negotiate_contract() -> BehaviorContract:
    return BehaviorContract(
        behavior="negotiate",
        target="price",
        constraints=ContractConstraints(max_turns=3, forbidden_intents=["financing"]),
    )


def _mock_reply(payload: dict) -> io.BytesIO:
    return io.BytesIO(
        json.dumps({"choices": [{"message": {"content": json.dumps(payload)}}]}).encode()
    )


class _Resp:
    def __init__(self, buf: io.BytesIO):
        self._buf = buf

    def read(self) -> bytes:
        return self._buf.read()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _run_judge(utterance: str, judge_payload: dict, contract=None):
    contract = contract or _negotiate_contract()
    verifier = LLMSemanticVerifier(api_key="test-key")
    with patch("urllib.request.urlopen", return_value=_Resp(_mock_reply(judge_payload))):
        return verifier.classify(utterance, contract)


def test_target_evidence_from_utterance_not_contract():
    """Golden TARGET gap: delivery-date utterance yields target=delivery_date."""
    utterance = "Could you deliver it next Saturday?"
    observed = _run_judge(
        utterance,
        {"act": "negotiate", "target": "delivery_date",
         "all_acts": ["negotiate"], "confidence": 0.85,
         "evidence": "deliver it next Saturday"},
    )
    assert observed.act == "negotiate"
    assert observed.target == "delivery_date"
    assert observed.confidence >= 0.5


def test_target_mismatch_rejects_for_right_reason():
    utterance = "Could you deliver it next Saturday?"
    candidate_claim = {"act": "negotiate", "target": "price"}
    from livekit_agent_simulator.caller_contract import (
        CandidateUtterance,
        GenerationIdentity,
    )

    verifier = LLMSemanticVerifier(api_key="test-key")
    judge_payload = {"act": "negotiate", "target": "delivery_date",
                     "all_acts": ["negotiate"], "confidence": 0.85,
                     "evidence": "deliver it next Saturday"}
    with patch("urllib.request.urlopen", return_value=_Resp(_mock_reply(judge_payload))):
        validator = ContractValidator(semantic_verifier=verifier)
        candidate = CandidateUtterance(
            act=candidate_claim["act"], target=candidate_claim["target"],
            slots={}, utterance=utterance,
            identity=GenerationIdentity(behavior_id="g", turn_id=1, generation_id=1),
        )
        result = validator.validate(candidate, _negotiate_contract())
    assert not result.is_valid()
    assert result.reason == "SEMANTIC_TARGET_MISMATCH"


def test_paraphrased_financing_detected_without_keyword():
    """Golden FORBIDDEN gap: 'pay off over several months' -> financing."""
    utterance = "Could I pay this off over several months rather than all at once?"
    observed = _run_judge(
        utterance,
        {"act": "negotiate", "target": "price",
         "all_acts": ["negotiate", "financing"], "confidence": 0.8,
         "evidence": "pay this off over several months"},
    )
    assert "financing" in observed.all_acts


def test_missing_evidence_caps_confidence_below_threshold():
    """Judge claim without a verbatim quote fails CLOSED (LOW_CONFIDENCE)."""
    observed = build_observed_act(
        {"act": "negotiate", "target": "price", "all_acts": ["negotiate"],
         "confidence": 0.95, "evidence": "a phrase never spoken here"},
        "Would you consider $30,000?",
    )
    assert observed.confidence < 0.5


def test_malformed_payload_raises_never_fabricates():
    with pytest.raises(SemanticJudgeError):
        build_observed_act({"target": "price", "confidence": 0.9}, "Hello?")
    with pytest.raises(SemanticJudgeError):
        build_observed_act(
            {"act": "negotiate", "confidence": 9.9, "evidence": "negotiate"}, "Hello?"
        )


def test_transport_failure_raises_for_error_mapping():
    verifier = LLMSemanticVerifier(api_key="test-key")
    with patch(
        "urllib.request.urlopen",
        side_effect=urllib.error.URLError("down"),
    ):
        with pytest.raises(SemanticJudgeError):
            verifier.classify("Hello?", _negotiate_contract())


def test_judge_error_maps_to_validator_error_never_valid():
    verifier = LLMSemanticVerifier(api_key="test-key")
    from livekit_agent_simulator.caller_contract import (
        CandidateUtterance,
        GenerationIdentity,
    )

    with patch(
        "urllib.request.urlopen",
        side_effect=urllib.error.URLError("down"),
    ):
        validator = ContractValidator(semantic_verifier=verifier)
        candidate = CandidateUtterance(
            act="negotiate", target="price", slots={}, utterance="Hello?",
            identity=GenerationIdentity(behavior_id="g", turn_id=1, generation_id=1),
        )
        result = validator.validate(candidate, _negotiate_contract())
    assert result.verdict.value == "ERROR"
    assert result.is_valid() is False


def test_text_only_boundary_no_publish_or_flow():
    verifier = LLMSemanticVerifier(api_key="test-key")
    for attr in ("room", "publish", "mixer", "orchestrator", "bridge", "run", "speak"):
        assert not hasattr(verifier, attr), f"judge must not have {attr}"
