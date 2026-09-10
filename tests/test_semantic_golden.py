"""Golden semantic-verifier contract (PLAN-20260910-real-semantic-verifier
slice 1): frozen ObservedAct/verdict expectations that ANY backend behind
SemanticVerifierProtocol must satisfy, independent of implementation.

The fixture (`tests/fixtures/semantic_verifier/golden_cases.json`) is the
single source of truth for "correct" semantic judgments; this runner never
special-cases a backend by name in its assertion logic. A backend's known
gaps are declared as `known_limitations.<backend_id>` DATA in the fixture
itself — the runner wraps that case in `pytest.mark.xfail(strict=True)`
so:
  - today, the documented gap is confirmed present (XFAIL, expected);
  - if the backend later closes that gap without the fixture being
    updated, the test XPASSes and (strict=True) turns that into a hard
    failure — an unnoticed backend improvement can never silently ship
    without the golden file being reconciled.

Adding a new backend = one entry in BACKENDS below + (optionally) some
`known_limitations` entries in the fixture for cases it doesn't yet pass.
Zero changes to the assertion logic itself.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from livekit_agent_simulator.caller_contract import (
    BehaviorContract,
    CandidateUtterance,
    ContractConstraints,
    GenerationIdentity,
)
from livekit_agent_simulator.caller_contract.semantic import RuleBasedSemanticVerifier
from livekit_agent_simulator.caller_contract.validator import ContractValidator

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "semantic_verifier" / "golden_cases.json"
CASES: list[dict[str, Any]] = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))["cases"]

# The ONLY place a concrete SemanticVerifierProtocol implementation is named.
BACKENDS: dict[str, Any] = {
    "rule_based": lambda: RuleBasedSemanticVerifier(),
}


def _identity() -> GenerationIdentity:
    return GenerationIdentity(behavior_id="golden", turn_id=1, generation_id=1)


def _build_contract(spec: dict[str, Any]) -> BehaviorContract:
    c = spec.get("constraints", {})
    constraints = ContractConstraints(
        max_turns=c.get("max_turns", 3),
        max_budget=c.get("max_budget"),
        max_words=c.get("max_words"),
        forbidden_intents=list(c.get("forbidden_intents", [])),
        must_not=list(c.get("must_not", [])),
    )
    return BehaviorContract(behavior=spec["behavior"], target=spec.get("target"), constraints=constraints)


def _params() -> list[Any]:
    params = []
    for case in CASES:
        for backend_id in BACKENDS:
            limitation = (case.get("known_limitations") or {}).get(backend_id)
            marks = []
            if limitation is not None:
                marks.append(
                    pytest.mark.xfail(
                        reason=f"{case['id']} [{backend_id}]: {limitation['reason']}",
                        strict=True,
                    )
                )
            params.append(
                pytest.param(case, backend_id, id=f"{case['id']}::{backend_id}", marks=marks)
            )
    return params


@pytest.mark.parametrize("case,backend_id", _params())
def test_golden_case(case: dict[str, Any], backend_id: str) -> None:
    contract = _build_contract(case["contract"])
    claim = case["candidate_claim"]
    candidate = CandidateUtterance(
        act=claim["act"],
        target=claim.get("target"),
        slots={},
        utterance=case["utterance"],
        identity=_identity(),
    )
    backend = BACKENDS[backend_id]()
    validator = ContractValidator(semantic_verifier=backend)
    result = validator.validate(candidate, contract)

    expected = case["expected"]
    if expected["verdict"] == "valid":
        assert result.is_valid(), (
            f"{case['id']}: expected VALID, got {result.verdict} ({result.reason})"
        )
    else:
        assert not result.is_valid(), f"{case['id']}: expected REJECT, got VALID"
        assert result.reason == expected["reason"], (
            f"{case['id']}: expected reason={expected['reason']!r}, got {result.reason!r}"
        )

    # classify() is checked directly (not only through validate()) so
    # ObservedAct fields are provable even when the deterministic layer
    # alone already decided the verdict (e.g. a lexical forbidden-intent
    # catch upstream of the semantic call).
    observed = BACKENDS[backend_id]().classify(case["utterance"], contract)
    if expected.get("act") is not None:
        assert observed.act == expected["act"], (
            f"{case['id']}: expected act={expected['act']!r}, got {observed.act!r}"
        )
    if expected.get("target") is not None:
        assert observed.target == expected["target"], (
            f"{case['id']}: expected target={expected['target']!r}, got {observed.target!r}"
        )
    if expected.get("confidence_min") is not None:
        assert observed.confidence >= expected["confidence_min"], (
            f"{case['id']}: confidence {observed.confidence} below min {expected['confidence_min']}"
        )
    if expected.get("confidence_max") is not None:
        assert observed.confidence <= expected["confidence_max"], (
            f"{case['id']}: confidence {observed.confidence} above max {expected['confidence_max']}"
        )
    for tag in expected.get("all_acts_contains", []):
        assert tag in observed.all_acts, (
            f"{case['id']}: all_acts missing {tag!r} (got {observed.all_acts})"
        )


def test_every_known_limitation_references_a_registered_backend() -> None:
    """Fixture hygiene: a `known_limitations` key that doesn't match a
    registered backend id would silently never apply its xfail — that is
    a bug in the fixture, not a passing test, so catch it explicitly."""
    for case in CASES:
        for backend_id in case.get("known_limitations") or {}:
            assert backend_id in BACKENDS, (
                f"{case['id']}: known_limitations references unknown backend {backend_id!r}"
            )


def test_every_case_has_a_category_and_unique_id() -> None:
    seen_ids: set[str] = set()
    for case in CASES:
        assert case["id"] not in seen_ids, f"duplicate golden case id: {case['id']}"
        seen_ids.add(case["id"])
        assert case.get("category") in {"ACT", "TARGET", "FORBIDDEN", "CONFIDENCE"}, (
            f"{case['id']}: missing/unknown category"
        )
