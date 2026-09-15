"""P0-5: AI Language Adapter tests (mocked provider, no network).

Covers structured-output parse, malformed-output retry path, timeout/429
path ending in LANGUAGE_GENERATION_ERROR, context cap behavior, proof that
the say-path never invokes the adapter, and proof that no failure-reason
strings are defined locally (all imported from the P0-1 contract bead).
"""

from __future__ import annotations

import pytest

from livekit_agent_simulator.caller_contract import (
    BehaviorContract,
    ContractConstraints,
    FailureReason,
    GenerationIdentity,
)
from livekit_agent_simulator.caller_contract.language_adapter import (
    DEFAULT_RECENT_TURNS_CAP,
    AILanguageAdapter,
    LanguageGenerationError,
    Turn,
    build_context,
    should_invoke_adapter,
)


def _contract() -> BehaviorContract:
    return BehaviorContract(
        behavior="negotiate",
        target="price",
        constraints=ContractConstraints(max_turns=3, max_budget=30000),
    )


def _identity() -> GenerationIdentity:
    return GenerationIdentity(behavior_id="b2", turn_id=1, generation_id=1)


# ---------------------------------------------------------------------------
# Structured-output parse
# ---------------------------------------------------------------------------


class _StubBackend:
    def __init__(self, responses: list) -> None:
        self._responses = list(responses)
        self.calls = 0

    def generate(self, context):
        self.calls += 1
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def test_structured_output_parses_into_candidate_utterance() -> None:
    backend = _StubBackend(
        [{"act": "negotiate", "target": "price", "slots": {"max_budget": 30000}, "utterance": "Could you do $30,000?"}]
    )
    adapter = AILanguageAdapter(backend=backend)
    candidate = adapter.generate_candidate(_contract(), context={}, identity=_identity())
    assert candidate.act == "negotiate"
    assert candidate.target == "price"
    assert candidate.slots == {"max_budget": 30000}
    assert candidate.utterance == "Could you do $30,000?"
    assert candidate.identity == _identity()
    assert backend.calls == 1


# ---------------------------------------------------------------------------
# Malformed-output retry path
# ---------------------------------------------------------------------------


def test_malformed_output_retries_then_succeeds() -> None:
    backend = _StubBackend(
        [
            {"act": "negotiate"},  # missing 'utterance' -> triggers retry
            {"act": "negotiate", "target": "price", "slots": {}, "utterance": "Could you do $30,000?"},
        ]
    )
    adapter = AILanguageAdapter(backend=backend, max_retries=1)
    candidate = adapter.generate_candidate(_contract(), context={}, identity=_identity())
    assert candidate.utterance == "Could you do $30,000?"
    assert backend.calls == 2


def test_malformed_output_exhausts_retries_raises_language_generation_error() -> None:
    backend = _StubBackend([{"act": "negotiate"}, {"act": "negotiate"}])  # both missing 'utterance'
    adapter = AILanguageAdapter(backend=backend, max_retries=1)
    with pytest.raises(LanguageGenerationError) as exc_info:
        adapter.generate_candidate(_contract(), context={}, identity=_identity())
    assert exc_info.value.reason == FailureReason.LANGUAGE_GENERATION_ERROR
    assert backend.calls == 2


def test_non_dict_slots_is_malformed() -> None:
    backend = _StubBackend(
        [
            {"act": "negotiate", "target": "price", "slots": "not-a-dict", "utterance": "hi"},
            {"act": "negotiate", "target": "price", "slots": "not-a-dict", "utterance": "hi"},
        ]
    )
    adapter = AILanguageAdapter(backend=backend, max_retries=1)
    with pytest.raises(LanguageGenerationError):
        adapter.generate_candidate(_contract(), context={}, identity=_identity())


# ---------------------------------------------------------------------------
# Timeout / 429 path ending in LANGUAGE_GENERATION_ERROR — never a fallback
# ---------------------------------------------------------------------------


def test_timeout_exhausts_retries_and_raises_language_generation_error() -> None:
    backend = _StubBackend([TimeoutError("backend timed out"), TimeoutError("backend timed out")])
    adapter = AILanguageAdapter(backend=backend, max_retries=1)
    with pytest.raises(LanguageGenerationError) as exc_info:
        adapter.generate_candidate(_contract(), context={}, identity=_identity())
    assert exc_info.value.reason == FailureReason.LANGUAGE_GENERATION_ERROR
    assert isinstance(exc_info.value.last_error, TimeoutError)


class _Http429Error(Exception):
    pass


def test_http_429_exhausts_retries_and_raises_language_generation_error() -> None:
    backend = _StubBackend([_Http429Error("rate limited"), _Http429Error("rate limited")])
    adapter = AILanguageAdapter(backend=backend, max_retries=1)
    with pytest.raises(LanguageGenerationError):
        adapter.generate_candidate(_contract(), context={}, identity=_identity())


def test_retry_recovers_after_transient_timeout() -> None:
    backend = _StubBackend(
        [
            TimeoutError("transient"),
            {"act": "negotiate", "target": "price", "slots": {}, "utterance": "Could you do $30,000?"},
        ]
    )
    adapter = AILanguageAdapter(backend=backend, max_retries=1)
    candidate = adapter.generate_candidate(_contract(), context={}, identity=_identity())
    assert candidate.utterance == "Could you do $30,000?"


# ---------------------------------------------------------------------------
# Context cap behavior — minimal structured JSON, never a full transcript
# ---------------------------------------------------------------------------


def test_context_builder_emits_minimal_structured_fields() -> None:
    context = build_context(
        contract=_contract(),
        turn=2,
        agent_latest="The lowest I can do is $31,000.",
        relevant_facts=["Caller wants the 2022 Honda CR-V", "Caller budget is $30,000"],
        recent_turns=[Turn("caller", "Could you do $30,000?"), Turn("agent", "The lowest I can do is $31,000.")],
    )
    assert context["current_behavior"] == {
        "act": "negotiate",
        "target": "price",
        "max_budget": 30000,
        "turn": 2,
        "max_turns": 3,
    }
    assert context["agent_latest"] == {"text": "The lowest I can do is $31,000."}
    assert context["relevant_facts"] == ["Caller wants the 2022 Honda CR-V", "Caller budget is $30,000"]
    assert len(context["recent_turns"]) == 2


def test_context_builder_caps_recent_turns() -> None:
    many_turns = [Turn("caller" if i % 2 == 0 else "agent", f"line {i}") for i in range(20)]
    context = build_context(
        contract=_contract(),
        turn=10,
        agent_latest="ok",
        relevant_facts=[],
        recent_turns=many_turns,
        recent_turns_cap=4,
    )
    assert len(context["recent_turns"]) == 4
    # Must be the MOST RECENT turns, not an arbitrary slice.
    assert context["recent_turns"][-1]["text"] == "line 19"


def test_context_builder_default_cap_matches_module_constant() -> None:
    many_turns = [Turn("caller", f"line {i}") for i in range(50)]
    context = build_context(
        contract=_contract(), turn=1, agent_latest=None, relevant_facts=[], recent_turns=many_turns
    )
    assert len(context["recent_turns"]) == DEFAULT_RECENT_TURNS_CAP


def test_context_builder_agent_latest_none_when_no_response_yet() -> None:
    context = build_context(contract=_contract(), turn=0, agent_latest=None, relevant_facts=[], recent_turns=[])
    assert context["agent_latest"] is None


# ---------------------------------------------------------------------------
# say-path never invokes the adapter
# ---------------------------------------------------------------------------


def test_should_invoke_adapter_false_for_say_actions() -> None:
    assert should_invoke_adapter(bypasses_ai_and_validator=True) is False


def test_should_invoke_adapter_true_for_do_actions() -> None:
    assert should_invoke_adapter(bypasses_ai_and_validator=False) is True


def test_say_action_from_dsl_never_invokes_adapter_end_to_end() -> None:
    """Integration proof: a say CallerAction parsed by the DSL (P0-3) must
    report should_invoke_adapter() == False."""
    from livekit_agent_simulator.caller_contract.dsl import parse_step

    action = parse_step({"say": "Hi."}, line_no=1)
    assert should_invoke_adapter(action.bypasses_ai_and_validator) is False


def test_do_action_from_dsl_invokes_adapter_end_to_end() -> None:
    from livekit_agent_simulator.caller_contract.dsl import parse_step

    action = parse_step({"do": "negotiate"}, line_no=1)
    assert should_invoke_adapter(action.bypasses_ai_and_validator) is True


# ---------------------------------------------------------------------------
# No failure-reason strings defined locally — always imported from P0-1
# ---------------------------------------------------------------------------


def test_no_failure_reason_strings_defined_locally() -> None:
    """LanguageGenerationError.reason must be the shared FailureReason enum
    member, not a locally re-declared string constant."""
    import livekit_agent_simulator.caller_contract.language_adapter as mod

    assert mod.LanguageGenerationError.reason is FailureReason.LANGUAGE_GENERATION_ERROR
    # Structural guard: no module-level string literal named like a reason code.
    assert not hasattr(mod, "LANGUAGE_GENERATION_ERROR")
