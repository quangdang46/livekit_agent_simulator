//! Script step / verify models — pure data (mirror `script/models.py`).
//!
//! The ScriptStep is the timed caller-cue model: trigger (agent_speaking |
//! silence | time), delay_ms, delivery (gemini_text | room_pcm), action
//! (speak | wait | hang_up | dtmf), barge semantics, interrupt class, overlay.

pub mod farewell;
pub mod hang_up_gate;
pub mod parse;
pub mod verify;

use serde_json::{Map, Value as Json};

pub const SUPPORTED_TRIGGERS: [&str; 3] = ["agent_speaking", "silence", "time"];
pub const SUPPORTED_ACTIONS: [&str; 4] = ["speak", "wait", "hang_up", "dtmf"];
pub const INTERRUPTION_CLASSES: [&str; 6] = [
    "correction",
    "backchannel",
    "noise",
    "dtmf",
    "silence",
    "escalate",
];
pub const RECOVERY_BARGE_CLASSES: [&str; 2] = ["correction", "escalate"];
pub const OVERLAY_ROLES: [&str; 2] = ["fixture", "line"];

/// `normalize_interrupt_class`: return a supported class or None.
/// barge_in=True without class defaults to "correction".
pub fn normalize_interrupt_class(
    raw: Option<&Json>,
    barge_in: bool,
    default_when_barge: &str,
) -> Result<Option<String>, String> {
    let raw = raw.and_then(|v| {
        if v.is_null() {
            None
        } else {
            let s = match v {
                Json::String(s) => s.clone(),
                other => other.to_string(),
            };
            if s.trim().is_empty() {
                None
            } else {
                Some(s)
            }
        }
    });
    let Some(raw) = raw else {
        return Ok(if barge_in {
            Some(default_when_barge.to_string())
        } else {
            None
        });
    };
    let mut key = raw.trim().to_lowercase().replace(['-', ' '], "_");
    let aliases: &[(&str, &str)] = &[
        ("true_correction", "correction"),
        ("correct", "correction"),
        ("barge", "correction"),
        ("ack", "backchannel"),
        ("uhhuh", "backchannel"),
        ("uh_huh", "backchannel"),
        ("false_positive", "noise"),
        ("false_interrupt", "noise"),
        ("click", "noise"),
        ("digit", "dtmf"),
        ("digits", "dtmf"),
        ("human", "escalate"),
        ("handoff", "escalate"),
        ("safety", "escalate"),
    ];
    for (alias, target) in aliases {
        if key == *alias {
            key = target.to_string();
            break;
        }
    }
    if !INTERRUPTION_CLASSES.contains(&key.as_str()) {
        return Err(format!(
            "unsupported interrupt class {raw:?} (supported: {:?})",
            INTERRUPTION_CLASSES
        ));
    }
    Ok(Some(key))
}

/// True when this cue drives recovery asserts / barge_recovery_rate.
pub fn counts_for_recovery_barge(barge_in: bool, interrupt_class: Option<&str>) -> bool {
    if !barge_in {
        return false;
    }
    let cls = interrupt_class.unwrap_or("correction");
    RECOVERY_BARGE_CLASSES.contains(&cls)
}

/// ScriptStep — frozen dataclass mirror.
#[derive(Debug, Clone, PartialEq)]
pub struct ScriptStep {
    pub id: String,
    pub trigger: String,
    pub delay_ms: i64,
    pub say: String,
    pub label: String,
    pub once: bool,
    pub min_agent_active_ms: i64,
    pub delivery: String,
    pub asset: Option<String>,
    pub silence_after_cue_ms: i64,
    pub action: String,
    pub mute_persona: Option<bool>,
    pub digits: String,
    /// `loop` is a Rust keyword — raw identifier.
    pub r#loop: bool,
    pub require_agent_spoke_first: bool,
    pub require_agent_reply_this_turn: bool,
    pub defer_on_open_question: bool,
    pub open_question_idle_ms: i64,
    pub barge_in: bool,
    pub with_blip: bool,
    pub gain: f64,
    pub interrupt_class: Option<String>,
    pub overlay: Option<String>,
}

/// `effective_overlay`: fixture vs line.
pub fn effective_overlay(step: &ScriptStep) -> &'static str {
    if let Some(o) = &step.overlay {
        // Explicit overlay (already validated to fixture|line at parse) wins.
        return if o == "line" { "line" } else { "fixture" };
    }
    let cls = step.interrupt_class.as_deref().unwrap_or("");
    if step.barge_in
        || step.delivery == "room_pcm"
        || matches!(cls, "noise" | "backchannel" | "dtmf" | "silence")
    {
        return "fixture";
    }
    if step.action == "speak" && !step.say.trim().is_empty() {
        return "line";
    }
    "fixture"
}

/// ScriptVerifySpec — mirror.
#[derive(Debug, Clone, PartialEq)]
pub struct ScriptVerifySpec {
    pub require_during_agent_speech: bool,
    pub min_agent_finals_after_first_cue: i64,
    pub min_user_finals_after_first_cue: i64,
    pub min_interruptions: Option<i64>,
    pub max_interruptions: Option<i64>,
    pub min_agent_finals_after_silence: i64,
    pub min_agent_finals_after_barge_in: i64,
    pub plugins: Vec<String>,
    pub plugin_options: Map<String, Json>,
}
