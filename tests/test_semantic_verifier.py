"""P0-2b: Semantic Intent Verification backend tests.

Covers: multi-act utterance reject, nested forbidden intent inside an
otherwise-valid sentence reject, paraphrase-with-same-act accept,
confidence-below-threshold rejects, and backend-error maps to ERROR verdict
(never VALID). No new required dependency for the default path (pure
stdlib rule/lexical baseline, tier 1 of the layered plan in report §17.1).
"""

from __future__ import annotations

import pytest

from livekit_agent_simulator.caller_contract import (
    BehaviorContract,
    CandidateUtterance,
    ContractConstraints,
    GenerationIdentity,
    Verdict,
)
from livekit_agent_simulator.caller_contract.semantic import RuleBasedSemanticVerifier
from livekit_agent_simulator.caller_contract.validator import (
    SEMANTIC_CONFIDENCE_THRESHOLD,
    ContractValidator,
)


def _negotiate_contract() -> BehaviorContract:
    constraints = ContractConstraints(
        max_turns=3,
        max_budget=30000,
        forbidden_intents=["financing", "trade_in", "vehicle_change"],
        must_not=["invent_facts", "end_call"],
    )
    return BehaviorContract(behavior="negotiate", target="price", constraints=constraints)


def _identity() -> GenerationIdentity:
    return GenerationIdentity(behavior_id="b2", turn_id=1, generation_id=1)


# ---------------------------------------------------------------------------
# Pure classifier unit tests (no validator involved)
# ---------------------------------------------------------------------------


def test_multi_act_utterance_detected() -> None:
    """Report §28.3(10): 'Could you lower the price, and by the way do you
    offer financing?' must surface BOTH the primary act and the secondary
    forbidden intent in all_acts.
    """
    verifier = RuleBasedSemanticVerifier()
    contract = _negotiate_contract()
    observed = verifier.classify(
        "Could you lower the price, and by the way do you offer financing?", contract
    )
    assert observed.act == "negotiate"
    assert "financing" in observed.all_acts
    assert observed.confidence >= SEMANTIC_CONFIDENCE_THRESHOLD


def test_nested_forbidden_intent_inside_valid_sentence_detected() -> None:
    """Report §28.3(11): primary intent is still negotiate, but 'financing'
    is nested inside the sentence and must still be surfaced.
    """
    verifier = RuleBasedSemanticVerifier()
    contract = _negotiate_contract()
    observed = verifier.classify(
        "$30,000 is really my limit, although I could consider financing.", contract
    )
    assert "financing" in observed.all_acts


def test_paraphrase_with_same_act_has_high_confidence_and_no_forbidden_tag() -> None:
    """A paraphrase of the SAME act must classify confidently with no
    forbidden intent leaking in — proves the classifier isn't just keying
    off superficial word overlap with the forbidden lexicon.
    """
    verifier = RuleBasedSemanticVerifier()
    contract = _negotiate_contract()
    observed = verifier.classify(
        "Could you meet me at $30,000, a little closer to my number?", contract
    )
    assert observed.act == "negotiate"
    assert observed.confidence >= SEMANTIC_CONFIDENCE_THRESHOLD
    assert "financing" not in observed.all_acts
    assert "trade_in" not in observed.all_acts
    assert "vehicle_change" not in observed.all_acts


def test_confidence_below_threshold_for_unmatched_utterance() -> None:
    """Genuinely ambiguous text (no act pattern hits) must return a
    confidence BELOW the validator's reject threshold — never fabricate
    confidence just because it happens to equal contract.behavior.
    """
    verifier = RuleBasedSemanticVerifier()
    contract = _negotiate_contract()
    observed = verifier.classify(
        "Well, that's an interesting thought about the weather today.", contract
    )
    assert observed.confidence < SEMANTIC_CONFIDENCE_THRESHOLD


def test_classifier_never_trusts_candidate_only_utterance_text() -> None:
    """Structural proof of report §17.3: classify() takes only the raw
    utterance string and the contract — there is no way to pass it a
    CandidateUtterance or its claimed act/target/slots.
    """
    import inspect

    sig = inspect.signature(RuleBasedSemanticVerifier.classify)
    param_names = list(sig.parameters.keys())
    assert param_names == ["self", "utterance", "contract"]


# ---------------------------------------------------------------------------
# End-to-end through ContractValidator (integration with P0-2a)
# ---------------------------------------------------------------------------


def test_multi_act_utterance_reject_via_validator() -> None:
    validator = ContractValidator(semantic_verifier=RuleBasedSemanticVerifier())
    contract = _negotiate_contract()
    candidate = CandidateUtterance(
        act="negotiate",
        target="price",
        slots={"max_budget": 30000},
        utterance="Could you lower the price, and by the way do you offer financing?",
        identity=_identity(),
    )
    result = validator.validate(candidate, contract)
    # The P0-2a lexical guard (step 7) already catches "financing" before the
    # semantic layer runs; either layer catching it satisfies the invariant
    # that this utterance never reaches TTS/LiveKit.
    assert result.verdict == Verdict.INVALID
    assert result.reason == "FORBIDDEN_INTENT_DETECTED"


def test_nested_forbidden_intent_reject_via_validator() -> None:
    validator = ContractValidator(semantic_verifier=RuleBasedSemanticVerifier())
    contract = _negotiate_contract()
    candidate = CandidateUtterance(
        act="negotiate",
        target="price",
        slots={"max_budget": 30000},
        utterance="$30,000 is really my limit, although I could consider financing.",
        identity=_identity(),
    )
    result = validator.validate(candidate, contract)
    assert result.verdict == Verdict.INVALID
    assert result.reason == "FORBIDDEN_INTENT_DETECTED"


def test_paraphrase_with_same_act_accept_via_validator() -> None:
    validator = ContractValidator(semantic_verifier=RuleBasedSemanticVerifier())
    contract = _negotiate_contract()
    candidate = CandidateUtterance(
        act="negotiate",
        target="price",
        slots={"max_budget": 30000},
        utterance="Could you meet me at $30,000, a little closer to my number?",
        identity=_identity(),
    )
    result = validator.validate(candidate, contract)
    assert result.verdict == Verdict.VALID, result.details


def test_confidence_below_threshold_rejects_via_validator() -> None:
    validator = ContractValidator(semantic_verifier=RuleBasedSemanticVerifier())
    contract = _negotiate_contract()
    candidate = CandidateUtterance(
        act="negotiate",
        target="price",
        slots={"max_budget": 30000},
        utterance="Well, that's an interesting thought about the weather today.",
        identity=_identity(),
    )
    result = validator.validate(candidate, contract)
    assert result.verdict == Verdict.UNKNOWN
    assert result.reason == "LOW_CONFIDENCE"


def test_backend_error_maps_to_error_verdict_never_valid() -> None:
    """A crashing/unavailable semantic backend must map to ERROR, and ERROR
    must never be conflated with VALID (report §28.3(23)).
    """

    class BrokenVerifier:
        def classify(self, utterance: str, contract: BehaviorContract):
            raise RuntimeError("semantic backend unavailable")

    validator = ContractValidator(semantic_verifier=BrokenVerifier())
    contract = _negotiate_contract()
    candidate = CandidateUtterance(
        act="negotiate",
        target="price",
        slots={"max_budget": 30000},
        utterance="Could you do $30,000?",
        identity=_identity(),
    )
    result = validator.validate(candidate, contract)
    assert result.verdict == Verdict.ERROR
    assert not result.is_valid()


@pytest.mark.parametrize(
    "utterance",
    [
        "What's the price?",
        "That sounds good, I'll take it.",
        "No thanks, that won't work for me.",
    ],
)
def test_classifier_distinguishes_common_acts(utterance: str) -> None:
    """Sanity sweep across a few distinct acts to guard against the
    classifier collapsing everything to one bucket.
    """
    verifier = RuleBasedSemanticVerifier()
    contract = _negotiate_contract()
    observed = verifier.classify(utterance, contract)
    assert observed.confidence >= SEMANTIC_CONFIDENCE_THRESHOLD
