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
use crate::evals::{parse_judgment_payload, repair_json, JudgmentResult};

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
        api_key: None,
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
) -> (String, String) {
    // Transcript: Caller / {mm:ss.xx} - {mm:ss.xx} / text lines.
    let mut lines: Vec<String> = Vec::new();
    for t in turns {
        let user = t.get("user_text").and_then(|v| v.as_str()).unwrap_or("");
        let agent = t.get("agent_text").and_then(|v| v.as_str()).unwrap_or("");
        let tt = t
            .get("turn_taking_ms")
            .and_then(|v| v.as_i64())
            .unwrap_or(0);
        if !user.is_empty() {
            lines.push(format!("Caller: {user}"));
        }
        if !agent.is_empty() {
            let ts = format!(
                "[{:02}:{:02}.{:02}]",
                tt / 60000,
                (tt % 60000) / 1000,
                (tt % 1000) / 10
            );
            lines.push(format!("{ts} {agent}"));
        }
    }
    let transcript = if lines.is_empty() {
        "(empty)".to_string()
    } else {
        lines.join("\n")
    };

    // Tool spans: one JSON object per event.
    let spans: Vec<String> = tool_events
        .iter()
        .map(|e| {
            let kind = e.get("kind").and_then(|v| v.as_str()).unwrap_or("");
            let spec = e.get("spec").and_then(|v| v.as_object()).cloned().unwrap_or_default();
            let name = spec.get("name").and_then(|v| v.as_str()).unwrap_or("tool");
            let turn = e.get("turn").and_then(|v| v.as_i64()).unwrap_or(0);
            let err = spec.get("error").and_then(|v| v.as_str()).unwrap_or("");
            let dur = spec.get("duration_ms").and_then(|v| v.as_i64()).unwrap_or(0);
            format!(
                "{{\"kind\": \"{kind}\", \"turn\": {turn}, \"name\": \"{name}\", \"error\": \"{err}\", \"duration_ms\": {dur}}}"
            )
        })
        .collect();
    let tool_spans = if spans.is_empty() {
        "(none)".to_string()
    } else {
        spans.join("\n")
    };
    (transcript, tool_spans)
}

/// Run the judge over pass_criteria (port of `judge_run`).
pub async fn judge_run(
    judge_cfg: Option<&JudgeConfig>,
    sim_api_key: &str,
    pass_criteria: &[String],
    turns: &[Map<String, Json>],
    tool_events: &[Map<String, Json>],
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
    let Some(base_url) = resolved.base_url.clone() else {
        // Gemini mode not wired (needs the gemini REST call); skip loudly.
        return JudgmentResult {
            verdict: "skipped".into(),
            notes: "Gemini judge backend not wired in the Rust build (use judge.base_url + judge.api_key).".into(),
            ..Default::default()
        }
        .to_dict();
    };
    let backend = HttpOpenAIBackend {
        base_url,
        api_key,
        model: resolved.model.clone(),
        temperature: resolved.temperature,
        timeout_s: 180,
    };
    let (transcript, tool_spans) = build_evidence_packet(turns, tool_events);
    let user = build_user_prompt(pass_criteria, &transcript, &tool_spans, None, false);
    let text = match backend.complete_json(JUDGE_SYSTEM, &user).await {
        Ok(t) => t,
        Err(e) => {
            return JudgmentResult {
                verdict: "error".into(),
                notes: e,
                ..Default::default()
            }
            .to_dict();
        }
    };
    // Parse + repair truncated JSON.
    let parsed = parse_judgment_payload(&repair_json(&text));
    parsed.to_dict()
}
