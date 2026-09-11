"""Slice 3 acceptance: semantic verifier backend selection in live_wiring.py.

No network, no LiveKit — pure unit test of `_build_semantic_verifier`'s
decision logic (PLAN-20260910 slice 3, provider-aware fix from run 010):
the google simulator key drives the judge directly via generateContent
(production evidence: the asserts-judge HTTP proxy returned empty content
on every classify() call, failing all 3 attempts as VERIFIER_UNAVAILABLE).
The HTTP `judge:` block is used only when the simulator provider is NOT
google; absent/unresolvable credentials fall back to RuleBasedSemanticVerifier
(zero regression, never skip validation).
"""

from __future__ import annotations

from types import SimpleNamespace

from livekit_agent_simulator.caller_contract.live_wiring import _build_semantic_verifier
from livekit_agent_simulator.caller_contract.semantic import RuleBasedSemanticVerifier
from livekit_agent_simulator.caller_contract.semantic_llm import LLMSemanticVerifier


def _cfg(*, judge=None, sim_provider="google", sim_api_key="sim-key"):
    return SimpleNamespace(
        simulator=SimpleNamespace(provider=sim_provider, api_key=sim_api_key),
        judge=judge,
    )


def test_google_provider_uses_sim_key_directly_ignoring_broken_http_judge():
    """Run-010 regression: simulator provider google + judge: HTTP proxy
    that cannot serve classify() -> Gemini direct, never the proxy."""
    from livekit_agent_simulator.config import JudgeConfig

    judge = JudgeConfig(
        base_url="http://localhost:20128/v1",
        api_key="sk-test",
        model="wwwww",
        endpoint_type="openai",
        temperature=0.0,
    )
    verifier = _build_semantic_verifier(_cfg(judge=judge))
    assert isinstance(verifier, LLMSemanticVerifier)
    assert verifier.provider == "gemini"
    assert verifier.api_key == "sim-key"
    assert verifier.model == "gemini-flash-latest"


def test_google_provider_without_judge_block_still_uses_sim_key():
    verifier = _build_semantic_verifier(_cfg(judge=None))
    assert isinstance(verifier, LLMSemanticVerifier)
    assert verifier.provider == "gemini"


def test_google_provider_without_any_key_falls_back_to_rule_based():
    verifier = _build_semantic_verifier(_cfg(judge=None, sim_api_key=""))
    assert isinstance(verifier, RuleBasedSemanticVerifier)


def test_judge_missing_from_cfg_attr_does_not_crash():
    """cfg objects without a `judge` attribute at all (older Config shape,
    or a bare test double) must not crash `getattr(cfg, "judge", None)`."""
    verifier = _build_semantic_verifier(SimpleNamespace(simulator=SimpleNamespace(provider="google", api_key="k")))
    assert isinstance(verifier, LLMSemanticVerifier)
    assert verifier.provider == "gemini"


def test_openai_provider_http_judge_selects_llm_verifier_with_resolved_fields():
    from livekit_agent_simulator.config import JudgeConfig

    judge = JudgeConfig(
        base_url="http://localhost:20128/v1",
        api_key="sk-test",
        model="wwwww",
        endpoint_type="openai",
        temperature=0.0,
    )
    verifier = _build_semantic_verifier(_cfg(judge=judge, sim_provider="openai"))
    assert isinstance(verifier, LLMSemanticVerifier)
    assert verifier.provider == "openai"
    assert verifier.model == "wwwww"
    assert verifier.base_url == "http://localhost:20128/v1"
    assert verifier.api_key == "sk-test"


def test_openai_provider_http_anthropic_judge_selects_anthropic_provider():
    from livekit_agent_simulator.config import JudgeConfig

    judge = JudgeConfig(
        base_url="https://gateway.example/v1",
        api_key="sk-test",
        model="claude-x",
        endpoint_type="anthropic",
    )
    verifier = _build_semantic_verifier(_cfg(judge=judge, sim_provider="openai"))
    assert isinstance(verifier, LLMSemanticVerifier)
    assert verifier.provider == "anthropic"


def test_openai_provider_no_judge_falls_back_to_rule_based():
    verifier = _build_semantic_verifier(_cfg(judge=None, sim_provider="openai"))
    assert isinstance(verifier, RuleBasedSemanticVerifier)


def test_openai_provider_judge_not_ready_falls_back_to_rule_based():
    """base_url set without api_key -> resolve_judge.ready is False; must
    fail SAFE to the baseline, never raise and never silently skip
    validation (an unvalidated utterance must never reach LiveKit)."""
    from livekit_agent_simulator.config import JudgeConfig

    judge = JudgeConfig(base_url="http://localhost:20128/v1", api_key=None)
    verifier = _build_semantic_verifier(_cfg(judge=judge, sim_provider="openai"))
    assert isinstance(verifier, RuleBasedSemanticVerifier)
