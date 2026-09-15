//! `do:` behavior generation — Rust port of the caller_contract text-backend
//! slice (`caller_contract/text_backends.py::OpenAITextBackend` +
//! `language_adapter.py::build_context`/`_parse_backend_response`).
//!
//! Scope: the stateless HTTP round trip + context/response shaping only.
//! Validation reuses `crate::caller_contract` (ContractValidator,
//! BehaviorContract, CandidateUtterance) — this module never re-implements
//! the Tier-1 rule lexicon. Orchestration (turn loop, satisfaction wait,
//! publish) lives in lks-livekit, which has the caller bridge + cue channel
//! this crate deliberately does not depend on.

use serde_json::{json, Map, Value as Json};

use crate::caller_contract::{BehaviorContract, CandidateUtterance, ContractConstraints, GenerationIdentity};
use crate::caller_dsl::DEFAULT_BEHAVIOR_CATALOG;

pub const DEFAULT_MODEL: &str = "gpt-4o-mini";
pub const DEFAULT_TEMPERATURE: f64 = 0.4;
pub const DEFAULT_TIMEOUT_S: u64 = 20;
/// Port of `driver.py::self.max_retries` default (2) applied to the `do:`
/// generate/validate loop — 3 total attempts (max_retries + 1).
pub const DEFAULT_MAX_RETRIES: u32 = 2;
/// Port of `language_adapter.py::DEFAULT_RECENT_TURNS_CAP`.
pub const DEFAULT_RECENT_TURNS_CAP: usize = 6;

/// Port of `text_backends.py::_SYSTEM_PROMPT`, verbatim (including the
/// Run-025 target-lexical-presence paragraph — dropping the topic word is
/// not a valid phrasing of the behavior even when the utterance is natural).
pub const SYSTEM_PROMPT: &str = concat!(
    "You are generating ONE line of dialogue for a simulated phone caller. ",
    "You do not decide what to do next, when to speak, or when to end the ",
    "call — you only phrase the CURRENT behavior naturally in first person. ",
    "Respond with a single JSON object, no prose, no markdown fences: ",
    "{\"act\": \"<same as current_behavior.act>\", \"target\": <same target or null>, ",
    "\"slots\": {}, \"utterance\": \"<one natural sentence>\"}. ",
    "The utterance must stay strictly on-topic for current_behavior and must ",
    "never mention anything in forbidden context, never ask about unrelated ",
    "topics, and never say goodbye/end the call unless current_behavior.act ",
    "itself is an end/hangup behavior. ",
    "When current_behavior.target is set, the utterance MUST name that topic ",
    "in words — do not paraphrase the topic away.",
);

/// One caller/agent line of prior turn history (mirrors `language_adapter.Turn`).
#[derive(Debug, Clone)]
pub struct DoTurn {
    pub speaker: String,
    pub text: String,
}

/// Parse a `do:` CallerAction payload's `constraints` map (mirrors
/// `dsl.py::_parse_constraints` field set, already validated at parse time
/// by `caller_dsl::parse_step` — this just lifts it into typed form for the
/// driver, applying the same defaults `BehaviorContract` uses).
pub fn parse_do_constraints(raw: Option<&Map<String, Json>>) -> ContractConstraints {
    let mut c = ContractConstraints {
        max_turns: 3,
        max_budget: None,
        max_words: None,
        max_duration_s: None,
        forbidden_intents: Vec::new(),
        must_not: Vec::new(),
    };
    let Some(raw) = raw else { return c };
    if let Some(mt) = raw.get("max_turns").and_then(|v| v.as_i64()) {
        c.max_turns = mt;
    }
    if let Some(mb) = raw.get("max_budget").and_then(|v| v.as_f64()) {
        c.max_budget = Some(mb);
    }
    if let Some(mw) = raw.get("max_words").and_then(|v| v.as_i64()) {
        c.max_words = Some(mw);
    }
    if let Some(md) = raw.get("max_duration_s").and_then(|v| v.as_f64()) {
        c.max_duration_s = Some(md);
    }
    c
}

/// Build the `BehaviorContract` a `do:` CallerAction represents (mirrors
/// how driver.py constructs BehaviorContract(behavior, target, constraints)
/// from the parsed CallerAction).
pub fn contract_from_do_payload(behavior: &str, target: Option<&str>, raw: &Map<String, Json>) -> BehaviorContract {
    BehaviorContract {
        behavior: behavior.to_string(),
        target: target.map(|s| s.to_string()),
        constraints: parse_do_constraints(raw.get("constraints").and_then(|v| v.as_object())),
    }
}

/// True iff `behavior` is one of the 11 catalog values (same source of
/// truth as `caller_dsl::DEFAULT_BEHAVIOR_CATALOG` — parse already rejected
/// unknown behaviors, this is a defensive re-check for direct callers).
pub fn is_known_behavior(behavior: &str) -> bool {
    DEFAULT_BEHAVIOR_CATALOG.contains(&behavior)
}

/// Minimal structured ConversationContext (mirrors
/// `language_adapter.py::build_context` — never a full transcript dump;
/// `recent_turns` is capped at `recent_turns_cap`).
pub fn build_context(
    contract: &BehaviorContract,
    turn: i64,
    agent_latest: Option<&str>,
    relevant_facts: &[String],
    recent_turns: &[DoTurn],
    recent_turns_cap: usize,
) -> Json {
    let capped: Vec<&DoTurn> = if recent_turns.len() > recent_turns_cap {
        recent_turns[recent_turns.len() - recent_turns_cap..].iter().collect()
    } else {
        recent_turns.iter().collect()
    };
    json!({
        "current_behavior": {
            "act": contract.behavior,
            "target": contract.target,
            "max_budget": contract.constraints.max_budget,
            "turn": turn,
            "max_turns": contract.constraints.max_turns,
        },
        "agent_latest": agent_latest.map(|t| json!({"text": t})),
        "relevant_facts": relevant_facts,
        "recent_turns": capped.iter().map(|t| json!({"speaker": t.speaker, "text": t.text})).collect::<Vec<_>>(),
    })
}

/// Strip a stray ```json ... ``` fence some models add despite instructions
/// (mirrors `text_backends.py::_parse_json_object`).
fn strip_fence(raw: &str) -> &str {
    let text = raw.trim();
    if let Some(rest) = text.strip_prefix("```") {
        let rest = rest.strip_prefix("json").unwrap_or(rest);
        return rest.trim_end_matches("```").trim();
    }
    text
}

/// Parse the backend's raw completion text into a `CandidateUtterance`
/// (mirrors `text_backends.py::_parse_json_object` +
/// `language_adapter.py::_parse_backend_response`). Required keys: act,
/// utterance (a missing/empty required key is a backend error, never a
/// silently-defaulted candidate).
pub fn parse_backend_response(raw_text: &str, identity: GenerationIdentity) -> Result<CandidateUtterance, String> {
    let stripped = strip_fence(raw_text);
    let parsed: Json = serde_json::from_str(stripped)
        .map_err(|e| format!("backend did not return valid JSON: {e}"))?;
    let Json::Object(obj) = parsed else {
        return Err("backend response must be a JSON object".to_string());
    };
    let act = obj.get("act").and_then(|v| v.as_str()).unwrap_or("");
    let utterance = obj.get("utterance").and_then(|v| v.as_str()).unwrap_or("");
    let mut missing = Vec::new();
    if act.trim().is_empty() {
        missing.push("act");
    }
    if utterance.trim().is_empty() {
        missing.push("utterance");
    }
    if !missing.is_empty() {
        return Err(format!("backend response missing required field(s): {missing:?}"));
    }
    let slots = match obj.get("slots") {
        Some(Json::Object(m)) => m.clone().into_iter().collect(),
        Some(_) => return Err("backend response 'slots' must be a dict".to_string()),
        None => std::collections::HashMap::new(),
    };
    Ok(CandidateUtterance {
        act: act.to_string(),
        target: obj.get("target").and_then(|v| v.as_str()).map(|s| s.to_string()),
        slots,
        utterance: utterance.to_string(),
        identity,
    })
}

/// One stateless OpenAI chat-completions round trip (mirrors
/// `text_backends.py::OpenAITextBackend.generate` — text-only `gpt-4o-mini`
/// class model, NOT the Realtime API).
pub async fn generate_do_candidate(
    api_key: &str,
    base_url: &str,
    model: &str,
    temperature: f64,
    timeout_s: u64,
    context: &Json,
    identity: GenerationIdentity,
) -> Result<CandidateUtterance, String> {
    let endpoint = if base_url.ends_with("/chat/completions") {
        base_url.to_string()
    } else {
        format!("{}/chat/completions", base_url.trim_end_matches('/'))
    };
    let body = json!({
        "model": model,
        "temperature": temperature,
        "stream": false,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": serde_json::to_string(context).unwrap_or_default()},
        ],
        "response_format": {"type": "json_object"},
    });
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(timeout_s))
        .build()
        .map_err(|e| format!("reqwest build: {e}"))?;
    let resp = client
        .post(&endpoint)
        .bearer_auth(api_key)
        .header("Content-Type", "application/json")
        .header("Accept", "application/json")
        .body(serde_json::to_string(&body).unwrap_or_default())
        .send()
        .await
        .map_err(|e| format!("do: backend unreachable: {e}"))?;
    let status = resp.status();
    let text = resp.text().await.unwrap_or_default();
    if !status.is_success() {
        return Err(format!("do: backend HTTP {status}: {}", text.chars().take(500).collect::<String>()));
    }
    let envelope: Json = serde_json::from_str(&text).map_err(|e| format!("do: backend response not JSON: {e}"))?;
    let content = envelope
        .get("choices")
        .and_then(|c| c.get(0))
        .and_then(|c| c.get("message"))
        .and_then(|m| m.get("content"))
        .and_then(|v| v.as_str())
        .ok_or_else(|| "do: backend response missing choices[0].message.content".to_string())?;
    parse_backend_response(content, identity)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_backend_response_strips_fence_and_requires_act_utterance() {
        let identity = GenerationIdentity {
            behavior_id: "b1".into(),
            turn_id: 0,
            generation_id: 0,
            context_version: 0,
        };
        let raw = "```json\n{\"act\": \"ask\", \"utterance\": \"What's the price?\"}\n```";
        let candidate = parse_backend_response(raw, identity.clone()).expect("parses");
        assert_eq!(candidate.act, "ask");
        assert_eq!(candidate.utterance, "What's the price?");

        let missing = parse_backend_response("{\"act\": \"ask\"}", identity);
        assert!(missing.is_err());
    }

    #[test]
    fn parse_do_constraints_defaults_max_turns_3() {
        let c = parse_do_constraints(None);
        assert_eq!(c.max_turns, 3);
        assert!(c.max_words.is_none());
    }

    #[test]
    fn build_context_caps_recent_turns() {
        let contract = BehaviorContract {
            behavior: "ask".into(),
            target: Some("price".into()),
            constraints: ContractConstraints {
                max_turns: 3,
                max_budget: None,
                max_words: None,
                max_duration_s: None,
                forbidden_intents: vec![],
                must_not: vec![],
            },
        };
        let turns: Vec<DoTurn> = (0..10)
            .map(|i| DoTurn { speaker: "caller".into(), text: format!("turn {i}") })
            .collect();
        let ctx = build_context(&contract, 2, Some("hello"), &[], &turns, 6);
        let recent = ctx.get("recent_turns").and_then(|v| v.as_array()).unwrap();
        assert_eq!(recent.len(), 6);
        assert_eq!(recent[0]["text"], "turn 4");
    }
}
