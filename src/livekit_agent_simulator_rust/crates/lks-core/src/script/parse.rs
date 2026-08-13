//! Script parser — byte-parity port of `script_parse.py`.
//!
//! Parses `Script.spec.steps` → Vec<ScriptStep> and `Script.spec.verify` →
//! ScriptVerifySpec. Error message strings are load-bearing (tests + user-facing).

use serde_json::{Map, Value as Json};

use super::{
    normalize_interrupt_class, ScriptStep, ScriptVerifySpec, SUPPORTED_ACTIONS, SUPPORTED_TRIGGERS,
};

fn as_str(v: &Json) -> String {
    match v {
        Json::String(s) => s.clone(),
        Json::Number(n) => n.to_string(),
        Json::Bool(b) => b.to_string(),
        Json::Null => String::new(),
        other => other.to_string(),
    }
}

fn as_i64(v: &Json) -> Option<i64> {
    match v {
        Json::Number(n) => n.as_i64(),
        Json::String(s) => s.parse().ok(),
        _ => None,
    }
}

fn as_f64(v: &Json) -> Option<f64> {
    match v {
        Json::Number(n) => n.as_f64(),
        Json::String(s) => s.parse().ok(),
        _ => None,
    }
}

fn py_bool(v: &Json) -> bool {
    match v {
        Json::Null => false,
        Json::Bool(b) => *b,
        Json::Number(n) => {
            if let Some(i) = n.as_i64() {
                i != 0
            } else {
                n.as_f64().map(|f| f != 0.0).unwrap_or(true)
            }
        }
        Json::String(s) => !s.is_empty(),
        _ => true,
    }
}

/// `_parse_step_gain`: gain|volume alias, 0.0..=1.0.
fn parse_step_gain(
    raw: &Map<String, Json>,
    path_label: &str,
    step_id: &str,
) -> Result<f64, String> {
    let key = if raw.contains_key("gain") {
        "gain"
    } else if raw.contains_key("volume") {
        "volume"
    } else {
        return Ok(1.0);
    };
    let Some(gain) = as_f64(raw.get(key).unwrap()) else {
        return Err(format!(
            "{path_label}: Script step {step_id:?}: {key} must be a number"
        ));
    };
    if !(0.0..=1.0).contains(&gain) {
        return Err(format!(
            "{path_label}: Script step {step_id:?}: {key} must be between 0.0 and 1.0"
        ));
    }
    Ok(gain)
}

/// Parse Script.spec.verify → ScriptVerifySpec (None when absent).
pub fn parse_script_verify(raw: &Json) -> Result<Option<ScriptVerifySpec>, String> {
    if raw.is_null() {
        return Ok(None);
    }
    let Some(v) = raw.as_object() else {
        return Err("Script.spec.verify must be an object".into());
    };
    let mut plugins: Vec<String> = Vec::new();
    // plugins (array) or plugin (single) alias.
    let plugins_arr = match v.get("plugins") {
        Some(Json::Array(a)) => Some(a.clone()),
        Some(other) if !other.is_null() => {
            return Err("Script.spec.verify.plugins must be an array of plugin names".into())
        }
        _ => v.get("plugin").map(|p| vec![p.clone()]),
    };
    if let Some(arr) = plugins_arr {
        plugins = arr
            .iter()
            .map(as_str)
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty())
            .collect();
    }
    let mut plugin_options = Map::new();
    if let Some(opts) = v.get("plugin_options") {
        let Some(o) = opts.as_object() else {
            return Err("Script.spec.verify.plugin_options must be an object".into());
        };
        plugin_options = o.clone();
    }
    Ok(Some(ScriptVerifySpec {
        require_during_agent_speech: py_bool(
            v.get("require_during_agent_speech")
                .unwrap_or(&Json::Bool(true)),
        ),
        min_agent_finals_after_first_cue: v
            .get("min_agent_finals_after_first_cue")
            .and_then(as_i64)
            .unwrap_or(0),
        min_user_finals_after_first_cue: v
            .get("min_user_finals_after_first_cue")
            .and_then(as_i64)
            .unwrap_or(0),
        min_interruptions: v.get("min_interruptions").and_then(as_i64),
        max_interruptions: v.get("max_interruptions").and_then(as_i64),
        min_agent_finals_after_silence: v
            .get("min_agent_finals_after_silence")
            .and_then(as_i64)
            .unwrap_or(0),
        min_agent_finals_after_barge_in: v
            .get("min_agent_finals_after_barge_in")
            .and_then(as_i64)
            .unwrap_or(0),
        plugins,
        plugin_options,
    }))
}

/// Parse Script.spec.steps → Vec<ScriptStep>.
pub fn parse_script_steps(
    spec: &Map<String, Json>,
    path_label: &str,
) -> Result<Vec<ScriptStep>, String> {
    let Some(raw_steps) = spec.get("steps") else {
        return Ok(Vec::new());
    };
    let Some(arr) = raw_steps.as_array() else {
        return Err(format!("{path_label}: Script.spec.steps must be an array"));
    };
    let mut steps = Vec::new();
    for (i, raw) in arr.iter().enumerate() {
        let Some(raw_map) = raw.as_object() else {
            return Err(format!(
                "{path_label}: Script.spec.steps[{i}] must be an object"
            ));
        };
        let step_id = raw_map
            .get("id")
            .or_else(|| raw_map.get("label"))
            .map(as_str)
            .filter(|s| !s.is_empty())
            .unwrap_or_else(|| format!("step-{i}"));

        let mut trigger = as_str(
            raw_map
                .get("trigger")
                .unwrap_or(&Json::String("agent_speaking".into())),
        );
        if !SUPPORTED_TRIGGERS.contains(&trigger.as_str()) {
            return Err(format!(
                "{path_label}: Script step {step_id:?}: unsupported trigger {trigger:?} (supported: {:?})",
                SUPPORTED_TRIGGERS
            ));
        }
        let mut action = as_str(
            raw_map
                .get("action")
                .unwrap_or(&Json::String("speak".into())),
        );
        if !SUPPORTED_ACTIONS.contains(&action.as_str()) {
            return Err(format!(
                "{path_label}: Script step {step_id:?}: action must be speak|wait"
            ));
        }
        let say = raw_map
            .get("say")
            .or_else(|| raw_map.get("text"))
            .map(as_str)
            .unwrap_or_default();
        if action == "speak" && say.trim().is_empty() {
            return Err(format!(
                "{path_label}: Script step {step_id:?}: say/text required when action=speak"
            ));
        }
        let delivery = as_str(
            raw_map
                .get("delivery")
                .unwrap_or(&Json::String("gemini_text".into())),
        );
        if delivery != "gemini_text" && delivery != "room_pcm" {
            return Err(format!(
                "{path_label}: Script step {step_id:?}: delivery must be gemini_text or room_pcm"
            ));
        }
        let asset_raw = raw_map.get("asset").cloned();
        if action == "speak" && delivery == "room_pcm" && asset_raw.is_none() {
            return Err(format!(
                "{path_label}: Script step {step_id:?}: room_pcm delivery requires asset (WAV path)"
            ));
        }

        let mut delay_ms =
            as_i64(raw_map.get("delay_ms").unwrap_or(&Json::Number(800.into()))).unwrap_or(800);
        let mut min_agent = as_i64(
            raw_map
                .get("min_agent_active_ms")
                .unwrap_or(&Json::Number(400.into())),
        )
        .unwrap_or(400);
        let barge_in = py_bool(raw_map.get("barge_in").unwrap_or(&Json::Null))
            || py_bool(raw_map.get("interrupt").unwrap_or(&Json::Null));
        if barge_in {
            delay_ms =
                as_i64(raw_map.get("delay_ms").unwrap_or(&Json::Number(250.into()))).unwrap_or(250);
            min_agent = as_i64(
                raw_map
                    .get("min_agent_active_ms")
                    .unwrap_or(&Json::Number(200.into())),
            )
            .unwrap_or(200);
            trigger = "agent_speaking".to_string();
            action = "speak".to_string();
        }

        // DTMF
        let mut digits = String::new();
        let mut say_final = say.clone();
        if action == "dtmf" {
            let raw_digits = as_str(
                raw_map
                    .get("digits")
                    .or_else(|| raw_map.get("digit"))
                    .unwrap_or(&Json::String("".into())),
            )
            .trim()
            .to_string();
            for ch in raw_digits.chars() {
                if !"0123456789*#w".contains(ch) {
                    return Err(format!(
                        "{path_label}: Script step {step_id:?}: action=dtmf: digits can only contain 0-9*#w (got {ch:?})"
                    ));
                }
            }
            digits = raw_digits;
            say_final = format!("[DTMF: {digits}]");
        }
        if action == "dtmf" && trigger != "silence" && trigger != "time" {
            trigger = "time".to_string();
        }

        let with_blip = if raw_map.contains_key("with_blip") {
            py_bool(raw_map.get("with_blip").unwrap())
        } else {
            barge_in && delivery != "room_pcm"
        };
        let gain = parse_step_gain(raw_map, path_label, &step_id)?;
        let loop_bool = py_bool(raw_map.get("loop").unwrap_or(&Json::Null))
            || (raw_map.get("repeat").is_some_and(py_bool));
        if let Some(repeat) = raw_map.get("repeat") {
            if !repeat.is_boolean() {
                let s = as_str(repeat);
                if !["0", "1"].contains(&s.as_str()) {
                    return Err(format!(
                        "{path_label}: Script step {step_id:?}: use loop=true for continuous ambient beds (repeat count is not supported)"
                    ));
                }
            }
        }
        if loop_bool && delivery != "room_pcm" {
            return Err(format!(
                "{path_label}: Script step {step_id:?}: loop requires delivery=room_pcm"
            ));
        }
        if loop_bool && action != "speak" {
            return Err(format!(
                "{path_label}: Script step {step_id:?}: loop only applies to speak + room_pcm"
            ));
        }
        let asset_s = asset_raw
            .map(|v| as_str(&v).trim().to_string())
            .filter(|s| !s.is_empty());
        if loop_bool {
            if let Some(a) = &asset_s {
                let mut name = a.to_lowercase();
                if name.starts_with("builtin:") {
                    name = name["builtin:".len()..].to_string();
                }
                if name.starts_with('@') {
                    name = name[1..].to_string();
                }
                if name.starts_with("voice.") {
                    return Err(format!(
                        "{path_label}: Script step {step_id:?}: loop is for noise/ambient beds, not voice.* speech assets"
                    ));
                }
            }
        }

        let interrupt_class = normalize_interrupt_class(
            raw_map
                .get("class")
                .or_else(|| raw_map.get("interrupt_class")),
            barge_in,
            "correction",
        )
        .map_err(|e| format!("{path_label}: Script step {step_id:?}: {e}"))?;

        let overlay_raw = raw_map
            .get("overlay")
            .or_else(|| raw_map.get("speech_role"));
        let mut overlay: Option<String> = None;
        if let Some(o) = overlay_raw {
            let os = as_str(o).trim().to_string();
            if !os.is_empty() {
                let mut ov = os.to_lowercase().replace('-', "_");
                if matches!(ov.as_str(), "forced_line" | "forced" | "say") {
                    ov = "line".to_string();
                }
                if ov != "fixture" && ov != "line" {
                    return Err(format!(
                        "{path_label}: Script step {step_id:?}: overlay must be fixture|line (got {o:?})"
                    ));
                }
                overlay = Some(ov);
            }
        }

        steps.push(ScriptStep {
            id: step_id.clone(),
            trigger,
            delay_ms,
            say: say_final.trim().to_string(),
            digits,
            label: as_str(
                raw_map
                    .get("label")
                    .unwrap_or(&Json::String(step_id.clone())),
            ),
            once: py_bool(raw_map.get("once").unwrap_or(&Json::Bool(true))),
            min_agent_active_ms: min_agent,
            delivery,
            asset: asset_s,
            silence_after_cue_ms: raw_map
                .get("silence_after_cue_ms")
                .and_then(as_i64)
                .unwrap_or(0),
            action,
            mute_persona: raw_map
                .get("mute_persona")
                .map(py_bool)
                .filter(|_| raw_map.contains_key("mute_persona")),
            require_agent_spoke_first: py_bool(
                raw_map
                    .get("require_agent_spoke_first")
                    .unwrap_or(&Json::Bool(true)),
            ),
            require_agent_reply_this_turn: py_bool(
                raw_map
                    .get("require_agent_reply_this_turn")
                    .unwrap_or(&Json::Bool(true)),
            ),
            defer_on_open_question: py_bool(
                raw_map
                    .get("defer_on_open_question")
                    .unwrap_or(&Json::Bool(true)),
            ),
            open_question_idle_ms: raw_map
                .get("open_question_idle_ms")
                .and_then(as_i64)
                .unwrap_or(20_000),
            barge_in,
            with_blip,
            gain,
            interrupt_class,
            overlay,
            r#loop: loop_bool,
        });
    }
    Ok(steps)
}
