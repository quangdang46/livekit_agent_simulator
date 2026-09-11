"""Live-wiring acceptance: the run path uses the Tier-1 rule-based baseline.

Run 015 (Phase D) pinned this: live evidence (runs 010-015) proved the
synchronous Tier-3 classify() path unusable on runs — the asserts-judge HTTP
proxy returns HTTP 200 with EMPTY content, so every attempt ended
``VERIFIER_UNAVAILABLE``. The Tier-3 LLM judge remains for OFFLINE use only
(``scripts/benchmark_semantic_judge.py``).

No network, no LiveKit — pure unit tests of ``_build_semantic_verifier``.
"""

from __future__ import annotations

from types import SimpleNamespace

from livekit_agent_simulator.caller_contract.live_wiring import _build_semantic_verifier
from livekit_agent_simulator.caller_contract.semantic import RuleBasedSemanticVerifier


def _cfg(*, judge=None, sim_provider="openai", sim_api_key="sim-key"):
    return SimpleNamespace(
        simulator=SimpleNamespace(provider=sim_provider, api_key=sim_api_key),
        judge=judge,
    )


def test_run_path_ignores_a_ready_http_judge():
    """Even a fully-resolved HTTP judge must NOT be selected on the run path.

    This is the run-010/run-015 regression in reverse: previously these tests
    asserted an LLMSemanticVerifier here, and every live run paid for it with
    3x VERIFIER_UNAVAILABLE per turn. The ``judge:`` block now serves only
    PassCriteria/asserts judging (evals/resolve.py), never semantic
    verification.
    """
    from livekit_agent_simulator.config import JudgeConfig

    judge = JudgeConfig(
        base_url="http://localhost:20128/v1",
        api_key="sk-test",
        model="claude-x",
        endpoint_type="openai",
        temperature=0.0,
    )
    for sim_provider in ("openai", "google"):
        verifier = _build_semantic_verifier(_cfg(judge=judge, sim_provider=sim_provider))
        assert isinstance(verifier, RuleBasedSemanticVerifier), sim_provider


def test_run_path_uses_the_baseline_with_no_judge_configured():
    for sim_provider in ("openai", "google"):
        verifier = _build_semantic_verifier(_cfg(judge=None, sim_provider=sim_provider))
        assert isinstance(verifier, RuleBasedSemanticVerifier), sim_provider


def test_run_path_never_skips_validation():
    """The baseline is fail-closed by construction: an unvalidated utterance
    must never reach LiveKit, regardless of config shape."""
    # cfg without a judge attribute at all (older Config shape, bare doubles)
    verifier = _build_semantic_verifier(
        SimpleNamespace(simulator=SimpleNamespace(provider="openai", api_key="k"))
    )
    assert isinstance(verifier, RuleBasedSemanticVerifier)
    # cfg without a simulator key either — nothing to resolve, still safe
    verifier = _build_semantic_verifier(
        SimpleNamespace(simulator=SimpleNamespace(provider="", api_key=""))
    )
    assert isinstance(verifier, RuleBasedSemanticVerifier)
