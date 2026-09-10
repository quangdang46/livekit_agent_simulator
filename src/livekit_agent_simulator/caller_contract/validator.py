"""P0-2a: Caller Contract Validator — deterministic layer.

This is the single enforcement choke point: no other module may publish
caller audio to LiveKit without a VALID verdict from here first. See epic
invariant 1: unvalidated utterance never reaches LiveKit.

Deterministic checks (this module, dependency-free/stdlib only):
    schema -> act -> target -> slots -> numeric constraints -> forbidden
    facts -> forbidden intents (lexical) -> goodbye/end control -> obvious
    lexical drift.

The semantic layer (P0-2b, SemanticVerifierProtocol) is optional and
swappable behind this same interface — plugging in a stronger backend must
never move the enforcement boundary defined here.

Retry policy: bounded (default 2 retries) via validate_with_retry(); when
still invalid after retries, the run must record CALLER_BEHAVIOR_VIOLATION
(FailureReason) and stop, never fall back to speaking an unvalidated line.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from . import (
    BehaviorContract,
    CandidateUtterance,
    ObservedAct,
    ValidationResult,
    Verdict,
)

# End/goodbye guard: lexical signal that the utterance is trying to close
# the call. Only enforced when the contract does NOT explicitly permit it
# via must_not absence of "end_call" is NOT a permission grant by itself —
# ending is only allowed when the contract's behavior itself is an end/hangup
# behavior (see ends_call_allowed()).
_END_CALL_PHRASES = (
    "goodbye",
    "bye",
    "thanks, that's all",
    "that's all i needed",
    "i'll let you go",
    "have a good day",
)

# Default lexical intent lexicon used when no SemanticVerifier is injected,
# and shared with the P0-2b RuleBasedSemanticVerifier (single source of
# truth for the baseline keyword tier). Deliberately small and conservative
# — this is the "obvious lexical drift" rule tier (report §17.1 row "obvious
# lexical drift"), not a replacement for a real semantic classifier.
DEFAULT_INTENT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "financing": ("financing", "finance option", "loan", "payment plan"),
    "trade_in": ("trade in", "trade-in", "trade my"),
    "vehicle_change": ("different car", "another vehicle", "looking for a toyota", "instead"),
}

# Below this confidence, the semantic verifier's classification is treated
# as ambiguous and MUST be rejected — never treated as a match just because
# observed.act happens to equal contract.behavior (report §14.1/§17.4:
# unknown/ambiguous is never valid).
SEMANTIC_CONFIDENCE_THRESHOLD = 0.5


def ends_call_allowed(contract: BehaviorContract) -> bool:
    """True only when the contract's own behavior is explicitly an end/hangup."""
    return contract.behavior.lower() in {"end", "hangup", "hang_up"}


class SemanticVerifierProtocol(Protocol):
    """Interface implemented by P0-2b. classify() re-derives the observed
    act/target/slots from the utterance text ALONE — it must never trust
    candidate.act/target/slots (those are generator claims, not evidence).
    """

    def classify(self, utterance: str, contract: BehaviorContract) -> ObservedAct: ...


def _lexical_forbidden_intent_hit(utterance: str, forbidden_intents: list[str]) -> str | None:
    """Baseline lexical drift check used when no semantic verifier is wired.

    Returns the matched forbidden intent name, or None.
    """
    text = utterance.lower()
    for intent in forbidden_intents:
        keywords = DEFAULT_INTENT_KEYWORDS.get(intent, (intent.replace("_", " "),))
        if any(kw in text for kw in keywords):
            return intent
    return None


def _word_count(text: str) -> int:
    return len([w for w in text.strip().split() if w])


class ContractValidator:
    """Deterministic Caller Contract Validator.

    Semantic verification is MANDATORY in every runtime path: when no
    explicit verifier is passed, the rule-based baseline (P0-2b) is used
    automatically. Passing ``semantic_verifier=None`` explicitly is only
    allowed for unit-testing the deterministic layer in isolation and must
    never be used on a real call path — an unvalidated utterance must never
    reach LiveKit (epic invariant 1).
    """

    def __init__(self, semantic_verifier: SemanticVerifierProtocol | None = None) -> None:
        if semantic_verifier is None:
            from .semantic import RuleBasedSemanticVerifier

            semantic_verifier = RuleBasedSemanticVerifier()
        self._semantic_verifier = semantic_verifier

    def validate(
        self,
        candidate: CandidateUtterance,
        contract: BehaviorContract,
    ) -> ValidationResult:
        # 1. Schema — malformed candidate/contract is INVALID, not ERROR.
        try:
            candidate.validate()
            contract.validate()
        except ValueError as exc:
            return ValidationResult(verdict=Verdict.INVALID, reason=f"SCHEMA_INVALID: {exc}")

        # 2. Act — the generator's claimed act must equal the contract behavior.
        if candidate.act != contract.behavior:
            return ValidationResult(
                verdict=Verdict.INVALID,
                reason="ACT_MISMATCH",
                details={"expected_act": contract.behavior, "observed_act": candidate.act},
            )

        # 3. Target — if the contract pins a target, the candidate must match it.
        if contract.target is not None and candidate.target != contract.target:
            return ValidationResult(
                verdict=Verdict.INVALID,
                reason="TARGET_MISMATCH",
                details={"expected_target": contract.target, "observed_target": candidate.target},
            )

        # 4. Slot / numeric constraints — e.g. a claimed max_budget above contract's ceiling.
        constraints = contract.constraints
        if constraints.max_budget is not None:
            claimed_budget = candidate.slots.get("max_budget")
            if claimed_budget is not None and float(claimed_budget) > float(constraints.max_budget):
                return ValidationResult(
                    verdict=Verdict.INVALID,
                    reason="SLOT_VIOLATION",
                    details={"max_budget": constraints.max_budget, "claimed": claimed_budget},
                )

        # 5. Utterance size constraints (report §28.3(9): valid but too long).
        if constraints.max_words is not None and _word_count(candidate.utterance) > constraints.max_words:
            return ValidationResult(
                verdict=Verdict.INVALID,
                reason="UTTERANCE_TOO_LONG",
                details={"max_words": constraints.max_words, "word_count": _word_count(candidate.utterance)},
            )

        # 6. Goodbye / end-call guard — never allowed unless the contract IS an end behavior.
        if "end_call" in constraints.must_not or not ends_call_allowed(contract):
            text = candidate.utterance.lower()
            if any(phrase in text for phrase in _END_CALL_PHRASES) and not ends_call_allowed(contract):
                return ValidationResult(
                    verdict=Verdict.INVALID,
                    reason="END_CALL_NOT_ALLOWED",
                    details={"utterance": candidate.utterance},
                )

        # 7. Forbidden intents — lexical drift baseline (always runs) …
        hit = _lexical_forbidden_intent_hit(candidate.utterance, constraints.forbidden_intents)
        if hit is not None:
            return ValidationResult(
                verdict=Verdict.INVALID,
                reason="FORBIDDEN_INTENT_DETECTED",
                details={"intent": hit, "utterance": candidate.utterance},
            )

        # 8. … then optionally the semantic verifier (P0-2b), which can catch
        #    paraphrases the lexical check misses (e.g. "Do you offer financing?").
        if self._semantic_verifier is not None:
            try:
                observed = self._semantic_verifier.classify(candidate.utterance, contract)
                observed.validate()
            except Exception as exc:  # noqa: BLE001 — verifier failure must never look like PASS
                return ValidationResult(verdict=Verdict.ERROR, reason=f"VERIFIER_UNAVAILABLE: {exc}")

            # Ambiguous/low-confidence classification is ALWAYS a reject,
            # regardless of whether observed.act happens to equal
            # contract.behavior — confidence gates correctness, not just act.
            if observed.confidence < SEMANTIC_CONFIDENCE_THRESHOLD:
                return ValidationResult(
                    verdict=Verdict.UNKNOWN,
                    reason="LOW_CONFIDENCE",
                    details={"confidence": observed.confidence, "threshold": SEMANTIC_CONFIDENCE_THRESHOLD},
                )

            all_acts = observed.all_acts or [observed.act]
            for act in all_acts:
                if act != contract.behavior and _matches_forbidden(act, constraints.forbidden_intents):
                    return ValidationResult(
                        verdict=Verdict.INVALID,
                        reason="FORBIDDEN_INTENT_DETECTED",
                        details={"intent": act, "utterance": candidate.utterance},
                    )

            if observed.act != contract.behavior:
                return ValidationResult(
                    verdict=Verdict.INVALID,
                    reason="SEMANTIC_ACT_MISMATCH",
                    details={"expected": contract.behavior, "observed": observed.act},
                )

            # Target cross-check (only when the backend supplies INDEPENDENT
            # target evidence): a verifier that actually derived a target
            # from the utterance (observed.target is not None) and disagrees
            # with the contract's pinned target rejects the candidate. The
            # rule-based baseline always returns target=None (it has no
            # independent target evidence — see semantic.py), so this branch
            # is a no-op for it by design: target enforcement for that tier
            # stays on the deterministic claim check in step 3 above. A
            # future tier-(2)/(3) backend populates observed.target and this
            # same branch enforces it with zero validator changes.
            if (
                contract.target is not None
                and observed.target is not None
                and observed.target != contract.target
            ):
                return ValidationResult(
                    verdict=Verdict.INVALID,
                    reason="SEMANTIC_TARGET_MISMATCH",
                    details={"expected_target": contract.target, "observed_target": observed.target},
                )

        return ValidationResult(verdict=Verdict.VALID)


def _matches_forbidden(act: str, forbidden_intents: list[str]) -> bool:
    return act in forbidden_intents


@dataclass
class RetryOutcome:
    result: ValidationResult
    candidate: CandidateUtterance | None
    attempts: int


def validate_with_retry(
    validator: ContractValidator,
    contract: BehaviorContract,
    generate_candidate: Callable[[], CandidateUtterance],
    max_retries: int = 2,
) -> RetryOutcome:
    """Bounded retry: generate -> validate -> retry up to max_retries times.

    On success, returns the first VALID result. On exhaustion, returns the
    last (invalid/unknown/error) result — callers must map this to
    FailureReason.CALLER_BEHAVIOR_VIOLATION and stop; never publish the
    rejected candidate.
    """
    last_result: ValidationResult | None = None
    last_candidate: CandidateUtterance | None = None
    attempts = 0
    for attempt in range(max_retries + 1):
        attempts = attempt + 1
        candidate = generate_candidate()
        result = validator.validate(candidate, contract)
        last_result, last_candidate = result, candidate
        if result.is_valid():
            return RetryOutcome(result=result, candidate=candidate, attempts=attempts)
    assert last_result is not None
    return RetryOutcome(result=last_result, candidate=None, attempts=attempts)


__all__ = [
    "ContractValidator",
    "RetryOutcome",
    "SemanticVerifierProtocol",
    "ends_call_allowed",
    "validate_with_retry",
]
