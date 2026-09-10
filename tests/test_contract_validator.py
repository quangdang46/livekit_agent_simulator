"""P0-2a: Caller Contract Validator (deterministic layer) tests.

Each check is a separate test function with detailed failure context in the
assertion message (via ValidationResult.details) so failures are debuggable
without re-reading the source.

Enforcement boundary under test: "unvalidated utterance never reaches
LiveKit" — every test below proves the validator, not the caller, decides
what may pass.
"""

from __future__ import annotations

import logging

import pytest

from livekit_agent_simulator.caller_contract import (
    BehaviorContract,
    CandidateUtterance,
    ContractConstraints,
    FailureReason,
    GenerationIdentity,
    Verdict,
)
from livekit_agent_simulator.caller_contract.validator import (
    ContractValidator,
    ends_call_allowed,
    validate_with_retry,
)

logger = logging.getLogger(__name__)


def _identity(behavior_id: str = "b2", turn_id: int = 1, generation_id: int = 1) -> GenerationIdentity:
    return GenerationIdentity(behavior_id=behavior_id, turn_id=turn_id, generation_id=generation_id)


def _negotiate_contract(**overrides) -> BehaviorContract:
    constraints = ContractConstraints(
        max_turns=3,
        max_budget=30000,
        forbidden_intents=["financing", "trade_in", "vehicle_change"],
        must_not=["invent_facts", "end_call"],
        **overrides,
    )
    return BehaviorContract(behavior="negotiate", target="price", constraints=constraints)


def test_valid_pass() -> None:
    validator = ContractValidator()
    contract = _negotiate_contract()
    candidate = CandidateUtterance(
        act="negotiate",
        target="price",
        slots={"max_budget": 30000},
        utterance="Would you be able to come down to $30,000?",
        identity=_identity(),
    )
    result = validator.validate(candidate, contract)
    assert result.verdict == Verdict.VALID, result.details


def test_act_mismatch_reject() -> None:
    validator = ContractValidator()
    contract = _negotiate_contract()
    candidate = CandidateUtterance(
        act="ask",
        target="price",
        slots={},
        utterance="What's the price?",
        identity=_identity(),
    )
    result = validator.validate(candidate, contract)
    assert result.verdict == Verdict.INVALID
    assert result.reason == "ACT_MISMATCH"


def test_target_mismatch_reject() -> None:
    validator = ContractValidator()
    contract = _negotiate_contract()
    candidate = CandidateUtterance(
        act="negotiate",
        target="mileage",
        slots={},
        utterance="Can you come down on the mileage?",
        identity=_identity(),
    )
    result = validator.validate(candidate, contract)
    assert result.verdict == Verdict.INVALID
    assert result.reason == "TARGET_MISMATCH"


def test_slot_violation_reject() -> None:
    """Caller claims a budget slot above the contract's ceiling."""
    validator = ContractValidator()
    contract = _negotiate_contract()
    candidate = CandidateUtterance(
        act="negotiate",
        target="price",
        slots={"max_budget": 45000},
        utterance="Could you do $45,000?",
        identity=_identity(),
    )
    result = validator.validate(candidate, contract)
    assert result.verdict == Verdict.INVALID
    assert result.reason == "SLOT_VIOLATION"


def test_forbidden_intent_reject_canonical_financing_drift_vector() -> None:
    """Report's canonical vector: negotiate(price) drifting into financing."""
    validator = ContractValidator()
    contract = _negotiate_contract()
    candidate = CandidateUtterance(
        act="negotiate",
        target="price",
        slots={},
        utterance="Could you explain your financing options?",
        identity=_identity(),
    )
    result = validator.validate(candidate, contract)
    assert result.verdict == Verdict.INVALID
    assert result.reason == "FORBIDDEN_INTENT_DETECTED"
    assert result.details.get("intent") == "financing"


def test_forbidden_intent_nested_in_otherwise_valid_sentence() -> None:
    """Report §28.3(11): primary intent still negotiate, but a forbidden
    intent is nested inside the sentence — must still reject.
    """
    validator = ContractValidator()
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


def test_end_call_guard_rejects_goodbye_when_not_end_behavior() -> None:
    validator = ContractValidator()
    contract = _negotiate_contract()
    candidate = CandidateUtterance(
        act="negotiate",
        target="price",
        slots={},
        utterance="Thanks, that's all I needed. Goodbye.",
        identity=_identity(),
    )
    result = validator.validate(candidate, contract)
    assert result.verdict == Verdict.INVALID
    assert result.reason == "END_CALL_NOT_ALLOWED"


def test_end_call_allowed_when_contract_behavior_is_end() -> None:
    validator = ContractValidator()
    contract = BehaviorContract(behavior="end", target=None, constraints=ContractConstraints(max_turns=1))
    assert ends_call_allowed(contract) is True
    candidate = CandidateUtterance(
        act="end",
        target=None,
        slots={},
        utterance="Thanks, bye.",
        identity=_identity(behavior_id="b_end"),
    )
    result = validator.validate(candidate, contract)
    assert result.verdict == Verdict.VALID, result.details


def test_lexical_drift_reject_vehicle_change() -> None:
    validator = ContractValidator()
    contract = _negotiate_contract()
    candidate = CandidateUtterance(
        act="negotiate",
        target="price",
        slots={},
        utterance="Actually, I'm looking for a different car instead.",
        identity=_identity(),
    )
    result = validator.validate(candidate, contract)
    assert result.verdict == Verdict.INVALID
    assert result.reason == "FORBIDDEN_INTENT_DETECTED"
    assert result.details.get("intent") == "vehicle_change"


def test_utterance_too_long_rejected() -> None:
    validator = ContractValidator()
    contract = _negotiate_contract(max_words=5)
    candidate = CandidateUtterance(
        act="negotiate",
        target="price",
        slots={},
        utterance="Would you be able to come down to thirty thousand dollars please",
        identity=_identity(),
    )
    result = validator.validate(candidate, contract)
    assert result.verdict == Verdict.INVALID
    assert result.reason == "UTTERANCE_TOO_LONG"


def test_schema_invalid_candidate_rejected() -> None:
    validator = ContractValidator()
    contract = _negotiate_contract()
    candidate = CandidateUtterance(
        act="negotiate",
        target="price",
        slots={},
        utterance="",  # invalid: empty utterance
        identity=_identity(),
    )
    result = validator.validate(candidate, contract)
    assert result.verdict == Verdict.INVALID
    assert result.reason is not None and result.reason.startswith("SCHEMA_INVALID")


def test_retry_then_fail_path_maps_to_caller_behavior_violation() -> None:
    """Bounded retry: always-invalid generator exhausts retries then fails.

    The failure must be attributable to FailureReason.CALLER_BEHAVIOR_VIOLATION
    (never silently published).
    """
    validator = ContractValidator()
    contract = _negotiate_contract()

    def always_off_topic() -> CandidateUtterance:
        return CandidateUtterance(
            act="ask",
            target="financing",
            slots={},
            utterance="Do you offer financing?",
            identity=_identity(generation_id=1),
        )

    outcome = validate_with_retry(validator, contract, always_off_topic, max_retries=2)
    assert outcome.attempts == 3  # 1 initial + 2 retries
    assert outcome.result.verdict != Verdict.VALID
    assert outcome.candidate is None, "rejected candidate must never be returned for publish"
    # Caller code maps any non-VALID exhausted-retry outcome to this reason:
    assert FailureReason.CALLER_BEHAVIOR_VIOLATION.value == "CALLER_BEHAVIOR_VIOLATION"


def test_retry_succeeds_on_second_attempt() -> None:
    contract = _negotiate_contract()
    validator = ContractValidator()
    attempts_made: list[int] = []

    def flaky_then_valid() -> CandidateUtterance:
        attempts_made.append(1)
        if len(attempts_made) == 1:
            return CandidateUtterance(
                act="ask", target="financing", slots={}, utterance="financing?", identity=_identity(generation_id=1)
            )
        return CandidateUtterance(
            act="negotiate",
            target="price",
            slots={"max_budget": 30000},
            utterance="Could you do $30,000?",
            identity=_identity(generation_id=2),
        )

    outcome = validate_with_retry(validator, contract, flaky_then_valid, max_retries=2)
    assert outcome.result.is_valid()
    assert outcome.candidate is not None
    assert outcome.attempts == 2


class _FakeSemanticVerifier:
    """Minimal stand-in for the P0-2b backend used to test the seam.

    Mirrors the real tier contract: observed.target=None means "no
    independent target evidence" (what the rule baseline always returns);
    pass observed_target explicitly to simulate a tier-(2)/(3) backend that
    actually derived a target from the utterance.
    """

    def __init__(
        self,
        observed_act: str,
        confidence: float = 0.9,
        observed_target: str | None = "__unset__",  # type: ignore[assignment]
    ) -> None:
        self._observed_act = observed_act
        self._confidence = confidence
        self._observed_target = observed_target

    def classify(self, utterance, contract):  # noqa: ANN001 — test double
        from livekit_agent_simulator.caller_contract import ObservedAct

        target = contract.target if self._observed_target == "__unset__" else self._observed_target
        return ObservedAct(act=self._observed_act, target=target, confidence=self._confidence)


def test_semantic_verifier_seam_can_reject_when_lexical_check_misses() -> None:
    """A semantic backend can catch a paraphrase the lexical baseline misses."""
    validator = ContractValidator(semantic_verifier=_FakeSemanticVerifier(observed_act="ask"))
    contract = _negotiate_contract()
    candidate = CandidateUtterance(
        act="negotiate",
        target="price",
        slots={},
        utterance="I'm wondering about payment options for this car.",  # not in lexicon
        identity=_identity(),
    )
    result = validator.validate(candidate, contract)
    assert result.verdict in {Verdict.INVALID, Verdict.UNKNOWN}
    assert result.reason == "SEMANTIC_ACT_MISMATCH"


def test_semantic_verifier_failure_yields_error_not_valid() -> None:
    """A crashing/unavailable verifier must map to ERROR, never VALID."""

    class BrokenVerifier:
        def classify(self, utterance, contract):  # noqa: ANN001
            raise RuntimeError("backend unavailable")

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


def test_semantic_target_mismatch_rejects_when_backend_supplies_evidence() -> None:
    """A tier-(2)/(3) verifier that actually derived a target from the
    utterance (observed.target set, disagreeing with the contract) must
    reject with SEMANTIC_TARGET_MISMATCH -- the branch the rule baseline
    never triggers because it always returns target=None."""
    validator = ContractValidator(
        semantic_verifier=_FakeSemanticVerifier(observed_act="negotiate", observed_target="delivery_date")
    )
    contract = _negotiate_contract()  # negotiate / price
    candidate = CandidateUtterance(
        act="negotiate",
        target="price",
        slots={},
        utterance="Would you consider changing the delivery date?",
        identity=_identity(),
    )
    result = validator.validate(candidate, contract)
    assert result.verdict == Verdict.INVALID
    assert result.reason == "SEMANTIC_TARGET_MISMATCH"
    assert result.details["expected_target"] == "price"
    assert result.details["observed_target"] == "delivery_date"


def test_rule_baseline_none_target_never_triggers_target_mismatch() -> None:
    """The rule baseline returns observed.target=None (no independent target
    evidence), so the SEMANTIC_TARGET_MISMATCH branch must stay a no-op for
    it -- target enforcement for that tier rests on the deterministic claim
    check (step 3), which still applies."""
    validator = ContractValidator(
        semantic_verifier=_FakeSemanticVerifier(observed_act="negotiate", observed_target=None)
    )
    contract = _negotiate_contract()
    candidate = CandidateUtterance(
        act="negotiate",
        target="price",
        slots={"max_budget": 30000},
        utterance="Could you do $30,000?",
        identity=_identity(),
    )
    result = validator.validate(candidate, contract)
    assert result.is_valid(), result.details
