# Plan Report

## Summary (read this first)
- **You asked:** Port LiveKit evals *patterns* into lk-sim with custom LLM via OpenAI-compatible HTTP gateway; no `openai_compatible` provider enum.
- **What is going on:** Judge was DIY Gemini-only; now `evals/` supports HTTP (`base_url`) + Gemini fallback.
- **We recommend:** Package **`evals/`** with clean seams (types / evidence / prompt / backend Protocol / runner / aggregate / relevancy / presets). HTTP via `endpoint_type` openai|anthropic; Gemini when no `base_url`. Orchestrator only calls `evals.runner`. Do **not** depend on `livekit-agents`.
- **Risk:** Medium
- **Status:** Implemented — reply go ahead was received

## Feature planning
- **Recommended approach:**
  1. New package `src/livekit_agent_simulator/evals/` — Clean Architecture lite (ports & adapters), matching repo style (`caller` PromptSection Protocol, `plugins` VerifyPlugin).
  2. **Module map (one PR, keep files small):**

     ```
     evals/
       __init__.py          # public API only
       types.py             # Verdict, CriterionScore, JudgmentResult
       evidence.py          # turns + tools → evidence packet (no LLM)
       prompt.py            # system + user builders (no I/O)
       resolve.py           # JudgeConfig: yaml literal → JUDGE_* → gemini key
       backend.py           # Protocol: complete_json(system, user) -> str
       backends/
         http_openai.py     # POST {base}/chat/completions, stream:false
         http_anthropic.py  # POST {base}/messages, stream:false
         gemini.py          # google.genai JSON mime
       relevancy.py         # Hamming step A (exclude irrelevant criteria)
       presets.py           # builtin dim criteria (LiveKit/Hamming-shaped)
       aggregate.py         # multi-judge all|majority|any (+ maybe scoring)
       runner.py            # evidence → (relevancy) → backend → parse/normalize
     ```

     | Layer | Responsibility | Depends on |
     |-------|----------------|------------|
     | `types` / `evidence` / `prompt` | Pure data + text | nothing LLM |
     | `backend` Protocol | Transport swap | `types` only |
     | `relevancy` / `presets` / `aggregate` | Policy | `types`, optionally `backend` |
     | `runner` | Orchestration | all above |
     | `run_orchestrator` | Call site | `evals.runner` only |

  3. **Extend later without rewriting runner:** new gateway = new `backends/foo.py` implementing Protocol; new dim = entry in `presets.py`; new aggregate mode = `aggregate.py` only.
  4. Config literals:

     ```yaml
     judge:
       base_url: http://localhost:8080/v1
       api_key: sk-...
       model: gpt-4o-mini
       endpoint_type: openai   # or anthropic
     ```

     Optional `JUDGE_*` if field omitted. See **Env / secrets** + **Judge logic audit** + **Clean-code layout** below.
  5. `gemini/judge.py` → thin re-export to `evals.runner` (compat; no business logic left there)

- **Prior art:** LiveKit [judge.py](https://github.com/livekit/agents/blob/main/livekit-agents/livekit/agents/evals/judge.py); OpenAI `/chat/completions` + Anthropic `/messages` wire formats
- **Integration points:** `JudgeConfig` (+ `base_url`, `api_key`, `endpoint_type`); orchestrator imports `evals.runner`; `gemini/judge.py` re-exports
- **Sub-agents used:** skipped
- **Reject:** `provider: openai_compatible` enum; fat God-module; importing `livekit-agents`. **Keep** `judge.endpoint_type: openai|anthropic` (default openai) as HTTP wire selector.
- **Open questions:**
  1. Ship **one PR** (layout + HTTP + schema + relevancy + presets) — **yes** (locked)
  2. Prefer literals in `judge:` yaml; env fallbacks optional only

## Evidence
1. **LiveKit judge.py:** JudgmentResult + function-forced verdict — adapt without ChatContext
2. **Our repo patterns:** `caller/prompt_sections.py` PromptSection Protocol; `plugins/api.py` VerifyPlugin — same Protocol-adapter style for judge backends
3. **HTTP wire:** OpenAI `/chat/completions` and Anthropic `/messages` with `stream:false`

## Steps (simple checklist)
1. [x] Scaffold `evals/` modules per map (types → evidence → prompt → backend Protocol → backends → runner)
2. [x] `resolve.py` + extend `JudgeConfig` (`base_url`, `api_key`); pick http vs gemini backend
3. [x] Schema: `maybe` / confidence / evidence; relevancy; presets; aggregate
4. [x] Wire orchestrator → `evals.runner`; thin `gemini/judge.py` re-export; `goals_met` null-guard
5. [x] Unit tests per layer (mock backend, no live network); GUIDE example
6. [x] `pytest`

## Files to touch
- `src/livekit_agent_simulator/evals/*`
- `src/livekit_agent_simulator/config.py`
- `src/livekit_agent_simulator/gemini/judge.py`
- `templates/GUIDE.md`, this plan

## Env / secrets for Judge — research (one-time tick)

### Current state (`verified`)

`config.py` explicitly:

> credentials are written directly in the file… **No env-var substitution in v1.**

| Mechanism | Exists? |
|-----------|---------|
| `${JUDGE_API_KEY}` expansion in YAML | **No** |
| `os.environ` resolve in `load_config` | **No** |
| Install agent: copy target `.env` → write literal into `config.yaml` | Documented in `installation.md` for LiveKit/Google only — not Judge |
| Judge today | Enabled only if `judge:` block present; always uses `simulator.google_api_key` |

So the `${JUDGE_API_KEY}` line in an earlier plan draft was **aspirational**, not implemented.

### Industry pattern (do this instead of full YAML `${}` expand)

| Source | Pattern |
|--------|---------|
| [OpenAI Python](https://github.com/openai/openai-python) | Client args or env for key/base URL |
| lk-sim LiveKit/Google | Still yaml-literal (gitignored `.agent-sim/`) — keep for room creds |

**Do not** add general `${VAR}` substitution for all of `config.yaml` (would break “secrets in file” contract + surprise expansions).  
**Do** add a **scoped** resolver only for Judge HTTP fields at load (or first judge call).

### Recommended resolution (Judge only)

**`api_key`** (first non-empty wins):

1. `judge.api_key` literal in yaml  
2. `JUDGE_API_KEY`  
3. else → if HTTP mode required, skip/fail judge with clear note; if Gemini mode → `simulator.google_api_key`

**`base_url`** (first non-empty wins):

1. `judge.base_url` literal  
2. `JUDGE_BASE_URL`  
3. empty → **Gemini** backend (legacy)

**`model`**: yaml `judge.model` → else `JUDGE_MODEL` → else default (`gemini-2.5-flash` or leave required for HTTP).

**No** general `${}` expand. HTTP wire: `judge.endpoint_type` (`openai` default | `anthropic`).
### Setup (config literals — preferred)

```yaml
judge:
  base_url: http://localhost:8080/v1
  api_key: sk-...
  model: gpt-4o-mini
  endpoint_type: openai
```

Optional: omit a field and resolve from `JUDGE_*`. Not required when yaml is complete.

**Enable rule:** Run judge when scenario has PassCriteria and `judge:` block is present with resolvable creds (literal and/or env). Gemini when no `base_url` → `simulator.google_api_key`.

If PassCriteria but no creds → `verdict: skipped` with notes (don’t hard-fail suite).

### Preflight
Add checks: `judge.http` pass/warn when base_url set (ping `/models` optional); never print full api_key in snapshot (`judge_api_key_set: true`).

### Out of scope for this tick
- Expanding `${}` across LiveKit/Google yaml fields  
- Reading target `.env` file automatically inside `load_config` (keep agent-install discovery as today)

---

## Judge logic audit (Exa + LiveKit docs + our code) — **chưa đủ parity**

Sources (2026-07): [Hamming LLM grader rubric](https://hamming.ai/resources/llm-grader-voice-agent-call-scoring-rubric), [LiveKit judge.py](https://github.com/livekit/agents/blob/main/livekit-agents/livekit/agents/evals/judge.py), [LiveKit test framework — JudgeGroup](https://docs.livekit.io/agents/start/testing/test-framework/).

### Pipeline / layering — **ĐÚNG** (`verified`)

| Rule | Ours | Industry |
|------|------|----------|
| Deterministic Asserts hard-fail before soft LLM | `run_orchestrator.py` assert → `status=failed`; judge still soft | Hamming: don’t LLM-score facts software can prove |
| PassCriteria soft unless `--strict-judge` | `ops.py` | LiveKit: separate hard asserts vs LLM judges |
| Evidence packet = transcript + tool spans | `gemini/judge.py` | Hamming baseline (audio optional P2) |
| Multi-judge `all\|majority\|any` | `judge_run_multi` | LiveKit `JudgeGroup` modes (`all_passed` / majority-style) |
| Criteria-only evaluation | system prompt “Evaluate ONLY against the criteria” | LiveKit `_LLMJudge` “Criteria: …” |

### Judgment semantics — **GAPS** (fix in one PR)

| Gap | LiveKit / Hamming | Ours today | Fix |
|-----|-------------------|------------|-----|
| Ternary verdict | `pass\|fail\|maybe`; `maybe` ≠ pass; score 1/0.5/0 | Binary `pass\|fail` only | Add `maybe`; suite: soft unless `--strict-judge`; multi-`all` requires all `pass` |
| Structured force | LiveKit `submit_verdict` **function tool** | Gemini `response_mime_type=json`; HTTP TBD | HTTP: `response_format=json_object` **or** tool-call `submit_verdict` when model supports tools |
| Confidence / escalation | Hamming `confidence` + `needs_human_review` | Missing | Add to JSON schema; store in `summary.verdict` |
| No invented facts | Hamming: grade only from evidence | Weak wording | Strengthen system prompt |
| Relevancy gate | Hamming step A / Cekura trigger | Missing | Same PR: per-criterion/judge relevancy |
| Builtin dim judges | LiveKit `accuracy_judge`, `coherence_judge`, … | Flat string criteria only | Same PR: preset factories |
| `goals_met` without `cfg.judge` | — | Calls `judge_goals(cfg.judge, …)` even if `judge` is None → crash risk | Guard: skip or require resolved judge |

### Verdict for implement

1. **Config** — set `judge.base_url` / `api_key` / `model` in yaml; env optional.  
2. **Layering** — keep as-is (Assert hard / PassCriteria soft).  
3. **One PR:** clean `evals/` package + HTTP OpenAI backend + Gemini backend + `maybe`/confidence/evidence + relevancy + presets + `goals_met` guard.  
4. **Still defer (out of scope):** audio-native judge, Hamming SaaS / Observe.

---

## Clean-code layout (locked)

Principles aligned with repo (`caller` / `plugins`):

| Rule | Practice |
|------|----------|
| Single responsibility | One concern per module; no HTTP inside `prompt.py` |
| Dependency inversion | Runner depends on `JudgeBackend` Protocol; backends are swappable |
| Open for extension | New backend / preset / aggregate mode = add file or table row, don’t edit runner guts |
| Closed for modification | Orchestrator stays dumb: build evidence → `run_judge(...)` → emit verdict |
| Pure core | `types` / `evidence` / `prompt` / `aggregate` unit-testable without network |
| Fail soft | Unparseable / missing creds → `verdict: error\|skipped` + notes; never crash suite |
| Public API surface | Export from `evals/__init__.py` only what orchestrator needs |

Backend Protocol (sketch):

```python
class JudgeBackend(Protocol):
    async def complete_json(self, *, system: str, user: str) -> str: ...
```

Factory: `backend_for(cfg: JudgeConfig, google_api_key: str) -> JudgeBackend`  
→ `HttpOpenAIBackend` if `base_url` else `GeminiBackend`.

---

## If you want more detail
HTTP judge uses `endpoint_type`: `openai` → `/chat/completions`, `anthropic` → `/messages`, both with `stream:false`.
---

## Hamming / industry judgment model (deep research)

Sources: [LLM grader rubric](https://hamming.ai/resources/llm-grader-voice-agent-call-scoring-rubric), [two-step eval](https://hamming.ai/resources/why-engineering-teams-choose-hamming), [sim+eval](https://hamming.ai/blog/why-voice-agents-need-simulations-and-evaluations), Cekura LLM-judge metric design, LiveKit `Judge`/`maybe`, our `docs/caller-behavior-research.md`.

### How Hamming structures Judgment

1. **Layer split (hard rule)**  
   - **Deterministic first:** disclosures, tool success/fail, IDs, latency, transfers, write outcomes.  
   - **LLM second:** semantic task completion, policy interpretation, empathy, flow, hallucination *risk*.  
   - Never spend LLM on facts software can prove — “friendly wrong call” trap (sounds good, tool failed).

2. **Two-step LLM pipeline (anti-flake)**  
   - Step A **relevancy**: should this assertion/metric apply to *this* call?  
   - Step B **evaluate**: only if relevant.  
   - Cuts false fails from irrelevant guardrails (Hamming cites ~95–96% human agreement with better models + this pipeline).

3. **Multi-dimension scorecard, not one “quality”**  
   Independent dims (weights example): task_completion, factual_accuracy, policy_compliance, conversation_flow, empathy, escalation, evidence_quality.  
   Each dim: `score` (0–5) + `pass` bool + `evidence_spans` + `rationale` + `confidence` (low|medium|high).  
   Call-level: `critical_failure`, `needs_human_review`, `grader_version`.

4. **Evidence packet required**  
   Transcript **with timestamps** + tool log + metadata + (ideally) audio pointers. Grader must not invent facts; missing evidence → lower `evidence_quality` / escalate human.

5. **Human triage, not oracle**  
   LLM grader = triage + CI soft signal. High-risk / low-confidence → review queue. Production failure → promote to regression scenario (sim loop).

6. **Assertions vs holistic outcomes**  
   Hamming product evals: conversational metrics + **expected outcomes** + compliance guardrails. Persona tests carry typed assertions (outcome / tool / side_effect / recovery), not only free-text “was the call good?”.

### Peers (same idea, different packaging)

| Platform | Judgment handle |
|----------|-----------------|
| **LiveKit evals** | Per-judge `pass\|fail\|maybe`; `JudgeGroup` aggregate; tool-forced verdict; builtin criteria strings |
| **Cekura** | Per-metric LLM judge + optional **trigger** (relevancy); N/A/skip; scoped FAILURE CONDITIONS; spirit-vs-letter |
| **Voicetest-style** | Per-metric analysis→score→confidence; threshold pass; separate rule/includes asserts |

### Mapping → lk-sim (do / don't)

| Hamming practice | lk-sim today | Plan impact |
|------------------|--------------|-------------|
| Deterministic vs LLM | Assert hard; PassCriteria soft | **Keep.** HTTP/Gemini only for PassCriteria / `llm_bool` / optional `goals_met` — do not move tool_order/recovery into LLM |
| Relevancy step | Missing | Add optional per-judge `when:` / relevancy prompt or `builtin` skip-if-no-tools (Cekura trigger / Hamming step A) |
| Dimensional rubric | Flat criteria list / multi `judges[]` | Map Hamming dims → named `judges[]` (id=`task_completion`…) + LiveKit presets; aggregate already `all\|majority\|any` |
| Evidence spans | Transcript turns + tools in prompt; no structured spans in output | Evolve judge JSON: `evidence`, `confidence`, `maybe`/`needs_review`; store in `summary.judge` |
| Confidence / human | Soft judge; `--strict-judge` | Treat `maybe` + low confidence as soft-fail (not CI hard) unless strict |
| Audio-native | WAV in reports; judge text-only | Defer (P2.E); transcript+tools is enough for v1 HTTP judge |
| SaaS monitoring | Out of scope | Don't build Hamming Observe |

### Judgment result shape (recommended for implement)

```json
{
  "verdict": "pass|fail|maybe",
  "confidence": "low|medium|high",
  "score": 0-100,
  "criteria": [{"criterion": "...", "met": true, "evidence": "...", "relevant": true}],
  "critical_failure": false,
  "notes": "..."
}
```

Relevancy: if `relevant=false`, criterion excluded from aggregate (Hamming step A / Cekura trigger).

### Bottom line — **one PR**
1. Scaffold clean `evals/` (Protocol backends + pure core)  
2. HTTP OpenAI + Gemini adapters  
3. Schema + relevancy + presets + aggregate  
4. Wire orchestrator; thin gemini re-export  
5. Do **not** weaken Assert layer
