"""Slice 3 acceptance: semantic verifier backend selection in live_wiring.py.

No network, no LiveKit — pure unit test of `_build_semantic_verifier`'s
decision logic (PLAN-20260910 slice 3): the existing `judge:` config block
is the opt-in fallback flag, reused as-is (no new config surface). Absent or
unresolved judge config -> RuleBasedSemanticVerifier (zero regression for
targets without a judge:). A ready judge config -> LLMSemanticVerifier with
the resolved provider/model/base_url/api_key.
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


def test_no_judge_config_falls_back_to_rule_based():
    verifier = _build_semantic_verifier(_cfg(judge=None))
    assert isinstance(verifier, RuleBasedSemanticVerifier)


def test_judge_missing_from_cfg_attr_falls_back_to_rule_based():
    """cfg objects without a `judge` attribute at all (older Config shape,
    or a bare test double) must not crash `getattr(cfg, "judge", None)`."""
    verifier = _build_semantic_verifier(SimpleNamespace(simulator=SimpleNamespace(provider="google", api_key="k")))
    assert isinstance(verifier, RuleBasedSemanticVerifier)


def test_http_openai_judge_selects_llm_verifier_with_resolved_fields():
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
    assert verifier.provider == "openai"
    assert verifier.model == "wwwww"
    assert verifier.base_url == "http://localhost:20128/v1"
    assert verifier.api_key == "sk-test"


def test_http_anthropic_judge_selects_llm_verifier_with_anthropic_provider():
    from livekit_agent_simulator.config import JudgeConfig

    judge = JudgeConfig(
        base_url="https://gateway.example/v1",
        api_key="sk-test",
        model="claude-x",
        endpoint_type="anthropic",
    )
    verifier = _build_semantic_verifier(_cfg(judge=judge))
    assert isinstance(verifier, LLMSemanticVerifier)
    assert verifier.provider == "anthropic"


def test_gemini_fallback_judge_uses_simulator_key_when_no_base_url():
    """No judge.base_url + provider: google simulator -> the Gemini fallback
    judge path (resolve_judge's `mode="gemini"`), authenticated with the
    SAME key the legacy caller bridge already uses (no new credential)."""
    from livekit_agent_simulator.config import JudgeConfig

    judge = JudgeConfig(model="gemini-flash-latest")
    verifier = _build_semantic_verifier(_cfg(judge=judge, sim_provider="google", sim_api_key="sim-key"))
    assert isinstance(verifier, LLMSemanticVerifier)
    assert verifier.provider == "gemini"
    assert verifier.api_key == "sim-key"


def test_judge_configured_but_not_ready_falls_back_to_rule_based():
    """base_url set without api_key -> resolve_judge.ready is False; must
    fail SAFE to the baseline, never raise and never silently skip
    validation (an unvalidated utterance must never reach LiveKit)."""
    from livekit_agent_simulator.config import JudgeConfig

    judge = JudgeConfig(base_url="http://localhost:20128/v1", api_key=None)
    verifier = _build_semantic_verifier(_cfg(judge=judge))
    assert isinstance(verifier, RuleBasedSemanticVerifier)
