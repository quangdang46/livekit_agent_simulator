"""Slice 2 benchmark: run the Tier-3 judge live over golden utterances.

Repo-agnostic per AGENTS.md ("no consumer env vars in core config schema",
"core stays repo-agnostic; consumer fit only under target .agent-sim/"): this
script never reads another project's config.yaml. It reads the judge API key
from this repo's own local `.env` (gitignored) or the environment, and reports
per-case: expected vs observed act/target, confidence, verdict reason match,
and latency. Exit 0 always (evidence report, not a gate).

Setup:
    echo 'LKS_SEMANTIC_JUDGE_API_KEY=<your-key>' >> .env
    # optional: LKS_SEMANTIC_JUDGE_PROVIDER=gemini|openai (default gemini)
    #           LKS_SEMANTIC_JUDGE_MODEL=<model id>
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

_KEY_VAR = "LKS_SEMANTIC_JUDGE_API_KEY"
_PROVIDER_VAR = "LKS_SEMANTIC_JUDGE_PROVIDER"
_MODEL_VAR = "LKS_SEMANTIC_JUDGE_MODEL"
_BASE_URL_VAR = "LKS_SEMANTIC_JUDGE_BASE_URL"


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (stdlib only): does not override already-set env vars."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _judge_api_key() -> str:
    _load_dotenv(ROOT / ".env")
    key = os.environ.get(_KEY_VAR)
    if not key:
        raise SystemExit(
            f"{_KEY_VAR} not set. Add it to this repo's .env (gitignored) or export it — "
            "never read another project's config.yaml (see AGENTS.md: core stays repo-agnostic)."
        )
    return key


class _CachingVerifier:
    """Wraps a SemanticVerifierProtocol so this script's own inspection of
    ObservedAct and ``ContractValidator.validate()``'s internal call (when it
    reaches the semantic step at all) share ONE network round trip per case.

    Scoped to exactly one case per instance, so memoization is unconditional:
    the first ``classify()`` call hits the network, every subsequent call on
    this instance returns the cached ObservedAct. Benchmark-only concern
    (never used in live_wiring/production) — without it, calling
    ``classify()`` once directly to inspect fields PLUS letting
    ``validate()`` call it again internally doubles every case's LLM
    cost/latency in the measurement."""

    def __init__(self, inner):
        self._inner = inner
        self.last_observed = None
        self.calls = 0

    def classify(self, utterance, contract):
        if self.calls == 0:
            self.calls += 1
            self.last_observed = self._inner.classify(utterance, contract)
        return self.last_observed


def main() -> None:
    from livekit_agent_simulator.caller_contract import (
        BehaviorContract,
        CandidateUtterance,
        ContractConstraints,
        GenerationIdentity,
    )
    from livekit_agent_simulator.caller_contract.semantic_llm import LLMSemanticVerifier
    from livekit_agent_simulator.caller_contract.validator import ContractValidator

    fixture = json.loads(
        (ROOT / "tests" / "fixtures" / "semantic_verifier" / "golden_cases.json").read_text()
    )["cases"]
    kwargs: dict[str, str] = {
        "api_key": _judge_api_key(),
        "provider": os.environ.get(_PROVIDER_VAR, "gemini"),
    }
    if os.environ.get(_MODEL_VAR):
        kwargs["model"] = os.environ[_MODEL_VAR]
    if os.environ.get(_BASE_URL_VAR):
        kwargs["base_url"] = os.environ[_BASE_URL_VAR]
    verifier = LLMSemanticVerifier(**kwargs)

    rows = []
    total_calls = 0
    for case in fixture:
        spec = case["contract"]
        c = spec.get("constraints", {})
        contract = BehaviorContract(
            behavior=spec["behavior"],
            target=spec.get("target"),
            constraints=ContractConstraints(
                max_turns=c.get("max_turns", 3),
                forbidden_intents=list(c.get("forbidden_intents", [])),
            ),
        )
        claim = case["candidate_claim"]
        candidate = CandidateUtterance(
            act=claim["act"], target=claim.get("target"), slots={},
            utterance=case["utterance"],
            identity=GenerationIdentity(behavior_id="bench", turn_id=1, generation_id=1),
        )
        cached = _CachingVerifier(verifier)
        t0 = time.monotonic()
        try:
            # Always classify once directly so ObservedAct is inspectable
            # even when the deterministic layer short-circuits validate()
            # before reaching the semantic step (e.g. a lexical forbidden-
            # intent hit). ContractValidator reuses this same cached result
            # if it does call classify() internally — never a second call.
            observed = cached.classify(case["utterance"], contract)
            result = ContractValidator(semantic_verifier=cached).validate(candidate, contract)
            err = None
        except Exception as exc:  # noqa: BLE001 — evidence report
            result, observed, err = None, None, f"{type(exc).__name__}: {exc}"[:160]
        dt_ms = (time.monotonic() - t0) * 1000
        total_calls += cached.calls

        exp = case["expected"]
        if err is not None:
            match = f"ERROR {err}"
        else:
            assert result is not None and observed is not None
            verdict_ok = (result.is_valid() and exp["verdict"] == "valid") or (
                not result.is_valid() and exp["verdict"] == "reject"
                and result.reason == exp["reason"]
            )
            act_ok = exp.get("act") is None or observed.act == exp["act"]
            tgt_ok = exp.get("target") is None or observed.target == exp["target"]
            conf_ok = (
                (exp.get("confidence_min") is None or observed.confidence >= exp["confidence_min"])
                and (exp.get("confidence_max") is None or observed.confidence <= exp["confidence_max"])
            )
            tags_ok = all(
                t in observed.all_acts for t in exp.get("all_acts_contains", [])
            )
            match = "PASS" if (verdict_ok and act_ok and tgt_ok and conf_ok and tags_ok) else (
                f"MISMATCH verdict_ok={verdict_ok} act={observed.act} "
                f"target={observed.target} conf={observed.confidence:.2f} "
                f"reason={result.reason} all_acts={observed.all_acts}"
            )
        rows.append((case["id"], f"{dt_ms:.0f}ms", match))

    width = max(len(r[0]) for r in rows)
    passed = sum(1 for r in rows if r[2] == "PASS")
    for cid, dt, match in rows:
        print(f"{cid:<{width}}  {dt:>7}  {match}")
    print(
        f"\n{passed}/{len(rows)} golden cases pass on Tier-3 "
        f"{kwargs['provider']}:{verifier.model} judge "
        f"({total_calls} LLM call(s) for {len(rows)} case(s))"
    )


if __name__ == "__main__":
    main()
