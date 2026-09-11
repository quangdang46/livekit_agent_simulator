"""P0-2b: Semantic Intent Verification backend.

Implements SemanticVerifierProtocol (see validator.py) with a rule/lexical
baseline classifier — tier (1) of the layered plan in report §17.1:

    (1) rule/lexical baseline           <- this module, zero new deps
    (2) local NLI / small model         <- optional extra, future work
    (3) LLM judge                       <- opt-in, last resort

classify() re-derives the observed act/target from the UTTERANCE TEXT ALONE.
It never trusts CandidateUtterance.act/target/slots (those are generator
claims, not evidence) — the classifier is not even given the candidate,
only the raw utterance string and the contract, to make that boundary
structurally impossible to violate.

Design constraints (see bead notes):
  - pure function of (utterance, contract) -> ObservedAct; no TTS/LiveKit
    code here, fully unit-testable without audio or transport.
  - multi-label: primary act plus every other detected tag, so nested
    forbidden intents inside an otherwise-valid sentence are caught
    (report §28.3(10) multi-act, §28.3(11) nested forbidden intent).
  - precision over recall: when no pattern matches with confidence, return
    a low-confidence ObservedAct so the validator's confidence gate (not
    this module) rejects it as ambiguous. This module never fabricates a
    confident answer it cannot support.
"""

from __future__ import annotations

import re

from . import BehaviorContract, ObservedAct
from .validator import DEFAULT_INTENT_KEYWORDS

# Topic keywords per contract target. These are EVIDENCE for the target
# gate (P0 review fix): a Tier-1 verifier with no independent target signal
# fails closed (TARGET_UNVERIFIED) whenever the contract pins a target it
# cannot confirm. Each entry lists generic deal-topic phrases for one
# target; matching is substring on the lowered utterance, same discipline
# as ACT_PATTERNS. Unknown targets (not in this map) mean no evidence can
# be produced at this tier -> TARGET_UNVERIFIED by construction.
TARGET_KEYWORDS: dict[str, tuple[str, ...]] = {
    "price": (
        "price", "$", "cost", "fee", "charge", "quote", "budget",
        "lower the price", "monthly fee", "how much",
    ),
    "delivery_date": (
        "deliver", "delivery", "arrival", "arrive", "shipping", "ship",
        "eta", "when will", "how long",
    ),
    "order_status": (
        "order", "status", "delayed", "delay", "shipment", "tracking",
    ),
    "hours": ("hours", "open", "close", "opening", "what time"),
    "plan": ("plan", "package", "subscription"),
    "status": ("status", "update", "checking on"),
    "charge": ("charge", "bill", "billing", "fee"),
    "fees": ("fee", "fees", "hidden", "cost", "charge"),
}


def _target_evidence(utterance: str, target: str | None) -> str | None:
    """Independent target evidence from the utterance text alone.

    Returns the contract's target string when the utterance contains a
    topic keyword for it, else None (no evidence — never a guess, never
    the generator's claim). Unknown targets (absent from TARGET_KEYWORDS)
    always yield None at this tier: a stronger verifier backend must
    supply the evidence instead.
    """
    if target is None:
        return None
    keywords = TARGET_KEYWORDS.get(target)
    if keywords is None:
        return None
    lowered = utterance.lower()
    if any(kw in lowered for kw in keywords):
        return target
    return None


# Keyword patterns per semantic act. Order does not determine precedence;
# the primary act is chosen by highest match count (ties broken by contract
# behavior match preferred, to avoid unnecessary false rejects on the
# caller's actual intended act when phrasing is ambiguous between two verbs).
ACT_PATTERNS: dict[str, tuple[str, ...]] = {
    # First-person question/intent markers observed in real adaptive `ask`
    # output ("What information are you looking for?" triggered the gap:
    # valid ask with zero act patterns -> LOW_CONFIDENCE false reject in a
    # live run). Keep generic auxiliaries narrowly scoped so multi-act
    # sentences still let the competing act win its own primary on count:
    # e.g. "Could you lower the price, and by the way do you offer
    # financing?" scores negotiate=1 ("lower the price") vs ask=1 ("do you
    # offer") and the contract-behavior tie-break keeps negotiate primary.
    "ask": (
        "what's",
        "what is",
        "what are",
        "what information",
        "what can",
        "what do",
        "what would",
        "how much",
        "how does",
        "how do",
        "could you tell me",
        "can you tell me",
        "could you let me know",
        "can you let me know",
        "i was wondering",
        "i wanted to ask",
        "i'd like to know",
        "i'd like to ask",
        "i have a question",
        "what are you asking",
        "are you looking for",
        "looking for",
    ),
    "negotiate": (
        "come down",
        "lower the price",
        "any room on",
        "closer to",
        "could you do",
        "would you consider",
        "meet me at",
        "my limit",
        "my budget",
    ),
    "confirm": ("so it's", "just to confirm", "is that right", "to confirm"),
    "deny": ("no thanks", "i don't think so", "that won't work"),
    "accept": ("sounds good", "that works", "i'll take it", "works for me", "okay, that's"),
    "reject": ("not interested", "no, i'd rather", "i'll pass"),
    "provide": ("i'm calling about", "i want", "i'd like to"),
    "arrange_visit": ("come by", "hold it until", "schedule a time", "book a time"),
    "end": ("goodbye", "bye", "thanks, that's all", "have a good day"),
}

# Confidence assigned when N keyword hits are found for the winning act.
_CONFIDENCE_BY_HIT_COUNT = {1: 0.75, 2: 0.85}
_CONFIDENCE_MANY_HITS = 0.92
_NO_MATCH_CONFIDENCE = 0.2  # deliberately below SEMANTIC_CONFIDENCE_THRESHOLD


def _split_clauses(utterance: str) -> list[str]:
    """Split on clause boundaries so a multi-act sentence can be scored
    per-clause, improving recall for the secondary-act / nested-intent case.
    """
    parts = re.split(r",|;|\band by the way\b|\balthough\b|\bbut\b", utterance, flags=re.IGNORECASE)
    return [p.strip() for p in parts if p.strip()]


def _score_act_hits(text: str) -> dict[str, int]:
    lowered = text.lower()
    hits: dict[str, int] = {}
    for act, patterns in ACT_PATTERNS.items():
        count = sum(1 for pat in patterns if pat in lowered)
        if count:
            hits[act] = count
    return hits


def _confidence_for(hit_count: int) -> float:
    if hit_count >= 3:
        return _CONFIDENCE_MANY_HITS
    return _CONFIDENCE_BY_HIT_COUNT.get(hit_count, _NO_MATCH_CONFIDENCE)


class RuleBasedSemanticVerifier:
    """Baseline semantic verifier: keyword/rule classification.

    Swappable behind SemanticVerifierProtocol; a stronger backend (local
    NLI, LLM judge) can replace this without the validator's enforcement
    boundary moving.
    """

    def classify(self, utterance: str, contract: BehaviorContract) -> ObservedAct:
        overall_hits = _score_act_hits(utterance)

        # Multi-label tag collection: every forbidden-intent keyword found
        # anywhere in the utterance is surfaced in all_acts, even if it is
        # not the primary act — this is what lets the validator catch a
        # forbidden intent nested inside an otherwise on-topic sentence.
        lowered = utterance.lower()
        detected_intent_tags = [
            intent
            for intent, keywords in DEFAULT_INTENT_KEYWORDS.items()
            if any(kw in lowered for kw in keywords)
        ]

        if not overall_hits:
            # No act pattern matched at all: genuinely ambiguous. Do not
            # guess the contract's own behavior just to look confident —
            # return low confidence and let the validator reject it.
            # target=None for the same reason as the matched path below:
            # no independent target evidence exists in this tier.
            return ObservedAct(
                act=contract.behavior,
                target=None,
                confidence=_NO_MATCH_CONFIDENCE,
                all_acts=detected_intent_tags or [contract.behavior],
            )

        # Prefer the contract's own behavior on ties, since a caller
        # genuinely executing the assigned behavior with a slightly unusual
        # phrasing should not be penalized versus an incidental keyword hit
        # elsewhere in the same clause.
        best_act = max(
            overall_hits,
            key=lambda act: (overall_hits[act], act == contract.behavior),
        )
        primary_hits = overall_hits[best_act]

        all_acts = list(dict.fromkeys([best_act, *overall_hits.keys(), *detected_intent_tags]))

        # TARGET EVIDENCE (P0 review fix): independent topic-keyword match
        # against the utterance text alone — never the generator's claim,
        # never an echo of contract.target. A match returns the target
        # string (evidence); no match returns None (unknown), which the
        # validator maps to TARGET_UNVERIFIED when the contract pins a
        # target. Unknown targets (absent from TARGET_KEYWORDS) always
        # yield None at this tier — a stronger backend must supply them.
        return ObservedAct(
            act=best_act,
            target=_target_evidence(utterance, contract.target),
            confidence=_confidence_for(primary_hits),
            all_acts=all_acts,
        )


__all__ = ["ACT_PATTERNS", "RuleBasedSemanticVerifier"]
