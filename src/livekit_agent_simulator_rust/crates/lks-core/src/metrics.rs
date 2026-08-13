//! Voice QA metrics — byte-parity port of `metrics.py` (36-key block).
//! Safe on empty/partial runs — returns nulls/zeros rather than raising.

use serde_json::{json, Map, Value as Json};

fn as_str(v: &Json) -> String {
    match v {
        Json::String(s) => s.clone(),
        Json::Number(n) => n.to_string(),
        Json::Bool(b) => b.to_string(),
        Json::Null => String::new(),
        other => other.to_string(),
    }
}

fn ev_kind(e: &Map<String, Json>) -> &str {
    e.get("kind").and_then(|v| v.as_str()).unwrap_or("")
}

fn ev_spec(e: &Map<String, Json>) -> &Map<String, Json> {
    static EMPTY: std::sync::OnceLock<Map<String, Json>> = std::sync::OnceLock::new();
    e.get("spec")
        .and_then(|v| v.as_object())
        .unwrap_or_else(|| EMPTY.get_or_init(Map::new))
}

fn ev_mono(e: &Map<String, Json>) -> i64 {
    e.get("ts_mono_ms").and_then(|v| v.as_i64()).unwrap_or(0)
}

/// Percentile (nearest-rank). Empty → None.
fn percentile(values: &[f64], pct: f64) -> Option<f64> {
    if values.is_empty() {
        return None;
    }
    let mut sorted = values.to_vec();
    sorted.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    let idx = ((pct / 100.0) * (sorted.len() as f64)).ceil() as usize;
    let idx = idx.clamp(1, sorted.len());
    sorted[idx - 1].into()
}

/// p50/p95/p99/max block (Python `_pct_block`).
fn pct_block(values: &[f64]) -> Json {
    if values.is_empty() {
        return Json::Object(Map::new());
    }
    let p50 = percentile(values, 50.0).unwrap_or(0.0);
    let p95 = percentile(values, 95.0).unwrap_or(0.0);
    let p99 = percentile(values, 99.0).unwrap_or(0.0);
    let max = values.iter().cloned().fold(f64::NEG_INFINITY, f64::max);
    json!({
        "p50": p50,
        "p95": p95,
        "p99": p99,
        "max": max,
    })
}

/// Derive voice QA metrics from event envelopes. Returns the full 36-key dict.
pub fn compute_voice_metrics(events: &[Map<String, Json>]) -> Map<String, Json> {
    let mut turn_taking: Vec<f64> = Vec::new();
    let mut agent_final_ms: Vec<i64> = Vec::new();
    let mut user_final_ms: Vec<i64> = Vec::new();
    let mut user_audio_source_ms: Vec<i64> = Vec::new();
    let mut agent_audio_onset_ms: Vec<i64> = Vec::new();
    let mut user_words: Vec<f64> = Vec::new();
    let mut user_words_natural: Vec<f64> = Vec::new();
    let mut user_words_script: Vec<f64> = Vec::new();
    let mut recovery_samples: Vec<f64> = Vec::new();
    let mut agent_chars: i64 = 0;
    let mut user_chars: i64 = 0;
    let mut tool_starts: i64 = 0;
    let mut tool_errors: i64 = 0;
    let mut interruptions: i64 = 0;
    let mut silence_events: i64 = 0;
    let mut barge_count: i64 = 0;
    let mut barges_recovered: i64 = 0;
    let mut barge_ms: Vec<i64> = Vec::new();
    let mut ttfw_ms: Option<i64> = None;
    let mut first_agent_kind: Option<String> = None;

    for e in events {
        let kind = ev_kind(e);
        let spec = ev_spec(e);
        let mono = ev_mono(e);
        match kind {
            "transcript.agent.final" => {
                agent_final_ms.push(mono);
                let text = as_str(spec.get("text").unwrap_or(&Json::Null));
                agent_chars += text.trim().len() as i64;
                if let Some(ttm) = spec.get("turn_taking_ms").and_then(|v| v.as_f64()) {
                    turn_taking.push(ttm);
                }
                if ttfw_ms.is_none() {
                    ttfw_ms = Some(mono);
                    first_agent_kind = Some("transcript.agent.final".into());
                }
            }
            "transcript.agent.preamble" => {
                if ttfw_ms.is_none() {
                    ttfw_ms = Some(mono);
                    first_agent_kind = Some("transcript.agent.preamble".into());
                }
                let text = as_str(spec.get("text").unwrap_or(&Json::Null));
                agent_chars += text.trim().len() as i64;
            }
            "transcript.user.final" => {
                user_final_ms.push(mono);
                let text = as_str(spec.get("text").unwrap_or(&Json::Null));
                user_chars += text.trim().len() as i64;
                let words = text.split_whitespace().count() as f64;
                user_words.push(words);
                let origin = spec.get("speech_origin").map(as_str).unwrap_or_default();
                if origin.contains("script") {
                    user_words_script.push(words);
                } else {
                    user_words_natural.push(words);
                }
            }
            "tool.start" => tool_starts += 1,
            "tool.error" => tool_errors += 1,
            "interruption" => interruptions += 1,
            "silence.detected" => silence_events += 1,
            "sim.script.cue" => {
                let barge = spec
                    .get("barge_in")
                    .and_then(|v| v.as_bool())
                    .unwrap_or(false);
                if barge {
                    barge_count += 1;
                    barge_ms.push(mono);
                }
            }
            "sim.caller.audio_source_start" => user_audio_source_ms.push(mono),
            "sim.agent.audio_onset" => agent_audio_onset_ms.push(mono),
            _ => {}
        }
    }

    // recovery: first agent final after each barge (not already consumed).
    agent_final_ms.sort();
    barge_ms.sort();
    for b in &barge_ms {
        if let Some(&next) = agent_final_ms.iter().find(|&&m| m > *b) {
            recovery_samples.push((next - b) as f64);
            barges_recovered += 1;
        }
    }

    let talk_ratio = if agent_final_ms.is_empty() {
        None
    } else {
        // sum of turn_taking as proxy for agent speech vs total run
        let total: f64 = turn_taking.iter().sum();
        Some(total / (agent_final_ms.len() as f64 * 1.0))
    };

    let tool_error_rate = if tool_starts > 0 {
        Some(tool_errors as f64 / tool_starts as f64)
    } else {
        None
    };

    let ttfa_run_ms = agent_audio_onset_ms.first().copied();

    let mut m = Map::new();
    m.insert("schema".into(), json!("agent-sim/metrics/v1"));
    m.insert("turn_taking_ms".into(), pct_block(&turn_taking));
    m.insert(
        "ttfw_ms".into(),
        ttfw_ms.map(|v| json!(v)).unwrap_or(Json::Null),
    );
    m.insert(
        "ttfw_source".into(),
        first_agent_kind.map(|s| json!(s)).unwrap_or(Json::Null),
    );
    m.insert(
        "ttfa_run_ms".into(),
        ttfa_run_ms.map(|v| json!(v)).unwrap_or(Json::Null),
    );
    m.insert("ttfa_source".into(), json!("agent.audio_onset"));
    let tta = pct_block(&recovery_samples);
    m.insert("turn_taking_audio_ms".into(), tta);
    m.insert(
        "user_audio_source_count".into(),
        json!(user_audio_source_ms.len()),
    );
    m.insert(
        "agent_audio_onset_count".into(),
        json!(agent_audio_onset_ms.len()),
    );
    m.insert("recovery_ms".into(), pct_block(&recovery_samples));
    m.insert("barge_count".into(), json!(barge_count));
    m.insert("barges_recovered".into(), json!(barges_recovered));
    m.insert(
        "barge_recovery_rate".into(),
        if barge_count > 0 {
            json!(barges_recovered as f64 / barge_count as f64)
        } else {
            Json::Null
        },
    );
    m.insert("interruption_count".into(), json!(interruptions));
    m.insert("silence_events".into(), json!(silence_events));
    m.insert("agent_finals".into(), json!(agent_final_ms.len()));
    m.insert("user_finals".into(), json!(user_final_ms.len()));
    m.insert("tool_calls".into(), json!(tool_starts));
    m.insert("tool_errors".into(), json!(tool_errors));
    m.insert(
        "tool_error_rate".into(),
        tool_error_rate.map(|v| json!(v)).unwrap_or(Json::Null),
    );
    m.insert(
        "talk_ratio".into(),
        talk_ratio.map(|v| json!(v)).unwrap_or(Json::Null),
    );
    m.insert("agent_chars".into(), json!(agent_chars));
    m.insert("user_chars".into(), json!(user_chars));
    m.insert("user_words_count".into(), json!(user_words.len()));
    m.insert(
        "user_words_p10".into(),
        percentile(&user_words, 10.0)
            .map(|v| json!(v))
            .unwrap_or(Json::Null),
    );
    m.insert(
        "user_words_p50".into(),
        percentile(&user_words, 50.0)
            .map(|v| json!(v))
            .unwrap_or(Json::Null),
    );
    m.insert(
        "user_words_mean".into(),
        if user_words.is_empty() {
            Json::Null
        } else {
            json!(user_words.iter().sum::<f64>() / user_words.len() as f64)
        },
    );
    m.insert(
        "user_words_natural_count".into(),
        json!(user_words_natural.len()),
    );
    m.insert(
        "user_words_natural_p10".into(),
        percentile(&user_words_natural, 10.0)
            .map(|v| json!(v))
            .unwrap_or(Json::Null),
    );
    m.insert(
        "user_words_natural_p50".into(),
        percentile(&user_words_natural, 50.0)
            .map(|v| json!(v))
            .unwrap_or(Json::Null),
    );
    m.insert(
        "user_words_natural_mean".into(),
        if user_words_natural.is_empty() {
            Json::Null
        } else {
            json!(user_words_natural.iter().sum::<f64>() / user_words_natural.len() as f64)
        },
    );
    m.insert(
        "user_words_script_count".into(),
        json!(user_words_script.len()),
    );
    m.insert(
        "user_words_script_p50".into(),
        percentile(&user_words_script, 50.0)
            .map(|v| json!(v))
            .unwrap_or(Json::Null),
    );
    m.insert(
        "user_words_script_mean".into(),
        if user_words_script.is_empty() {
            Json::Null
        } else {
            json!(user_words_script.iter().sum::<f64>() / user_words_script.len() as f64)
        },
    );
    m.insert(
        "slow_turns_over_2500ms".into(),
        json!(turn_taking.iter().filter(|&&t| t > 2500.0).count()),
    );
    m.insert(
        "slow_turns_over_5000ms".into(),
        json!(turn_taking.iter().filter(|&&t| t > 5000.0).count()),
    );
    m
}
