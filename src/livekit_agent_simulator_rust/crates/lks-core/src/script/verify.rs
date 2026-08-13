//! Log-based script verify — byte-parity port of `script/verify.py` (core,
//! non-plugin path). Plugin checks land in P8 (pyo3 loader).

use serde_json::{json, Map, Value as Json};

use super::{counts_for_recovery_barge, ScriptStep, ScriptVerifySpec};

fn ev_kind(e: &Map<String, Json>) -> &str {
    e.get("kind").and_then(|v| v.as_str()).unwrap_or("")
}

/// The spec dict of an event (shared empty map when absent).
fn ev_spec(e: &Map<String, Json>) -> &Map<String, Json> {
    static EMPTY: std::sync::OnceLock<Map<String, Json>> = std::sync::OnceLock::new();
    e.get("spec")
        .and_then(|v| v.as_object())
        .unwrap_or_else(|| EMPTY.get_or_init(Map::new))
}

fn ev_mono(e: &Map<String, Json>) -> i64 {
    e.get("ts_mono_ms").and_then(|v| v.as_i64()).unwrap_or(0)
}

/// Evaluate a script log against steps + verify spec.
/// Returns the same dict shape as `evaluate_script_log`.
pub fn evaluate_script_log(
    events: &[Map<String, Json>],
    steps: &[ScriptStep],
    verify: Option<&ScriptVerifySpec>,
) -> Map<String, Json> {
    let cues: Vec<&Map<String, Json>> = events
        .iter()
        .filter(|e| {
            let k = ev_kind(e);
            k == "sim.script.cue" || k == "sim.script.wait" || k == "sim.script.hang_up"
        })
        .collect();
    let agent_finals: Vec<&Map<String, Json>> = events
        .iter()
        .filter(|e| ev_kind(e) == "transcript.agent.final")
        .collect();
    let user_finals: Vec<&Map<String, Json>> = events
        .iter()
        .filter(|e| ev_kind(e) == "transcript.user.final")
        .collect();
    let interruptions: Vec<&Map<String, Json>> = events
        .iter()
        .filter(|e| ev_kind(e) == "interruption")
        .collect();

    let mut checks: Vec<Json> = Vec::new();

    // Per-step checks.
    for step in steps {
        let matching: Vec<&&Map<String, Json>> = cues
            .iter()
            .filter(|c| {
                ev_spec(c).get("step_id").and_then(|v| v.as_str()) == Some(step.id.as_str())
            })
            .collect();
        if matching.is_empty() {
            checks.push(json!({
                "step_id": step.id,
                "pass": false,
                "reason": "script step not fired",
            }));
            continue;
        }
        let cue = matching[0];
        let spec = ev_spec(cue);
        if let Some(err) = spec.get("error") {
            checks.push(json!({
                "step_id": step.id,
                "pass": false,
                "reason": format!("cue fired with error: {err}"),
            }));
            continue;
        }
        let during = spec
            .get("during_agent_speech")
            .and_then(|v| v.as_bool())
            .unwrap_or(false);
        if step.trigger == "agent_speaking" && step.action == "speak" && !during {
            checks.push(json!({
                "step_id": step.id,
                "pass": false,
                "reason": "cue fired but agent was not active speaker",
            }));
            continue;
        }
        checks.push(json!({
            "step_id": step.id,
            "pass": true,
            "during_agent_speech": during,
            "trigger": step.trigger,
            "action": step.action,
        }));
    }

    let cue_ms = cues.first().map(|c| ev_mono(c));
    let silence_cues: Vec<&&Map<String, Json>> = cues
        .iter()
        .filter(|e| {
            let s = ev_spec(e);
            s.get("trigger").and_then(|v| v.as_str()) == Some("silence")
                || s.get("action").and_then(|v| v.as_str()) == Some("wait")
        })
        .collect();
    let silence_ms = silence_cues.first().map(|c| ev_mono(c));

    // Barge cues: counts_for_recovery_barge OR legacy short-during-agent heuristic.
    let mut barge_cues: Vec<&&Map<String, Json>> = Vec::new();
    for e in &cues {
        if ev_kind(e) != "sim.script.cue" {
            continue;
        }
        let spec = ev_spec(e);
        let cls = spec
            .get("class")
            .or_else(|| spec.get("interrupt_class"))
            .map(|v| v.as_str().unwrap_or(&v.to_string()).to_string());
        if counts_for_recovery_barge(
            spec.get("barge_in")
                .and_then(|v| v.as_bool())
                .unwrap_or(false),
            cls.as_deref(),
        ) {
            barge_cues.push(e);
            continue;
        }
        // Legacy heuristic: short during-agent cue without class → correction.
        if cls.is_none()
            && spec
                .get("during_agent_speech")
                .and_then(|v| v.as_bool())
                .unwrap_or(false)
            && spec.get("trigger").and_then(|v| v.as_str()) == Some("agent_speaking")
            && spec
                .get("waited_ms")
                .and_then(|v| v.as_i64())
                .unwrap_or(9999)
                < 800
        {
            barge_cues.push(e);
        }
    }
    let barge_ms = barge_cues.first().map(|c| ev_mono(c));

    let count_ge = |finals: &[&Map<String, Json>], ms: Option<i64>| -> i64 {
        match ms {
            Some(ms) => finals.iter().filter(|e| ev_mono(e) >= ms).count() as i64,
            None => 0,
        }
    };
    let count_gt = |finals: &[&Map<String, Json>], ms: Option<i64>| -> i64 {
        match ms {
            Some(ms) => finals.iter().filter(|e| ev_mono(e) > ms).count() as i64,
            None => 0,
        }
    };
    let agent_after_cue = count_ge(&agent_finals, cue_ms);
    let user_after_cue = count_ge(&user_finals, cue_ms);
    let agent_after_silence = count_ge(&agent_finals, silence_ms);
    let agent_after_barge = count_gt(&agent_finals, barge_ms);

    let verify = verify.cloned().unwrap_or(ScriptVerifySpec {
        require_during_agent_speech: true,
        min_agent_finals_after_first_cue: 0,
        min_user_finals_after_first_cue: 0,
        min_interruptions: None,
        max_interruptions: None,
        min_agent_finals_after_silence: 0,
        min_agent_finals_after_barge_in: 0,
        plugins: Vec::new(),
        plugin_options: Map::new(),
    });

    if verify.min_agent_finals_after_first_cue > 0 {
        checks.push(json!({
            "check": "min_agent_finals_after_first_cue",
            "pass": agent_after_cue >= verify.min_agent_finals_after_first_cue,
            "expected": verify.min_agent_finals_after_first_cue,
            "actual": agent_after_cue,
        }));
    }
    if verify.min_user_finals_after_first_cue > 0 {
        checks.push(json!({
            "check": "min_user_finals_after_first_cue",
            "pass": user_after_cue >= verify.min_user_finals_after_first_cue,
            "expected": verify.min_user_finals_after_first_cue,
            "actual": user_after_cue,
        }));
    }
    if verify.min_agent_finals_after_silence > 0 {
        checks.push(json!({
            "check": "min_agent_finals_after_silence",
            "pass": agent_after_silence >= verify.min_agent_finals_after_silence,
            "expected": verify.min_agent_finals_after_silence,
            "actual": agent_after_silence,
        }));
    }
    if verify.min_agent_finals_after_barge_in > 0 {
        checks.push(json!({
            "check": "min_agent_finals_after_barge_in",
            "pass": agent_after_barge >= verify.min_agent_finals_after_barge_in,
            "expected": verify.min_agent_finals_after_barge_in,
            "actual": agent_after_barge,
        }));
    }
    if let Some(min_i) = verify.min_interruptions {
        checks.push(json!({
            "check": "min_interruptions",
            "pass": (interruptions.len() as i64) >= min_i,
            "expected": min_i,
            "actual": interruptions.len(),
        }));
    }
    if let Some(max_i) = verify.max_interruptions {
        checks.push(json!({
            "check": "max_interruptions",
            "pass": (interruptions.len() as i64) <= max_i,
            "expected": max_i,
            "actual": interruptions.len(),
        }));
    }

    let cues_fired = cues
        .iter()
        .filter(|e| ev_kind(e) == "sim.script.cue")
        .count();
    let waits_fired = cues
        .iter()
        .filter(|e| ev_kind(e) == "sim.script.wait")
        .count();
    let hang_ups_fired = cues
        .iter()
        .filter(|e| ev_kind(e) == "sim.script.hang_up")
        .count();
    let all_pass = if checks.is_empty() {
        false
    } else {
        checks
            .iter()
            .all(|c| c.get("pass").and_then(|v| v.as_bool()).unwrap_or(false))
    };

    let mut out = Map::new();
    out.insert("script_steps".into(), json!(steps.len()));
    out.insert("cues_fired".into(), json!(cues_fired));
    out.insert("waits_fired".into(), json!(waits_fired));
    out.insert("hang_ups_fired".into(), json!(hang_ups_fired));
    out.insert(
        "agent_finals_after_first_cue".into(),
        json!(agent_after_cue),
    );
    out.insert("user_finals_after_first_cue".into(), json!(user_after_cue));
    out.insert(
        "agent_finals_after_silence".into(),
        json!(agent_after_silence),
    );
    out.insert(
        "agent_finals_after_barge_in".into(),
        json!(agent_after_barge),
    );
    out.insert("interruptions".into(), json!(interruptions.len()));
    out.insert("checks".into(), Json::Array(checks));
    out.insert("plugin_results".into(), Json::Array(Vec::new()));
    out.insert("pass".into(), json!(all_pass));
    out
}
