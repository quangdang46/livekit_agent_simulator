"""P0-8: Python side of the Python-Rust behavioral parity suite.

Golden vectors under tests/fixtures/parity/ are language-neutral JSON —
this file replays them through the REAL Python caller_contract
implementation and asserts the expected verdict/result. The Rust side
(src/livekit_agent_simulator_rust/crates/lks-core/src/caller_contract.rs)
reads the SAME directory and must produce the SAME verdicts for the SAME
inputs — see that module's `#[cfg(test)] mod parity_tests`.

Any change to a vector file requires updating both sides in the same
commit (see bead notes / module docstrings).
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
    EndedBy,
    FailureReason,
    GenerationIdentity,
    is_current,
)
from livekit_agent_simulator.caller_contract.orchestrator import TurnDetector
from livekit_agent_simulator.caller_contract.semantic import RuleBasedSemanticVerifier
from livekit_agent_simulator.caller_contract.validator import ContractValidator

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "parity"

_VALIDATOR_VECTOR_FILES = [
    "validator_valid_pass.json",
    "validator_act_mismatch.json",
    "validator_target_mismatch.json",
    "validator_slot_violation.json",
    "validator_slot_string_coercion.json",
    "validator_schema_invalid.json",
    "validator_utterance_too_long.json",
    "validator_end_call_not_allowed.json",
    "validator_forbidden_intent_lexical.json",
    "validator_forbidden_intent_nested_semantic.json",
    "validator_ambiguous_low_confidence.json",
]

_REQUIRED_VALIDATOR_VECTOR_KEYS = {
    "id",
    "use_semantic_verifier",
    "contract",
    "candidate",
    "expected_verdict",
    "expected_reason",
}
_REQUIRED_CONTRACT_KEYS = {"behavior", "target", "constraints"}
_REQUIRED_CONSTRAINTS_KEYS = {
    "max_turns",
    "max_budget",
    "max_words",
    "max_duration_s",
    "forbidden_intents",
    "must_not",
}
_REQUIRED_CANDIDATE_KEYS = {"act", "target", "slots", "utterance", "identity"}
_REQUIRED_IDENTITY_KEYS = {"behavior_id", "turn_id", "generation_id", "context_version"}


def _load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Structural validation ("validates against its own JSON schema" — a
# hand-rolled structural check equivalent to schema validation, since this
# fixture set is small and language-neutral rather than a general-purpose
# schema library consumer)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("filename", _VALIDATOR_VECTOR_FILES)
def test_validator_vector_matches_required_schema_shape(filename: str) -> None:
    data = _load(filename)
    assert _REQUIRED_VALIDATOR_VECTOR_KEYS <= set(data.keys())
    assert _REQUIRED_CONTRACT_KEYS <= set(data["contract"].keys())
    assert _REQUIRED_CONSTRAINTS_KEYS <= set(data["contract"]["constraints"].keys())
    assert _REQUIRED_CANDIDATE_KEYS <= set(data["candidate"].keys())
    assert _REQUIRED_IDENTITY_KEYS <= set(data["candidate"]["identity"].keys())
    assert data["expected_verdict"] in {"VALID", "INVALID", "UNKNOWN", "ERROR"}


def test_turn_debounce_vector_matches_required_shape() -> None:
    data = _load("turn_debounce.json")
    assert "cases" in data and len(data["cases"]) > 0
    for case in data["cases"]:
        assert {"silence_debounce_ms", "last_stop_ms", "now_ms", "expected_turn_complete"} <= set(case.keys())


def test_staleness_vector_matches_required_shape() -> None:
    data = _load("staleness_drop.json")
    assert "cases" in data and len(data["cases"]) > 0
    for case in data["cases"]:
        assert {"identity", "current", "expected_is_current"} <= set(case.keys())
        assert _REQUIRED_IDENTITY_KEYS <= set(case["identity"].keys())


def test_failure_codes_vector_matches_required_shape() -> None:
    data = _load("failure_codes.json")
    assert "expected_failure_reasons" in data
    assert "expected_ended_by" in data
    assert len(data["expected_failure_reasons"]) == 7
    assert len(data["expected_ended_by"]) == 6


# ---------------------------------------------------------------------------
# Replay through the real Python implementation
# ---------------------------------------------------------------------------


def _build_contract(raw: dict[str, Any]) -> BehaviorContract:
    constraints = ContractConstraints(**raw["constraints"])
    return BehaviorContract(behavior=raw["behavior"], target=raw["target"], constraints=constraints)


def _build_candidate(raw: dict[str, Any]) -> CandidateUtterance:
    identity = GenerationIdentity(**raw["identity"])
    return CandidateUtterance(
        act=raw["act"], target=raw["target"], slots=raw["slots"], utterance=raw["utterance"], identity=identity
    )


@pytest.mark.parametrize("filename", _VALIDATOR_VECTOR_FILES)
def test_validator_vector_produces_expected_verdict_in_python(filename: str) -> None:
    data = _load(filename)
    contract = _build_contract(data["contract"])
    candidate = _build_candidate(data["candidate"])
    semantic_verifier = RuleBasedSemanticVerifier() if data["use_semantic_verifier"] else None
    validator = ContractValidator(semantic_verifier=semantic_verifier)

    result = validator.validate(candidate, contract)

    assert result.verdict.value == data["expected_verdict"], (
        f"{data['id']}: expected {data['expected_verdict']}, got {result.verdict.value} ({result.details})"
    )
    if data["expected_reason"] is not None:
        # SCHEMA_INVALID carries a ": <detail>" suffix in Python (the
        # ValueError message); parity asserts the PREFIX only — the Rust
        # side returns the bare code. Verdict equality above is exact.
        if data["expected_reason"] == "SCHEMA_INVALID":
            assert (result.reason or "").startswith("SCHEMA_INVALID"), (
                f"{data['id']}: reason mismatch: {result.reason!r}"
            )
        else:
            assert result.reason == data["expected_reason"], f"{data['id']}: reason mismatch: {result.reason!r}"


def test_turn_debounce_vector_matches_python_formula() -> None:
    data = _load("turn_debounce.json")
    for case in data["cases"]:
        detector = TurnDetector(silence_debounce_ms=case["silence_debounce_ms"])
        detector.on_agent_audio_started(now_ms=0)
        detector.on_agent_audio_stopped(now_ms=case["last_stop_ms"])
        from livekit_agent_simulator.caller_contract.orchestrator import TurnState

        state = detector.poll(now_ms=case["now_ms"])
        is_complete = state == TurnState.AGENT_TURN_COMPLETE
        assert is_complete == case["expected_turn_complete"], case


def test_staleness_vector_matches_python_is_current() -> None:
    data = _load("staleness_drop.json")
    for case in data["cases"]:
        identity = GenerationIdentity(**case["identity"])
        current = GenerationIdentity(**case["current"])
        assert is_current(identity, current) == case["expected_is_current"], case["name"]


def test_failure_codes_vector_matches_python_enums() -> None:
    data = _load("failure_codes.json")
    assert {m.value for m in FailureReason} == set(data["expected_failure_reasons"])
    assert {m.value for m in EndedBy} == set(data["expected_ended_by"])


def test_should_interrupt_vector_matches_required_shape() -> None:
    data = _load("should_interrupt.json")
    assert data["none_interaction_expected"] is False
    assert data["no_rate_expected"] is False
    assert len(data["cases"]) > 0
    for case in data["cases"]:
        assert {"scenario_id", "seed", "turn", "rate", "expected"} <= set(case.keys())
        assert case["rate"] in {"low", "medium", "high"}


def test_should_interrupt_vector_matches_python_policy() -> None:
    from livekit_agent_simulator.caller_contract.dsl import InteractionConfig
    from livekit_agent_simulator.caller_contract.interaction_planner import CallerInteractionPlanner

    data = _load("should_interrupt.json")
    planner = CallerInteractionPlanner()
    assert planner.should_interrupt(None, scenario_id="s", agent_turn_index=0) is (
        data["none_interaction_expected"]
    )
    assert planner.should_interrupt(
        InteractionConfig(), scenario_id="s", agent_turn_index=0
    ) is (data["no_rate_expected"])
    for case in data["cases"]:
        ic = InteractionConfig(
            interruption_rate=case["rate"], interruption_seed=case["seed"]
        )
        assert planner.should_interrupt(
            ic, scenario_id=case["scenario_id"], agent_turn_index=case["turn"]
        ) is case["expected"], case


def test_semantic_lexicon_vector_matches_required_shape() -> None:
    data = _load("semantic_lexicon.json")
    assert len(data["cases"]) > 0
    for case in data["cases"]:
        assert {"utterance", "behavior", "target", "expected"} <= set(case.keys())
        assert {"act", "confidence", "target", "all_acts"} <= set(case["expected"].keys())


def _check_lexicon_cases(data, *, source: str) -> None:
    from livekit_agent_simulator.caller_contract import ContractConstraints

    verifier = RuleBasedSemanticVerifier()
    for case in data["cases"]:
        assert case.get("source", source), case
        _check_one_lexicon_case(verifier, case)


def _check_one_lexicon_case(verifier, case) -> None:
    from livekit_agent_simulator.caller_contract import ContractConstraints

    contract = _build_contract(
        {
            "behavior": case["behavior"],
            "target": case["target"],
            "constraints": {
                "max_turns": 3,
                "max_budget": None,
                "max_words": None,
                "max_duration_s": None,
                "forbidden_intents": [],
                "must_not": [],
            },
        }
    )
    observed = verifier.classify(case["utterance"], contract)
    exp = case["expected"]
    assert observed.act == exp["act"], case
    assert observed.confidence == exp["confidence"], case
    assert observed.target == exp["target"], case
    assert observed.all_acts == exp["all_acts"], case


def test_semantic_lexicon_vector_matches_python_classifier() -> None:
    data = _load("semantic_lexicon.json")
    _check_lexicon_cases(data, source="authored")


def test_semantic_lexicon_run_failures_match_python_classifier() -> None:
    """Every case traces to a witnessed Phase D run failure (016-018) or a
    probed neighbor of one — see the `source` field. No pattern was added
    without a run that needed it."""
    data = _load("semantic_lexicon_run_failures.json")
    assert len(data["cases"]) > 0
    _check_lexicon_cases(data, source="run")


def test_orchestrator_evaluator_vector_matches_python_logic() -> None:
    from livekit_agent_simulator.caller_contract import BehaviorContract, ContractConstraints
    from livekit_agent_simulator.caller_contract.orchestrator import (
        BehaviorEvaluator,
        Orchestrator,
        TurnDetector,
        classify_agent_silence,
    )

    data = _load("orchestrator_evaluator.json")

    evaluator = BehaviorEvaluator()
    for case in data["evaluator_cases"]:
        contract = BehaviorContract(
            behavior=case.get("contract_behavior", "negotiate"),
            target=case["contract_target"],
            constraints=ContractConstraints(),
        )
        assert evaluator.evaluate(contract, case["text"]).value == case["expected"], case

    for case in data["silence_cases"]:
        outcome = classify_agent_silence(
            elapsed_ms=case["elapsed_ms"],
            turn_timeout_ms=case["turn_timeout_ms"],
            agent_hung_up=case["agent_hung_up"],
            transport_lost=case["transport_lost"],
        )
        assert outcome.value == case["expected"], case

    script = data["turn_detector_script"]
    detector = TurnDetector(silence_debounce_ms=script["silence_debounce_ms"])
    for step in script["steps"]:
        op = step["op"]
        if op == "poll":
            assert detector.poll(now_ms=step["at_ms"]).value == step["expected_state"], step
        elif op == "started":
            detector.on_agent_audio_started(now_ms=step["at_ms"])
        elif op == "stopped":
            detector.on_agent_audio_stopped(now_ms=step["at_ms"])
        elif op == "begin_caller_turn":
            detector.begin_caller_turn()
        elif op == "reset":
            detector.reset_for_next_agent_turn()
        else:  # pragma: no cover - fixture typo guard
            raise AssertionError(f"unknown turn-detector op {op!r}")

    script = data["orchestrator_script"]
    orch = Orchestrator()
    for step in script["steps"]:
        op = step["op"]
        if op == "identity":
            ident = orch.current_identity()
            assert ident.behavior_id == step["expected"]["behavior_id"], step
            assert ident.turn_id == step["expected"]["turn_id"], step
            assert ident.generation_id == step["expected"]["generation_id"], step
        elif op == "advance_caller_turn":
            orch.advance_caller_turn()
        elif op == "advance_behavior_turn":
            orch.advance_behavior_turn()
        elif op == "new_generation":
            orch.new_generation()
        elif op == "start_behavior":
            orch.start_behavior()
        elif op == "check_max_turns":
            contract = BehaviorContract(
                behavior="x", constraints=ContractConstraints(max_turns=step["max_turns"])
            )
            assert orch.check_max_turns(contract).value == step["expected"], step
        else:  # pragma: no cover - fixture typo guard
            raise AssertionError(f"unknown orchestrator op {op!r}")


def test_interaction_planner_vector_matches_python_delivery() -> None:
    from livekit_agent_simulator.caller_contract.dsl import InteractionConfig
    from livekit_agent_simulator.caller_contract.interaction_planner import (
        HESITATION_TOKEN_ALLOWLIST,
        CallerInteractionPlanner,
        verify_semantic_preserving,
    )

    data = _load("interaction_planner.json")

    assert set(HESITATION_TOKEN_ALLOWLIST) == set(data["hesitation_allowlist"])

    planner = CallerInteractionPlanner()
    for case in data["speak_cases"]:
        raw_ic = case["interaction"]
        ic = InteractionConfig(**raw_ic) if raw_ic is not None else None
        outcome = planner.plan_speak(case["utterance"], ic)
        exp = case["expected"]
        assert outcome.tokens == exp["tokens"], case
        assert outcome.pre_delay_ms == exp["pre_delay_ms"], case
        assert outcome.pace == exp["pace"], case
        assert verify_semantic_preserving(case["utterance"], outcome.tokens), case

    for case in data["preserving_cases"]:
        assert verify_semantic_preserving(case["utterance"], case["tokens"]) is case["expected"], case

    for case in data["action_cases"]:
        op = case["op"]
        if op == "dtmf":
            outcome = planner.plan_dtmf(case["digits"])
        elif op == "silence":
            outcome = planner.plan_silence()
        elif op == "hangup":
            outcome = planner.plan_hangup()
        elif op == "backchannel_default":
            outcome = planner.plan_backchannel()
            assert outcome.tokens == case["expected_tokens"], case
        elif op == "barge_in":
            outcome = planner.trigger_barge_in()
        else:  # pragma: no cover - fixture typo guard
            raise AssertionError(f"unknown planner action op {op!r}")
        assert outcome.kind.value == case["expected_kind"], case


def test_record_replay_vector_matches_python_primitives() -> None:
    import json

    from livekit_agent_simulator.caller_contract import (
        CandidateUtterance,
        GenerationIdentity,
        Verdict,
    )
    from livekit_agent_simulator.caller_contract.record_replay import (
        RECORD_FORMAT_V1,
        RECORD_FORMAT_VERSION,
        RecordedSemanticVerifier,
        ReplayLanguageBackend,
        ReplayMismatchError,
        RunRecord,
        rebuild_identity_from_candidate,
    )
    from livekit_agent_simulator.caller_contract.validator import (
        ContractValidator,
        validate_with_retry,
    )

    data = _load("record_replay.json")

    assert RECORD_FORMAT_VERSION == data["format_version"]
    assert RECORD_FORMAT_V1 == data["v1_legacy_version"]
    for bad in data["rejected_versions"]:
        with pytest.raises(ValueError):
            RunRecord.from_dict(
                {"format_version": bad, "scenario_id": "x", "seed": 0, "attempts": []}
            )

    # Malformed input reports its REAL cause: broken JSON is a parse error,
    # wrong-shaped JSON is a shape error — never a generic "empty record".
    with pytest.raises(json.JSONDecodeError):
        RunRecord.from_json("{not json")
    with pytest.raises(KeyError):
        RunRecord.from_dict({"format_version": 2, "scenario_id": "s", "seed": 0})

    # v1 legacy: reads, observed None, neutral fallback once, then exhaustion.
    v1 = RunRecord.from_dict(data["v1_record"])
    assert v1.format_version == 1
    assert v1.attempts[0].observed is None
    verifier = RecordedSemanticVerifier(v1)
    fallback = verifier.classify("anything", None)
    exp_fallback = data["v1_expected"]["fallback_observed"]
    assert fallback.act == exp_fallback["act"]
    assert fallback.confidence == exp_fallback["confidence"]
    with pytest.raises(RuntimeError):
        verifier.classify("anything", None)

    # v2: round-trip, verbatim replay, identity rebuild, loud divergence.
    raw_v2 = data["v2_record"]
    v2 = RunRecord.from_dict(raw_v2)
    v2_rt = RunRecord.from_json(v2.to_json())
    assert (v2_rt.scenario_id, v2_rt.seed) == (v2.scenario_id, v2.seed)
    assert len(v2_rt.attempts) == 1
    backend = ReplayLanguageBackend(v2_rt)
    candidate = backend.generate({})
    assert candidate["utterance"] == data["v2_expected"]["replayed_utterance"]
    rebuilt = rebuild_identity_from_candidate(candidate)
    exp_ident = data["v2_expected"]["rebuilt_identity"]
    assert (rebuilt.behavior_id, rebuilt.turn_id, rebuilt.generation_id) == (
        exp_ident["behavior_id"],
        exp_ident["turn_id"],
        exp_ident["generation_id"],
    )
    # Evidence fidelity: the recorded slot must survive the round trip —
    # dropping it would lose the data a forensic replay needs to explain a
    # slot verdict.
    exp_slots = data["v2_expected"]["observed_slots_survive_roundtrip"]
    assert v2_rt.attempts[0].observed["slots"] == exp_slots

    backend.assert_verdict(Verdict(data["v2_expected"]["replayed_verdict"]))
    exp_outcome = data["v2_expected"]["replayed_outcome"]
    backend.assert_outcome(
        failure_reason=exp_outcome["failure_reason"], ended_by=exp_outcome["ended_by"]
    )
    with pytest.raises(ReplayMismatchError):
        backend.assert_verdict(Verdict.INVALID)
    with pytest.raises(ReplayMismatchError):
        backend.assert_outcome(failure_reason="CALLER_BEHAVIOR_VIOLATION", ended_by="scenario")
    with pytest.raises(ReplayMismatchError):
        backend.assert_outcome(failure_reason=None, ended_by="timeout")
    with pytest.raises(RuntimeError):
        backend.generate({})

    # Bounded retry: early exit on first VALID; exhaustion is max_retries+1
    # with candidate None.
    from livekit_agent_simulator.caller_contract import BehaviorContract, ContractConstraints

    contract = BehaviorContract(behavior="negotiate", constraints=ContractConstraints())
    validator = ContractValidator()
    for case in data["retry_cases"]:
        if case["always_invalid"]:
            def gen_bad() -> CandidateUtterance:
                return CandidateUtterance(
                    act="wrong",
                    target=None,
                    slots={},
                    utterance="x y",
                    identity=GenerationIdentity(behavior_id="b1", turn_id=1, generation_id=1),
                )

            out = validate_with_retry(validator, contract, gen_bad, max_retries=case["max_retries"])
        else:
            def gen_good() -> CandidateUtterance:
                return CandidateUtterance(
                    act="negotiate",
                    target=None,
                    slots={},
                    utterance="Would you consider $28,000?",
                    identity=GenerationIdentity(behavior_id="b1", turn_id=1, generation_id=1),
                )

            out = validate_with_retry(validator, contract, gen_good, max_retries=case["max_retries"])
        assert out.attempts == case["expected_attempts"], case
        assert (out.candidate is None) == case["expected_candidate_null"], case
        assert out.result.verdict.value == case["expected_verdict"], case


def test_observer_transcript_vector_matches_python_logic(tmp_path: Path) -> None:
    """Observer.on_transcript dedupe/backchannel/preamble parity vector.

    Same fixture as the Rust side's `lks-core::observer::parity_tests`
    (crates/lks-core/src/observer.rs) — replayed here against the REAL
    `Observer` class (mirrors tests/test_observer.py's `_observer` helper,
    but driven from the shared JSON fixture instead of inline literals).
    """
    from unittest.mock import MagicMock

    from livekit_agent_simulator.config import ObserveConfig
    from livekit_agent_simulator.livekit.observer import Observer
    from livekit_agent_simulator.logging.event_writer import EventWriter

    data = json.loads((FIXTURES_DIR / "observer_transcript.json").read_text(encoding="utf-8"))

    for case in data["cases"]:
        cfg = case["config"]
        observe = ObserveConfig(
            transcript_dedupe_window_ms=cfg.get("transcript_dedupe_window_ms", 15000)
        )
        report_dir = tmp_path / "reports" / case["name"]
        writer = EventWriter(f"r-{case['name']}", report_dir, timezone_name="UTC")
        obs = Observer(
            MagicMock(),
            writer,
            observe,
            agent_identity=cfg.get("agent_identity", "agent-1"),
            sim_identity=cfg.get("sim_identity", "sim-1"),
            first_speaker=cfg.get("first_speaker", "agent"),
        )

        for step in case["steps"]:
            obs.on_transcript(
                step["role"],
                step["text"],
                final=step.get("final", True),
                segment_id=step.get("segment_id"),
                source=step["source"],
            )

        for expected in case.get("expect_events", []):
            kind = expected["kind"]
            matches = [e for e in writer._events if e["kind"] == kind]
            assert matches, f"case {case['name']}: expected at least one {kind} event, found none"
            spec_contains = expected.get("spec_contains")
            if spec_contains:
                found = any(
                    all(m["spec"].get(k) == v for k, v in spec_contains.items())
                    for m in matches
                )
                assert found, f"case {case['name']}: no {kind} event matched {spec_contains}"

        for kind, expected_count in case.get("expect_event_counts", {}).items():
            actual = len([e for e in writer._events if e["kind"] == kind])
            assert actual == expected_count, (
                f"case {case['name']}: expected {expected_count} {kind} event(s), found {actual}"
            )

        if "expect_final_turn" in case:
            assert obs.turn == case["expect_final_turn"], f"case {case['name']}: final turn mismatch"
