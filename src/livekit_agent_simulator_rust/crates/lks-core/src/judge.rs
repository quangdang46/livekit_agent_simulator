//! PassCriteria LLM judge (port of `evals/resolve.py` + `evals/backends/http_openai.py`
//! + `evals/runner.py` core slice).
//!
//! Resolution: `judge.endpoint_type → JUDGE_ENDPOINT_TYPE → "openai"`;
//! `base_url` set → http mode (needs judge.api_key / JUDGE_API_KEY);
//! no base_url → the SIMULATOR api_key drives a Gemini REST call (not yet wired —
//! the http path is the primary one and works with any OpenAI-wire gateway).
//! `model → JUDGE_MODEL → "gemini-2.5-flash"`; temperature default 0.0.

use serde_json::{json, Map, Value as Json};

use crate::config::JudgeConfig;
use crate::evals::{apply_relevancy, parse_judgment_payload, repair_json, JudgmentResult};

pub const DEFAULT_JUDGE_MODEL: &str = "gemini-2.5-flash";

#[derive(Debug, Clone)]
pub struct ResolvedJudge {
    pub model: String,
    pub temperature: f64,
    pub base_url: Option<String>,
    pub api_key: Option<String>,
    pub mode: String, // http | gemini
    pub endpoint_type: String,
    pub ready: bool,
    pub skip_reason: String,
}

pub fn env_var(name: &str) -> Option<String> {
    let v = std::env::var(name).ok()?;
    let s = v.trim().to_string();
    if s.is_empty() {
        None
    } else {
        Some(s)
    }
}

pub fn resolve_judge(judge_cfg: Option<&JudgeConfig>, sim_api_key: Option<&str>) -> ResolvedJudge {
    let Some(cfg) = judge_cfg else {
        return ResolvedJudge {
            model: DEFAULT_JUDGE_MODEL.into(),
            temperature: 0.0,
            base_url: None,
            api_key: None,
            mode: "gemini".into(),
            endpoint_type: "openai".into(),
            ready: false,
            skip_reason: "No judge: block in config.".into(),
        };
    };
    let model = cfg
        .model
        .clone()
        .filter(|m| !m.trim().is_empty())
        .or_else(|| env_var("JUDGE_MODEL"))
        .unwrap_or_else(|| DEFAULT_JUDGE_MODEL.into());
    let endpoint_type = {
        let raw = cfg
            .endpoint_type
            .trim()
            .to_lowercase()
            .pipe_nonempty()
            .or_else(|| env_var("JUDGE_ENDPOINT_TYPE"))
            .unwrap_or_else(|| "openai".into());
        if raw == "openai" || raw == "anthropic" {
            raw
        } else {
            "openai".into()
        }
    };
    let base_url = cfg
        .base_url
        .clone()
        .filter(|b| !b.trim().is_empty())
        .or_else(|| env_var("JUDGE_BASE_URL"));
    let api_key = cfg
        .api_key
        .clone()
        .filter(|k| !k.trim().is_empty())
        .or_else(|| env_var("JUDGE_API_KEY"));
    let gkey = sim_api_key
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty());

    if let Some(base) = base_url {
        let api_key = api_key.unwrap_or_default();
        if api_key.is_empty() {
            return ResolvedJudge {
                model,
                temperature: cfg.temperature,
                base_url: Some(base),
                api_key: None,
                mode: "http".into(),
                endpoint_type,
                ready: false,
                skip_reason: "HTTP judge needs judge.api_key or JUDGE_API_KEY.".into(),
            };
        }
        return ResolvedJudge {
            model,
            temperature: cfg.temperature,
            base_url: Some(base.trim_end_matches('/').to_string()),
            api_key: Some(api_key),
            mode: "http".into(),
            endpoint_type,
            ready: true,
            skip_reason: String::new(),
        };
    }
    if gkey.is_none() {
        return ResolvedJudge {
            model,
            temperature: cfg.temperature,
            base_url: None,
            api_key: None,
            mode: "gemini".into(),
            endpoint_type,
            ready: false,
            skip_reason: "Gemini judge needs a Gemini key (simulator.api_key with provider: google; no base_url)."
                .into(),
        };
    }
    ResolvedJudge {
        model,
        temperature: cfg.temperature,
        base_url: None,
        api_key: gkey,
        mode: "gemini".into(),
        endpoint_type,
        ready: true,
        skip_reason: String::new(),
    }
}

trait PipeNonempty {
    fn pipe_nonempty(self) -> Option<String>;
}
impl PipeNonempty for String {
    fn pipe_nonempty(self) -> Option<String> {
        if self.is_empty() {
            None
        } else {
            Some(self)
        }
    }
}

/// OpenAI-wire chat completions backend (port of `http_openai.py`).
pub struct HttpOpenAIBackend {
    pub base_url: String,
    pub api_key: String,
    pub model: String,
    pub temperature: f64,
    pub timeout_s: u64,
}

impl HttpOpenAIBackend {
    fn endpoint(&self) -> String {
        if self.base_url.ends_with("/chat/completions") {
            self.base_url.clone()
        } else {
            format!("{}/chat/completions", self.base_url)
        }
    }

    /// POST the judge prompt and return the raw completion text.
    pub async fn complete_json(&self, system: &str, user: &str) -> Result<String, String> {
        let body = json!({
            "model": self.model,
            "temperature": self.temperature,
            "stream": false,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
        });
        let client = reqwest::Client::builder()
            .timeout(std::time::Duration::from_secs(self.timeout_s))
            .build()
            .map_err(|e| format!("reqwest build: {e}"))?;
        let resp = client
            .post(self.endpoint())
            .bearer_auth(&self.api_key)
            .header("Content-Type", "application/json")
            .header("Accept", "application/json")
            .body(serde_json::to_string(&body).unwrap_or_default())
            .send()
            .await
            .map_err(|e| format!("HTTP judge unreachable: {e}"))?;
        let status = resp.status();
        if !status.is_success() {
            let err_body = resp
                .text()
                .await
                .unwrap_or_default()
                .chars()
                .take(500)
                .collect::<String>();
            return Err(format!("HTTP judge {status}: {err_body}"));
        }
        let payload: Json = resp
            .json()
            .await
            .map_err(|e| format!("HTTP judge parse: {e}"))?;
        let choices = payload
            .get("choices")
            .and_then(|v| v.as_array())
            .cloned()
            .unwrap_or_default();
        if choices.is_empty() {
            return Err(format!(
                "HTTP judge empty choices: {}",
                serde_json::to_string(&payload)
                    .unwrap_or_default()
                    .chars()
                    .take(300)
                    .collect::<String>()
            ));
        }
        let message = choices[0]
            .get("message")
            .and_then(|v| v.as_object())
            .cloned()
            .unwrap_or_default();
        let content = message.get("content").cloned().unwrap_or(Json::Null);
        let text = match content {
            Json::String(s) => s,
            Json::Array(parts) => parts
                .iter()
                .filter(|p| {
                    p.get("type")
                        .and_then(|t| t.as_str())
                        .map(|t| t == "text")
                        .unwrap_or(true)
                })
                .filter_map(|p| p.get("text").and_then(|t| t.as_str()))
                .collect::<Vec<_>>()
                .join(""),
            _ => String::new(),
        };
        if text.trim().is_empty() {
            return Err("HTTP judge returned empty content".into());
        }
        Ok(text)
    }
}

/// Anthropic Messages-wire backend (port of `http_anthropic.py`).
pub struct HttpAnthropicBackend {
    pub base_url: String,
    pub api_key: String,
    pub model: String,
    pub temperature: f64,
    pub max_tokens: u32,
    pub timeout_s: u64,
}

impl HttpAnthropicBackend {
    fn endpoint(&self) -> String {
        if self.base_url.ends_with("/messages") {
            self.base_url.clone()
        } else {
            format!("{}/messages", self.base_url)
        }
    }

    /// POST the judge prompt (Messages wire) and return the raw text.
    pub async fn complete_json(&self, system: &str, user: &str) -> Result<String, String> {
        let body = json!({
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "stream": false,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        });
        let client = reqwest::Client::builder()
            .timeout(std::time::Duration::from_secs(self.timeout_s))
            .build()
            .map_err(|e| format!("reqwest build: {e}"))?;
        let resp = client
            .post(self.endpoint())
            .header("Authorization", format!("Bearer {}", self.api_key))
            .header("x-api-key", &self.api_key)
            .header("anthropic-version", "2023-06-01")
            .header("Content-Type", "application/json")
            .header("Accept", "application/json")
            .body(serde_json::to_string(&body).unwrap_or_default())
            .send()
            .await
            .map_err(|e| format!("HTTP anthropic judge unreachable: {e}"))?;
        let status = resp.status();
        if !status.is_success() {
            let err_body = resp
                .text()
                .await
                .unwrap_or_default()
                .chars()
                .take(500)
                .collect::<String>();
            return Err(format!("HTTP anthropic judge {status}: {err_body}"));
        }
        let payload: Json = resp
            .json()
            .await
            .map_err(|e| format!("HTTP anthropic judge parse: {e}"))?;
        // content: str | [{type?: "text"|"thinking", text}] — take text parts.
        let text = match payload.get("content") {
            Some(Json::String(s)) => s.clone(),
            Some(Json::Array(parts)) => parts
                .iter()
                .filter(|p| {
                    p.get("type")
                        .and_then(|t| t.as_str())
                        .map(|t| t == "text")
                        .unwrap_or(true)
                })
                .filter_map(|p| p.get("text").and_then(|t| t.as_str()))
                .collect::<Vec<_>>()
                .join(""),
            _ => String::new(),
        };
        if text.trim().is_empty() {
            return Err("HTTP anthropic judge returned empty content".into());
        }
        Ok(text)
    }
}

/// Native Gemini generateContent backend (port of `backends/gemini.py`).
pub struct GeminiRestBackend {
    pub api_key: String,
    pub model: String,
    pub temperature: f64,
    pub timeout_s: u64,
}

impl GeminiRestBackend {
    fn endpoint(&self) -> String {
        format!(
            "https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent",
            self.model.trim_start_matches("models/")
        )
    }

    /// POST the judge prompt (generateContent wire) and return raw text.
    pub async fn complete_json(&self, system: &str, user: &str) -> Result<String, String> {
        let body = json!({
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "temperature": self.temperature,
                "responseMimeType": "application/json",
            },
        });
        let client = reqwest::Client::builder()
            .timeout(std::time::Duration::from_secs(self.timeout_s))
            .build()
            .map_err(|e| format!("reqwest build: {e}"))?;
        let resp = client
            .post(self.endpoint())
            .query(&[("key", &self.api_key)])
            .header("Content-Type", "application/json")
            .body(serde_json::to_string(&body).unwrap_or_default())
            .send()
            .await
            .map_err(|e| format!("gemini judge unreachable: {e}"))?;
        let status = resp.status();
        if !status.is_success() {
            let err_body = resp
                .text()
                .await
                .unwrap_or_default()
                .chars()
                .take(500)
                .collect::<String>();
            return Err(format!("gemini judge {status}: {err_body}"));
        }
        let payload: Json = resp
            .json()
            .await
            .map_err(|e| format!("gemini judge parse: {e}"))?;
        let text = payload["candidates"][0]["content"]["parts"]
            .as_array()
            .map(|parts| {
                parts
                    .iter()
                    .filter_map(|p| p.get("text").and_then(|t| t.as_str()))
                    .collect::<Vec<_>>()
                    .join("")
            })
            .unwrap_or_default();
        if text.trim().is_empty() {
            return Err("gemini judge returned empty content".into());
        }
        Ok(text)
    }
}

/// Build the judge user prompt (port of `build_user_prompt`).
pub fn build_user_prompt(
    pass_criteria: &[String],
    transcript: &str,
    tool_spans: &str,
    flow_digest: Option<&str>,
    goals_met: bool,
) -> String {
    let mut parts: Vec<String> = vec!["PASS CRITERIA:".into()];
    for c in pass_criteria {
        parts.push(format!("- {c}"));
    }
    parts.push(String::new());
    parts.push("TRANSCRIPT:".into());
    parts.push(if transcript.is_empty() {
        "(empty)".into()
    } else {
        transcript.to_string()
    });
    parts.push(String::new());
    parts.push("TOOL SPANS:".into());
    parts.push(if tool_spans.is_empty() {
        "(none)".into()
    } else {
        tool_spans.to_string()
    });
    if let Some(fd) = flow_digest {
        parts.push(String::new());
        parts.push(
            "FLOW EVENTS (node lifecycle — strong evidence for hold/advance behavior):".into(),
        );
        parts.push(fd.to_string());
    }
    if goals_met {
        parts.push(String::new());
        parts.push(
            "NOTE: This is a goals_met check. Evaluate whether the CALLER (simulated human) \
             stated or pursued each listed goal. Agent responses alone do not satisfy caller goals."
                .into(),
        );
    }
    parts.join("\n")
}

/// The 59-line judge system prompt (port of `JUDGE_SYSTEM` — verbatim).
pub const JUDGE_SYSTEM: &str = r#"You are an experienced reviewer of conversational AI interactions.

Review the transcript objectively, based only on what appears in the conversation.
Do not assume any product-specific requirements, hidden prompts, business logic, or
implementation details. Evaluate the conversation from the perspective of the end user.

Focus on:
- Whether the conversation achieved its goal
- Whether the agent understood the caller correctly
- Whether the conversation was coherent
- Whether responses were relevant
- Whether the conversation progressed naturally
- Whether questions were appropriate
- Whether confirmations were useful
- Whether there were unnecessary repetitions
- Whether the agent recovered well from misunderstandings
- Whether there were awkward or unnatural responses
- Whether important information appeared to be missing
- Whether the conversation remained consistent

Do not report issues simply because you would phrase something differently.
Only report issues that have a meaningful impact on clarity, correctness, efficiency, or
user experience. Always support findings with evidence from the transcript.

FLOW EVENTS are the agent's own published node-lifecycle digest. Repeating entries for
the same node indicate the flow held on that node across turns; transitions between nodes
show advancement.

When reviewing:
- Do not criticize stylistic differences unless they negatively affect usability.
- Distinguish between critical issues and minor wording improvements.
- Explain why something is problematic.
- Suggest better alternatives whenever possible.
- If something is acceptable, explicitly say it is OK.
- Be objective and avoid inventing problems that are not present.
- Quote the EXACT agent line (verbatim, in the caller's language) for each issue.
- Do not just say "met"/"not met" — an engineer must be able to act on the review.
- If no significant issue exists, say so explicitly in overall_summary.

Severity levels: Critical | Major | Minor | Suggestion

Return JSON with this structure:
{"verdict": "pass"|"fail"|"maybe",
 "score": 0-100,
 "confidence": "low"|"medium"|"high",
 "needs_human_review": bool,
 "overall_summary": str,
 "criteria": [{"criterion": str, "met": bool, "evidence": str}],
 "issues": [{"severity": str, "title": str, "detail": str, "agent_line": str, "impact": str, "recommendation": str}],
 "strengths": [str],
 "missing_checks": [str],
 "language_naturalness": str}
Return ONLY valid JSON."#;

/// Evidence packet: transcript + tool spans (port of `build_evidence_packet` core).
pub fn build_evidence_packet(
    turns: &[Map<String, Json>],
    tool_events: &[Map<String, Json>],
    flow_events: &[Map<String, Json>],
) -> (String, String, String) {
    // Transcript: Caller/Agent blocks with mm:ss.xx ranges (evidence.py
    // format_transcript) — elapsed time accumulates turn_taking_ms.
    let ms_to_mmss = |ms: f64| -> String {
        let total_s = ms / 1000.0;
        let minutes = (total_s as i64) / 60;
        let seconds = total_s % 60.0;
        format!("{minutes}:{seconds:05.2}")
    };
    let mut lines: Vec<String> = Vec::new();
    let mut elapsed_ms = 0.0f64;
    for t in turns {
        let start = ms_to_mmss(elapsed_ms);
        let ttm = t.get("turn_taking_ms").and_then(|v| v.as_f64()).unwrap_or(0.0);
        elapsed_ms += ttm;
        let end = ms_to_mmss(elapsed_ms);
        let time_range = format!("{start} - {end}");
        let user = t.get("user_text").and_then(|v| v.as_str()).unwrap_or("");
        let agent = t.get("agent_text").and_then(|v| v.as_str()).unwrap_or("");
        if !user.is_empty() {
            lines.push("Caller".into());
            lines.push(time_range.clone());
            lines.push(user.to_string());
            lines.push(String::new());
        }
        if !agent.is_empty() {
            lines.push("Agent".into());
            lines.push(time_range);
            lines.push(agent.to_string());
            lines.push(String::new());
        }
    }
    let transcript = if lines.is_empty() {
        "(empty)".to_string()
    } else {
        lines.join("\n")
    };

    // Tool spans: one compact JSON object per event (evidence.py format_tool_spans).
    let spans: Vec<String> = tool_events
        .iter()
        .map(|e| {
            let spec = e.get("spec").and_then(|v| v.as_object()).cloned().unwrap_or_default();
            json!({
                "kind": e.get("kind").cloned().unwrap_or(Json::Null),
                "turn": e.get("turn").cloned().unwrap_or(Json::Null),
                "name": spec.get("name").cloned().unwrap_or(Json::Null),
                "error": spec.get("error").cloned().unwrap_or(Json::Null),
                "duration_ms": spec.get("duration_ms").cloned().unwrap_or(Json::Null),
            })
            .to_string()
        })
        .collect();
    let tool_spans = if spans.is_empty() {
        "(none)".to_string()
    } else {
        spans.join("\n")
    };

    // Flow digest: opaque flow-lifecycle payloads, key=value per event
    // (evidence.py format_flow_digest — core never interprets payload keys).
    let mut flow_lines: Vec<String> = Vec::new();
    for e in flow_events {
        let payload = e
            .get("spec")
            .and_then(|v| v.as_object())
            .and_then(|s| s.get("payload"))
            .cloned()
            .unwrap_or(Json::Null);
        match payload.as_object() {
            Some(o) if !o.is_empty() => {
                let bits: Vec<String> = o
                    .iter()
                    .filter(|(k, _)| k.as_str() != "_seq" && k.as_str() != "ts")
                    .map(|(k, v)| format!("{k}={v}"))
                    .collect();
                flow_lines.push(if bits.is_empty() { payload.to_string() } else { bits.join(" | ") });
            }
            _ => flow_lines.push(payload.to_string()),
        }
    }
    let flow_digest = if flow_lines.is_empty() {
        "(none)".to_string()
    } else {
        flow_lines.join("\n")
    };

    (transcript, tool_spans, flow_digest)
}

/// Run the judge over pass_criteria (port of `judge_run`).
pub async fn judge_run(
    judge_cfg: Option<&JudgeConfig>,
    sim_api_key: &str,
    pass_criteria: &[String],
    turns: &[Map<String, Json>],
    tool_events: &[Map<String, Json>],
    flow_events: &[Map<String, Json>],
) -> Map<String, Json> {
    if pass_criteria.is_empty() {
        return JudgmentResult {
            verdict: "skipped".into(),
            notes: "No criteria.".into(),
            ..Default::default()
        }
        .to_dict();
    }
    let resolved = resolve_judge(judge_cfg, Some(sim_api_key));
    if !resolved.ready {
        return JudgmentResult {
            verdict: "skipped".into(),
            notes: if resolved.skip_reason.is_empty() {
                "Judge not ready (check judge.base_url/api_key or simulator key).".into()
            } else {
                resolved.skip_reason.clone()
            },
            ..Default::default()
        }
        .to_dict();
    }
    let Some(api_key) = resolved.api_key.clone() else {
        return JudgmentResult {
            verdict: "skipped".into(),
            notes: "Judge backend unavailable.".into(),
            ..Default::default()
        }
        .to_dict();
    };
    // Builtin preset expansion (port of runner._judge expand_criteria).
    let criteria = match crate::presets::expand_criteria(pass_criteria) {
        Ok(c) => c,
        Err(e) => {
            return JudgmentResult {
                verdict: "error".into(),
                notes: e,
                ..Default::default()
            }
            .to_dict()
        }
    };
    let (transcript, tool_spans, flow_digest) = build_evidence_packet(turns, tool_events, flow_events);
    let user = build_user_prompt(&criteria, &transcript, &tool_spans, Some(&flow_digest), false);
    // Backend dispatch (port of evals/backend.py): gemini mode → native
    // generateContent; endpoint_type anthropic → Messages wire; else OpenAI
    // chat completions.
    let text = if resolved.mode == "gemini" && resolved.base_url.is_none() {
        let backend = GeminiRestBackend {
            api_key,
            model: resolved.model.clone(),
            temperature: resolved.temperature,
            timeout_s: 180,
        };
        match backend.complete_json(JUDGE_SYSTEM, &user).await {
            Ok(t) => t,
            Err(e) => {
                return JudgmentResult {
                    verdict: "error".into(),
                    notes: e,
                    ..Default::default()
                }
                .to_dict();
            }
        }
    } else {
        let Some(base_url) = resolved.base_url.clone() else {
            return JudgmentResult {
                verdict: "skipped".into(),
                notes: "Judge backend unavailable.".into(),
                ..Default::default()
            }
            .to_dict();
        };
        if resolved.endpoint_type == "anthropic" {
            let backend = HttpAnthropicBackend {
                base_url,
                api_key,
                model: resolved.model.clone(),
                temperature: resolved.temperature,
                max_tokens: 2048,
                timeout_s: 180,
            };
            match backend.complete_json(JUDGE_SYSTEM, &user).await {
                Ok(t) => t,
                Err(e) => {
                    return JudgmentResult {
                        verdict: "error".into(),
                        notes: e,
                        ..Default::default()
                    }
                    .to_dict();
                }
            }
        } else {
            let backend = HttpOpenAIBackend {
                base_url,
                api_key,
                model: resolved.model.clone(),
                temperature: resolved.temperature,
                timeout_s: 180,
            };
            match backend.complete_json(JUDGE_SYSTEM, &user).await {
                Ok(t) => t,
                Err(e) => {
                    return JudgmentResult {
                        verdict: "error".into(),
                        notes: e,
                        ..Default::default()
                    }
                    .to_dict();
                }
            }
        }
    };
    // Parse + repair truncated JSON, then apply the relevancy gate.
    let parsed = parse_judgment_payload(&repair_json(&text));
    apply_relevancy(parsed).to_dict()
}

/// Multi-judge run (port of `runner.judge_run_multi`): evaluate each judge in
/// `pass_judges` (config judges list or per-judge overrides) then aggregate.
pub async fn judge_run_multi(
    judge_cfg: Option<&crate::config::JudgeConfig>,
    sim_api_key: &str,
    _pass_criteria: &[String], // global flat criteria — multi mode uses per-group criteria (Python parity)
    turns: &[Map<String, Json>],
    tool_events: &[Map<String, Json>],
    flow_events: &[Map<String, Json>],
    judges: &[Map<String, Json>],
    mode: &str,
) -> Map<String, Json> {
    if judges.is_empty() {
        return JudgmentResult {
            verdict: "skipped".into(),
            notes: "No judges.".into(),
            ..Default::default()
        }
        .to_dict();
    }
    // Per-judge overrides: judge_id, model, temperature, base_url, api_key.
    // Per-judge criteria from the group (builtin preset prepended + expanded —
    // port of runner.judge_run_multi expand_judge_group per judge).
    let mut results: Vec<Map<String, Json>> = Vec::new();
    for j in judges {
        let group = match crate::presets::expand_judge_group(j) {
            Ok(g) => g,
            Err(e) => {
                let mut r = JudgmentResult {
                    verdict: "error".into(),
                    notes: e,
                    ..Default::default()
                }
                .to_dict();
                r.insert(
                    "judge_id".into(),
                    serde_json::Value::String(
                        j.get("id")
                            .or_else(|| j.get("judge_id"))
                            .and_then(|v| v.as_str())
                            .unwrap_or("judge")
                            .to_string(),
                    ),
                );
                results.push(r);
                continue;
            }
        };
        let judge_id = group
            .get("id")
            .or_else(|| j.get("judge_id"))
            .and_then(|v| v.as_str())
            .unwrap_or("judge")
            .to_string();
        let group_criteria: Vec<String> = group
            .get("criteria")
            .and_then(|v| v.as_array())
            .map(|a| a.iter().map(|v| v.as_str().unwrap_or_default().to_string()).collect())
            .unwrap_or_default();
        let per = crate::config::JudgeConfig {
            model: j
                .get("model")
                .and_then(|v| v.as_str())
                .map(String::from)
                .or_else(|| judge_cfg.and_then(|c| c.model.clone())),
            temperature: j
                .get("temperature")
                .and_then(|v| v.as_f64())
                .or_else(|| judge_cfg.map(|c| c.temperature))
                .unwrap_or(0.0),
            base_url: j
                .get("base_url")
                .and_then(|v| v.as_str())
                .map(String::from)
                .or_else(|| judge_cfg.and_then(|c| c.base_url.clone())),
            api_key: j
                .get("api_key")
                .and_then(|v| v.as_str())
                .map(String::from)
                .or_else(|| judge_cfg.and_then(|c| c.api_key.clone())),
            endpoint_type: judge_cfg
                .map(|c| c.endpoint_type.clone())
                .unwrap_or_default(),
        };
        let mut v =
            judge_run(Some(&per), sim_api_key, &group_criteria, turns, tool_events, flow_events).await;
        v.insert("judge_id".into(), serde_json::Value::String(judge_id));
        results.push(v);
    }
    crate::evals::aggregate_judges(&results, mode)
}

/// Goals judge for `goals_met` asserts (port of `runner.judge_goals`):
/// verify the CALLER stated/pursued at least `min_goals` of `goals`.
/// Returns `{verdict, score, notes, ...}` (verdict pass|fail|maybe|skipped|error).
pub async fn judge_goals(
    judge_cfg: Option<&JudgeConfig>,
    sim_api_key: &str,
    goals: &[String],
    min_goals: i64,
    turns: &[Map<String, Json>],
    flow_events: &[Map<String, Json>],
) -> Map<String, Json> {
    if judge_cfg.is_none() {
        let mut r = JudgmentResult {
            verdict: "skipped".into(),
            notes: "goals_met skipped: no judge config.".into(),
            ..Default::default()
        }
        .to_dict();
        r.insert("score".into(), json!(0));
        return r;
    }
    let resolved = resolve_judge(judge_cfg, Some(sim_api_key));
    if !resolved.ready {
        let mut r = JudgmentResult {
            verdict: "skipped".into(),
            notes: resolved.skip_reason.clone(),
            ..Default::default()
        }
        .to_dict();
        r.insert("score".into(), json!(0));
        return r;
    }
    // Goal criteria text mirrors runner.judge_goals verbatim.
    let criteria = vec![format!(
        "The simulated caller stated or pursued the following goal(s) before the \
         call ended: {goals:?}. Verify at least {min_goals} of {n} goals were \
         explicitly mentioned or pursued.",
        goals = goals,
        min_goals = min_goals,
        n = goals.len(),
    )];
    let (transcript, tool_spans, flow_digest) = build_evidence_packet(turns, &[], flow_events);
    let user = build_user_prompt(&criteria, &transcript, &tool_spans, Some(&flow_digest), true);
    let Some(api_key) = resolved.api_key.clone() else {
        let mut r = JudgmentResult {
            verdict: "skipped".into(),
            notes: resolved.skip_reason.clone(),
            ..Default::default()
        }
        .to_dict();
        r.insert("score".into(), json!(0));
        return r;
    };
    let text = if resolved.mode == "gemini" && resolved.base_url.is_none() {
        let backend = GeminiRestBackend {
            api_key,
            model: resolved.model.clone(),
            temperature: resolved.temperature,
            timeout_s: 180,
        };
        match backend.complete_json(JUDGE_SYSTEM, &user).await {
            Ok(t) => t,
            Err(e) => {
                let mut r = JudgmentResult {
                    verdict: "error".into(),
                    notes: e,
                    ..Default::default()
                }
                .to_dict();
                r.insert("score".into(), json!(0));
                return r;
            }
        }
    } else {
        let Some(base_url) = resolved.base_url.clone() else {
            let mut r = JudgmentResult {
                verdict: "skipped".into(),
                notes: resolved.skip_reason.clone(),
                ..Default::default()
            }
            .to_dict();
            r.insert("score".into(), json!(0));
            return r;
        };
        let complete = |base_url: String| async {
            if resolved.endpoint_type == "anthropic" {
                let backend = HttpAnthropicBackend {
                    base_url,
                    api_key: api_key.clone(),
                    model: resolved.model.clone(),
                    temperature: resolved.temperature,
                    max_tokens: 2048,
                    timeout_s: 180,
                };
                backend.complete_json(JUDGE_SYSTEM, &user).await
            } else {
                let backend = HttpOpenAIBackend {
                    base_url,
                    api_key: api_key.clone(),
                    model: resolved.model.clone(),
                    temperature: resolved.temperature,
                    timeout_s: 180,
                };
                backend.complete_json(JUDGE_SYSTEM, &user).await
            }
        };
        match complete(base_url).await {
            Ok(t) => t,
            Err(e) => {
                let mut r = JudgmentResult {
                    verdict: "error".into(),
                    notes: e,
                    ..Default::default()
                }
                .to_dict();
                r.insert("score".into(), json!(0));
                return r;
            }
        }
    };
    let parsed = apply_relevancy(parse_judgment_payload(&repair_json(&text)));
    parsed.to_dict()
}
