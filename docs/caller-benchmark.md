# Caller Contract benchmark (baseline 7c23667 + hardening 118d9fe)

Date: 2026-09-11. Machine: Windows x64, Python 3.12. All numbers measured
locally on this machine unless noted; re-run the commands below to reproduce.
This file records evidence only — no tuning decisions are made here.

## 1. Semantic verifier latency

| Backend | Per classify() | Source |
|---|---|---|
| Tier-1 rule-based | ~0.01 ms | 2000 calls over 10 golden utterances, mean of 3 runs (16/32/31 ms per 2000) |
| Tier-3 LLM judge (gemini-flash-latest) | ~2.0–4.4 s | `scripts/benchmark_semantic_judge.py`, 10/10 golden PASS, 10 LLM calls / 10 cases |
| Tier-3 via local openai-compatible proxy (`wwwww`) | ~3.5–8.5 s | same script with `LKS_SEMANTIC_JUDGE_PROVIDER=openai`, 9/10 PASS |

Implication (evidence, not policy): the LLM judge is ~5 orders of magnitude
slower than the rule baseline per turn. Any per-turn use in a live call path
must budget seconds of added latency per `do:` turn, or restrict the judge to
offline/CI gating. The rule baseline stays the safe default for latency;
the judge buys recall (target evidence, paraphrase generalization).

## 2. Golden reject-rate profile (10 cases)

Tier-1 rule-based (from `pytest tests/test_semantic_golden.py`): 8 pass +
4 xfail — the 4 documented gaps are exactly the recall the judge closes
(target evidence x2, paraphrase generalization, delivery-date inference).

Tier-3 Gemini (`benchmark_semantic_judge.py`): 10/10 PASS — every golden
verdict reached for the right reason (ACT_MISMATCH / SEMANTIC_TARGET_MISMATCH
/ LOW_CONFIDENCE / FORBIDDEN_INTENT_DETECTED as the fixture requires).

## 3. Validation retry behavior (max_retries = 2, driver.py)

Bounded: at most 3 validator attempts per `do:` turn (`max_retries + 1`).
Transport failures (`LanguageGenerationError`) do NOT consume the retry
budget — they fail the behavior immediately as LANGUAGE_GENERATION_ERROR.
Stale-identity / refused-publish spins are capped by a separate liveness
guard (`max_stalled_spins = max_retries + 1`) and surface as TRANSPORT_ERROR,
never as a caller violation. Exhaustion of all validator attempts surfaces
the LAST verdict and maps to CALLER_BEHAVIOR_VIOLATION (no publish).

Worst-case LLM-judge cost per `do:` turn: 3 classify calls (one per attempt).
At ~2–4 s/call that is up to ~12 s of added latency on a fully-retried turn.

## 4. TTS (Kitten nano EN fp16 via sherpa-onnx 1.13.8, warmed backend)

| Utterance (chars) | Audio | Synth time | RTF |
|---|---|---|---|
| sedan greeting (44) | 3325 ms | 844 ms | 0.25 |
| price negotiate (52) | 4050 ms | 1031 ms | 0.25 |
| delivery question (33) | 2350 ms | 625 ms | 0.27 |
| goodbye (30) | 2475 ms | 688 ms | 0.28 |

First synth after construct includes lazy backend init (~3 s once, RTF 0.92
on that call); steady-state RTF ~0.25–0.28. Same 4 utterances via SAPI
(`synthesize_pcm16_mono`): ~1.0–1.2 s each — comparable wall-clock on this
machine, sherpa wins on determinism (pinned bytes), not speed.

Model: `kitten-nano-en-v0_1-fp16` (~25.6 MB), pinned SHA, cache
`~/.cache/lks/tts-models/`. Cold construct measured ~0 ms (lazy init happens
on first synthesize, not construction).

## 5. Reproduce

```bash
# judge latency + golden pass rate (needs LKS_SEMANTIC_JUDGE_API_KEY in .env)
.venv/Scripts/python scripts/benchmark_semantic_judge.py
# rule baseline latency
.venv/Scripts/python -m pytest tests/test_semantic_golden.py -q
# retry/gate behavior (mocked, CI)
.venv/Scripts/python -m pytest tests/test_contract_driver.py tests/test_contract_validator.py -q
```
