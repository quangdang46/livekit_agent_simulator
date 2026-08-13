//! Compile Hamming-style Behavior / speech_conditions into ScriptStep lists —
//! byte-parity port of `behavior_compile.py`.
//!
//! Explicit Script steps win by id; compiled steps fill gaps (append unknown ids).

use serde_json::{json, Map, Value as Json};

use crate::script::{
    counts_for_recovery_barge, normalize_interrupt_class, ScriptStep, ScriptVerifySpec,
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

/// True for package vocal speech refs (voice.*), not synthetic noise.*.
pub fn is_voice_asset(asset: Option<&str>) -> bool {
    let Some(asset) = asset else {
        return false;
    };
    let mut name = asset.trim().to_lowercase();
    if name.starts_with("builtin:") {
        name = name["builtin:".len()..].to_string();
    }
    if name.starts_with('@') {
        name = name[1..].to_string();
    }
    name.starts_with("voice.")
}

/// speech_conditions of a persona (speech_conditions | speechConditions).
pub fn speech_conditions_of(persona: &Map<String, Json>) -> &Map<String, Json> {
    static EMPTY: std::sync::OnceLock<Map<String, Json>> = std::sync::OnceLock::new();
    persona
        .get("speech_conditions")
        .or_else(|| persona.get("speechConditions"))
        .and_then(|v| v.as_object())
        .unwrap_or_else(|| EMPTY.get_or_init(Map::new))
}

/// True when Persona.speech_conditions.silent_mode is on.
pub fn silent_mode_enabled(persona: &Map<String, Json>) -> bool {
    let sc = speech_conditions_of(persona);
    let raw = sc
        .get("silent_mode")
        .or_else(|| sc.get("silentMode"))
        .or_else(|| sc.get("silent"));
    let Some(raw) = raw else { return false };
    if raw.as_bool() == Some(true) || as_i64(raw) == Some(1) {
        return true;
    }
    if let Some(s) = raw.as_str() {
        let t = s.trim().to_lowercase();
        if matches!(t.as_str(), "1" | "true" | "yes" | "on" | "silent") {
            return true;
        }
    }
    false
}

/// Derive default timed steps from Persona.speech_conditions.
pub fn compile_from_speech_conditions(
    persona: &Map<String, Json>,
) -> Result<Vec<ScriptStep>, String> {
    let sc = speech_conditions_of(persona);
    if sc.is_empty() {
        return Ok(Vec::new());
    }
    if silent_mode_enabled(persona) {
        return Ok(Vec::new());
    }
    let mut steps: Vec<ScriptStep> = Vec::new();

    let silent_mode = {
        let mut sil = false;
        for key in ["silent_mode", "silent", "dead_air"] {
            if let Some(v) = sc.get(key) {
                if v.as_bool() == Some(true)
                    || v.as_str()
                        .map(|s| {
                            matches!(
                                s.trim().to_lowercase().as_str(),
                                "1" | "true" | "yes" | "on"
                            )
                        })
                        .unwrap_or(false)
                {
                    sil = true;
                    break;
                }
            }
        }
        sil
    };

    // auto-ambient
    let noise = sc.get("noise").or_else(|| sc.get("ambient")).cloned();
    if let Some(noise_v) = noise {
        let noise_str = as_str(&noise_v);
        if !noise_str.is_empty() {
            let delay = as_i64(
                sc.get("noise_delay_ms")
                    .or_else(|| sc.get("after_join_ms"))
                    .unwrap_or(&json!(5000)),
            )
            .unwrap_or(5000);
            let noise_gain = as_f64(sc.get("noise_gain").unwrap_or(&json!(1.0))).unwrap_or(1.0);
            if !(0.0..=1.0).contains(&noise_gain) {
                return Err(
                    "Persona.speech_conditions.noise_gain must be between 0.0 and 1.0".into(),
                );
            }
            let when = as_str(
                sc.get("noise_when")
                    .or_else(|| sc.get("ambient_when"))
                    .unwrap_or(&Json::String("once".into())),
            )
            .trim()
            .to_lowercase();
            let mut loop_bool = matches!(
                when.as_str(),
                "background" | "loop" | "continuous" | "bed" | "always"
            );
            if let Some(nl) = sc.get("noise_loop") {
                loop_bool = py_bool(nl);
            } else if let Some(l) = sc.get("loop") {
                if l.is_boolean() {
                    loop_bool = py_bool(l);
                }
            }
            steps.push(ScriptStep {
                id: "auto-ambient".into(),
                trigger: "time".into(),
                delay_ms: delay.max(0),
                say: "[ambient]".into(),
                label: "auto-ambient".into(),
                once: true,
                min_agent_active_ms: 400,
                delivery: "room_pcm".into(),
                asset: Some(noise_str),
                silence_after_cue_ms: 0,
                action: "speak".into(),
                mute_persona: None,
                digits: "".into(),
                r#loop: loop_bool,
                require_agent_spoke_first: true,
                require_agent_reply_this_turn: true,
                defer_on_open_question: true,
                open_question_idle_ms: 20_000,
                barge_in: false,
                with_blip: false,
                gain: noise_gain,
                interrupt_class: Some("noise".into()),
                overlay: None,
            });
        }
    }

    // auto-barge
    let barge = as_str(
        sc.get("barge_policy")
            .or_else(|| sc.get("barge"))
            .unwrap_or(&Json::String("".into())),
    )
    .trim()
    .to_lowercase();
    if !silent_mode
        && matches!(
            barge.as_str(),
            "mid_agent_turn" | "mid" | "interrupt" | "barge" | "true" | "1"
        )
    {
        let after = as_i64(
            sc.get("barge_after_agent_ms")
                .or_else(|| sc.get("after_agent_ms"))
                .unwrap_or(&json!(600)),
        )
        .unwrap_or(600);
        let say = as_str(
            sc.get("barge_say")
                .unwrap_or(&Json::String("Sorry — one second —".into())),
        )
        .trim()
        .to_string();
        let asset_s = sc
            .get("barge_asset")
            .map(as_str)
            .unwrap_or_default()
            .trim()
            .to_string();
        let delivery = if asset_s.is_empty() {
            "gemini_text"
        } else {
            "room_pcm"
        };
        let default_blip = if asset_s.is_empty() {
            true
        } else {
            !is_voice_asset(Some(&asset_s))
        };
        let with_blip = if sc.contains_key("with_blip") {
            py_bool(sc.get("with_blip").unwrap())
        } else {
            default_blip
        };
        let barge_gain = as_f64(
            sc.get("barge_gain")
                .or_else(|| sc.get("gain"))
                .unwrap_or(&json!(1.0)),
        )
        .unwrap_or(1.0);
        if !(0.0..=1.0).contains(&barge_gain) {
            return Err("Persona.speech_conditions.barge_gain must be between 0.0 and 1.0".into());
        }
        let barge_class = normalize_interrupt_class(
            Some(
                sc.get("barge_class")
                    .or_else(|| sc.get("class"))
                    .unwrap_or(&Json::String("correction".into())),
            ),
            true,
            "correction",
        )?;
        steps.push(ScriptStep {
            id: "auto-barge-1".into(),
            trigger: "agent_speaking".into(),
            delay_ms: (after / 2).max(100),
            say: if delivery == "gemini_text" {
                say.clone()
            } else if say.is_empty() {
                "[barge]".into()
            } else {
                say
            },
            label: "auto-barge-1".into(),
            once: true,
            min_agent_active_ms: (after / 2).max(100),
            delivery: delivery.to_string(),
            asset: if asset_s.is_empty() {
                None
            } else {
                Some(asset_s)
            },
            silence_after_cue_ms: 0,
            action: "speak".into(),
            mute_persona: None,
            digits: "".into(),
            r#loop: false,
            require_agent_spoke_first: true,
            require_agent_reply_this_turn: true,
            defer_on_open_question: true,
            open_question_idle_ms: 20_000,
            barge_in: true,
            with_blip,
            gain: barge_gain,
            interrupt_class: barge_class,
            overlay: None,
        });
    }

    // auto-user-silence
    let mut silence_ms = as_i64(
        sc.get("silence_ms")
            .or_else(|| sc.get("user_silence_ms"))
            .unwrap_or(&json!(0)),
    )
    .unwrap_or(0);
    if silent_mode && silence_ms < 500 {
        silence_ms = as_i64(sc.get("silent_hold_ms").unwrap_or(&json!(12000))).unwrap_or(12000);
    }
    if silence_ms >= 500 {
        steps.push(ScriptStep {
            id: "auto-user-silence".into(),
            trigger: "time".into(),
            delay_ms: as_i64(sc.get("silence_arm_ms").unwrap_or(&json!(400))).unwrap_or(400),
            say: "".into(),
            label: "auto-user-silence".into(),
            once: true,
            min_agent_active_ms: 400,
            delivery: "gemini_text".into(),
            asset: None,
            silence_after_cue_ms: silence_ms,
            action: "wait".into(),
            mute_persona: None,
            digits: "".into(),
            r#loop: false,
            require_agent_spoke_first: true,
            require_agent_reply_this_turn: true,
            defer_on_open_question: true,
            open_question_idle_ms: 20_000,
            barge_in: false,
            with_blip: false,
            gain: 1.0,
            interrupt_class: None,
            overlay: None,
        });
    }

    Ok(steps)
}

/// Expand kind=Behavior.spec into ScriptStep list.
pub fn compile_from_behavior_spec(
    spec: &Map<String, Json>,
    path_label: &str,
) -> Result<Vec<ScriptStep>, String> {
    let mut steps: Vec<ScriptStep> = Vec::new();

    // ambient
    if let Some(ambient) = spec.get("ambient").and_then(|v| v.as_object()) {
        if let Some(asset_v) = ambient.get("asset") {
            let asset_s = as_str(asset_v).trim().to_string();
            if !asset_s.is_empty() {
                let delay = as_i64(ambient.get("delay_ms").unwrap_or(&json!(5000))).unwrap_or(5000);
                let amb_gain = as_f64(
                    ambient
                        .get("gain")
                        .or_else(|| ambient.get("volume"))
                        .unwrap_or(&json!(1.0)),
                )
                .unwrap_or(1.0);
                if !(0.0..=1.0).contains(&amb_gain) {
                    return Err(format!(
                        "{path_label}: ambient.gain must be between 0.0 and 1.0"
                    ));
                }
                let when = as_str(
                    ambient
                        .get("when")
                        .or_else(|| ambient.get("noise_when"))
                        .unwrap_or(&Json::String("".into())),
                )
                .trim()
                .to_lowercase();
                let loop_bool = py_bool(ambient.get("loop").unwrap_or(&Json::Bool(false)))
                    || matches!(
                        when.as_str(),
                        "background" | "loop" | "continuous" | "bed" | "always"
                    );
                steps.push(ScriptStep {
                    id: as_str(
                        ambient
                            .get("id")
                            .unwrap_or(&Json::String("behavior-ambient".into())),
                    ),
                    trigger: "time".into(),
                    delay_ms: delay.max(0),
                    say: as_str(
                        ambient
                            .get("say")
                            .unwrap_or(&Json::String("[ambient]".into())),
                    ),
                    label: as_str(
                        ambient
                            .get("label")
                            .unwrap_or(&Json::String("behavior-ambient".into())),
                    ),
                    once: py_bool(ambient.get("once").unwrap_or(&Json::Bool(true))),
                    min_agent_active_ms: 400,
                    delivery: "room_pcm".into(),
                    asset: Some(asset_s),
                    silence_after_cue_ms: 0,
                    action: "speak".into(),
                    mute_persona: None,
                    digits: "".into(),
                    r#loop: loop_bool,
                    require_agent_spoke_first: true,
                    require_agent_reply_this_turn: true,
                    defer_on_open_question: true,
                    open_question_idle_ms: 20_000,
                    barge_in: false,
                    with_blip: false,
                    gain: amb_gain,
                    interrupt_class: Some("noise".into()),
                    overlay: None,
                });
            }
        }
    }

    // barge_ins
    let barges = spec
        .get("barge_ins")
        .or_else(|| spec.get("barge_in"))
        .and_then(|v| v.as_array())
        .cloned()
        .unwrap_or_default();
    for (i, raw) in barges.iter().enumerate() {
        let Some(raw_map) = raw.as_object() else {
            return Err(format!("{path_label}: barge_ins[{i}] must be object"));
        };
        let sid = as_str(
            raw_map
                .get("id")
                .unwrap_or(&Json::String(format!("behavior-barge-{i}"))),
        );
        let after = as_i64(
            raw_map
                .get("after_agent_ms")
                .or_else(|| raw_map.get("delay_ms"))
                .unwrap_or(&json!(600)),
        )
        .unwrap_or(600);
        let say = as_str(
            raw_map
                .get("say")
                .or_else(|| raw_map.get("text"))
                .unwrap_or(&Json::String("Wait —".into())),
        )
        .trim()
        .to_string();
        let asset_s = raw_map
            .get("asset")
            .map(as_str)
            .unwrap_or_default()
            .trim()
            .to_string();
        let delivery = if raw_map.contains_key("delivery") {
            as_str(
                raw_map
                    .get("delivery")
                    .unwrap_or(&Json::String("gemini_text".into())),
            )
        } else if asset_s.is_empty() {
            "gemini_text".to_string()
        } else {
            "room_pcm".to_string()
        };
        if delivery == "room_pcm" && asset_s.is_empty() {
            return Err(format!("{path_label}: barge_ins[{i}] room_pcm needs asset"));
        }
        let with_blip = if raw_map.contains_key("with_blip") {
            py_bool(raw_map.get("with_blip").unwrap())
        } else if is_voice_asset(Some(&asset_s)) {
            false
        } else {
            delivery != "room_pcm"
        };
        let step_gain = as_f64(
            raw_map
                .get("gain")
                .or_else(|| raw_map.get("volume"))
                .unwrap_or(&json!(1.0)),
        )
        .unwrap_or(1.0);
        if !(0.0..=1.0).contains(&step_gain) {
            return Err(format!(
                "{path_label}: barge_ins[{i}] gain must be between 0.0 and 1.0"
            ));
        }
        let icls = normalize_interrupt_class(
            Some(
                raw_map
                    .get("class")
                    .or_else(|| raw_map.get("interrupt_class"))
                    .unwrap_or(&Json::String("correction".into())),
            ),
            true,
            "correction",
        )
        .map_err(|e| format!("{path_label}: barge_ins[{i}]: {e}"))?;
        let dl = as_i64(
            raw_map
                .get("delay_ms")
                .unwrap_or(&Json::Number((after / 2).max(150).into())),
        )
        .unwrap_or((after / 2).max(150));
        let ma = as_i64(
            raw_map
                .get("min_agent_active_ms")
                .unwrap_or(&Json::Number((after / 2).max(150).into())),
        )
        .unwrap_or((after / 2).max(150));
        steps.push(ScriptStep {
            id: sid.clone(),
            trigger: "agent_speaking".into(),
            delay_ms: dl.max(100),
            say,
            label: as_str(raw_map.get("label").unwrap_or(&Json::String(sid))),
            min_agent_active_ms: ma.max(100),
            delivery,
            asset: if asset_s.is_empty() {
                None
            } else {
                Some(asset_s)
            },
            silence_after_cue_ms: 0,
            action: "speak".into(),
            mute_persona: None,
            digits: "".into(),
            r#loop: false,
            require_agent_spoke_first: true,
            require_agent_reply_this_turn: true,
            defer_on_open_question: true,
            open_question_idle_ms: 20_000,
            barge_in: true,
            with_blip,
            once: py_bool(raw_map.get("once").unwrap_or(&Json::Bool(true))),
            gain: step_gain,
            interrupt_class: icls,
            overlay: None,
        });
    }

    // backchannels
    let bcs = spec
        .get("backchannels")
        .or_else(|| spec.get("backchannel"))
        .and_then(|v| v.as_array())
        .cloned()
        .unwrap_or_default();
    for (i, raw) in bcs.iter().enumerate() {
        let Some(raw_map) = raw.as_object() else {
            return Err(format!("{path_label}: backchannels[{i}] must be object"));
        };
        let sid = as_str(
            raw_map
                .get("id")
                .unwrap_or(&Json::String(format!("behavior-backchannel-{i}"))),
        );
        let after = as_i64(
            raw_map
                .get("after_agent_ms")
                .or_else(|| raw_map.get("delay_ms"))
                .unwrap_or(&json!(1200)),
        )
        .unwrap_or(1200);
        let say = as_str(
            raw_map
                .get("say")
                .or_else(|| raw_map.get("text"))
                .unwrap_or(&Json::String("uh-huh".into())),
        )
        .trim()
        .to_string();
        let asset_s = as_str(
            raw_map
                .get("asset")
                .unwrap_or(&Json::String("builtin:voice.backchannel".into())),
        )
        .trim()
        .to_string();
        let step_gain = as_f64(
            raw_map
                .get("gain")
                .or_else(|| raw_map.get("volume"))
                .unwrap_or(&json!(1.0)),
        )
        .unwrap_or(1.0);
        if !(0.0..=1.0).contains(&step_gain) {
            return Err(format!(
                "{path_label}: backchannels[{i}] gain must be between 0.0 and 1.0"
            ));
        }
        steps.push(ScriptStep {
            id: sid.clone(),
            trigger: "agent_speaking".into(),
            delay_ms: after.max(100),
            say,
            label: as_str(raw_map.get("label").unwrap_or(&Json::String(sid))),
            min_agent_active_ms: as_i64(
                raw_map
                    .get("min_agent_active_ms")
                    .unwrap_or(&Json::Number(after.into())),
            )
            .unwrap_or(after)
            .max(100),
            delivery: "room_pcm".into(),
            asset: Some(asset_s),
            silence_after_cue_ms: 0,
            action: "speak".into(),
            mute_persona: None,
            digits: "".into(),
            r#loop: false,
            require_agent_spoke_first: true,
            require_agent_reply_this_turn: true,
            defer_on_open_question: true,
            open_question_idle_ms: 20_000,
            barge_in: false,
            with_blip: false,
            once: py_bool(raw_map.get("once").unwrap_or(&Json::Bool(true))),
            gain: step_gain,
            interrupt_class: Some("backchannel".into()),
            overlay: None,
        });
    }

    // false_interrupts
    let fis = spec
        .get("false_interrupts")
        .or_else(|| spec.get("false_interrupt"))
        .and_then(|v| v.as_array())
        .cloned()
        .unwrap_or_default();
    for (i, raw) in fis.iter().enumerate() {
        let Some(raw_map) = raw.as_object() else {
            return Err(format!(
                "{path_label}: false_interrupts[{i}] must be object"
            ));
        };
        let sid = as_str(
            raw_map
                .get("id")
                .unwrap_or(&Json::String(format!("behavior-noise-{i}"))),
        );
        let after = as_i64(
            raw_map
                .get("after_agent_ms")
                .or_else(|| raw_map.get("delay_ms"))
                .unwrap_or(&json!(500)),
        )
        .unwrap_or(500);
        let say = as_str(
            raw_map
                .get("say")
                .or_else(|| raw_map.get("text"))
                .unwrap_or(&Json::String("[noise]".into())),
        )
        .trim()
        .to_string();
        let asset_s = as_str(
            raw_map
                .get("asset")
                .unwrap_or(&Json::String("builtin:noise.loud".into())),
        )
        .trim()
        .to_string();
        let step_gain = as_f64(
            raw_map
                .get("gain")
                .or_else(|| raw_map.get("volume"))
                .unwrap_or(&json!(1.0)),
        )
        .unwrap_or(1.0);
        if !(0.0..=1.0).contains(&step_gain) {
            return Err(format!(
                "{path_label}: false_interrupts[{i}] gain must be between 0.0 and 1.0"
            ));
        }
        steps.push(ScriptStep {
            id: sid.clone(),
            trigger: "agent_speaking".into(),
            delay_ms: after.max(100),
            say,
            label: as_str(raw_map.get("label").unwrap_or(&Json::String(sid))),
            min_agent_active_ms: as_i64(
                raw_map
                    .get("min_agent_active_ms")
                    .unwrap_or(&Json::Number(after.into())),
            )
            .unwrap_or(after)
            .max(100),
            delivery: "room_pcm".into(),
            asset: Some(asset_s),
            silence_after_cue_ms: 0,
            action: "speak".into(),
            mute_persona: None,
            digits: "".into(),
            r#loop: false,
            require_agent_spoke_first: true,
            require_agent_reply_this_turn: true,
            defer_on_open_question: true,
            open_question_idle_ms: 20_000,
            barge_in: true,
            with_blip: py_bool(raw_map.get("with_blip").unwrap_or(&Json::Bool(false))),
            once: py_bool(raw_map.get("once").unwrap_or(&Json::Bool(true))),
            gain: step_gain,
            interrupt_class: Some("noise".into()),
            overlay: None,
        });
    }

    // user_silence
    let uss = spec
        .get("user_silence")
        .or_else(|| spec.get("silences"))
        .and_then(|v| v.as_array())
        .cloned()
        .unwrap_or_default();
    for (i, raw) in uss.iter().enumerate() {
        let Some(raw_map) = raw.as_object() else {
            return Err(format!("{path_label}: user_silence[{i}] must be object"));
        };
        let sid = as_str(
            raw_map
                .get("id")
                .unwrap_or(&Json::String(format!("behavior-silence-{i}"))),
        );
        let hold = as_i64(
            raw_map
                .get("hold_ms")
                .or_else(|| raw_map.get("silence_after_cue_ms"))
                .unwrap_or(&json!(0)),
        )
        .unwrap_or(0);
        if hold < 500 {
            return Err(format!(
                "{path_label}: user_silence[{i}] hold_ms must be >= 500"
            ));
        }
        let arm = as_i64(
            raw_map
                .get("delay_ms")
                .or_else(|| raw_map.get("arm_ms"))
                .unwrap_or(&json!(400)),
        )
        .unwrap_or(400);
        steps.push(ScriptStep {
            id: sid.clone(),
            trigger: as_str(
                raw_map
                    .get("trigger")
                    .unwrap_or(&Json::String("time".into())),
            ),
            delay_ms: arm.max(0),
            say: "".into(),
            label: as_str(raw_map.get("label").unwrap_or(&Json::String(sid))),
            once: py_bool(raw_map.get("once").unwrap_or(&Json::Bool(true))),
            min_agent_active_ms: 400,
            delivery: "gemini_text".into(),
            asset: None,
            silence_after_cue_ms: hold,
            action: "wait".into(),
            mute_persona: None,
            digits: "".into(),
            r#loop: false,
            require_agent_spoke_first: py_bool(
                raw_map
                    .get("require_agent_spoke_first")
                    .unwrap_or(&Json::Bool(true)),
            ),
            require_agent_reply_this_turn: true,
            defer_on_open_question: true,
            open_question_idle_ms: 20_000,
            barge_in: false,
            with_blip: false,
            gain: 1.0,
            interrupt_class: None,
            overlay: None,
        });
    }

    Ok(steps)
}

/// Explicit Script wins on id collision; compiled steps append if id free.
pub fn merge_script_steps(explicit: &[ScriptStep], compiled: &[ScriptStep]) -> Vec<ScriptStep> {
    if compiled.is_empty() {
        return explicit.to_vec();
    }
    if explicit.is_empty() {
        return compiled.to_vec();
    }
    let mut seen: std::collections::HashSet<String> =
        explicit.iter().map(|s| s.id.clone()).collect();
    let mut out = explicit.to_vec();
    for s in compiled {
        if seen.contains(&s.id) {
            continue;
        }
        out.push(s.clone());
        seen.insert(s.id.clone());
    }
    out
}

/// If we auto-added barge/silence and no verify, soft defaults for recovery.
pub fn default_verify_for_compiled(
    steps: &[ScriptStep],
    existing: Option<&ScriptVerifySpec>,
) -> Option<ScriptVerifySpec> {
    if existing.is_some() {
        return existing.cloned();
    }
    let has_barge = steps
        .iter()
        .any(|s| counts_for_recovery_barge(s.barge_in, s.interrupt_class.as_deref()));
    let has_silence = steps
        .iter()
        .any(|s| s.action == "wait" && s.silence_after_cue_ms > 0);
    if !has_barge && !has_silence {
        return None;
    }
    Some(ScriptVerifySpec {
        require_during_agent_speech: false,
        min_agent_finals_after_barge_in: if has_barge { 1 } else { 0 },
        min_agent_finals_after_silence: 0,
        min_agent_finals_after_first_cue: 0,
        min_user_finals_after_first_cue: 0,
        min_interruptions: None,
        max_interruptions: None,
        plugins: Vec::new(),
        plugin_options: Map::new(),
    })
}

/// Compile persona speech_conditions + Behavior and merge with explicit Script.
pub fn apply_caller_behavior(
    persona: &Map<String, Json>,
    behavior_spec: Option<&Map<String, Json>>,
    explicit_steps: &[ScriptStep],
    explicit_verify: Option<&ScriptVerifySpec>,
    path_label: &str,
) -> Result<(Vec<ScriptStep>, Option<ScriptVerifySpec>), String> {
    // NOTE: interrupt_rate.parse_interrupt_rate validation is deferred to the
    // interrupt_rate module (P3) — it runs as a parallel runner, not compiled steps.
    let silent = silent_mode_enabled(persona);
    let mut compiled: Vec<ScriptStep> = Vec::new();
    compiled.extend(compile_from_speech_conditions(persona)?);
    if let Some(bs) = behavior_spec {
        if !silent {
            compiled.extend(compile_from_behavior_spec(
                bs,
                &format!("{path_label}:Behavior"),
            )?);
        } else {
            // Silent Mode: keep only wait/hang_up silences from Behavior.
            let raw_steps = compile_from_behavior_spec(bs, &format!("{path_label}:Behavior"))?;
            compiled.extend(
                raw_steps
                    .into_iter()
                    .filter(|s| matches!(s.action.as_str(), "wait" | "hang_up") && !s.barge_in),
            );
        }
    }
    let mut base_steps = explicit_steps.to_vec();
    if silent {
        // Drop explicit speak/barge fixtures; keep wait/hang_up (non-barge, non-room_pcm),
        // fallback to wait/hang_up only.
        let filtered: Vec<ScriptStep> = base_steps
            .iter()
            .filter(|s| {
                matches!(s.action.as_str(), "wait" | "hang_up")
                    && !s.barge_in
                    && s.delivery != "room_pcm"
            })
            .cloned()
            .collect();
        base_steps = if filtered.is_empty() {
            base_steps
                .iter()
                .filter(|s| matches!(s.action.as_str(), "wait" | "hang_up"))
                .cloned()
                .collect()
        } else {
            filtered
        };
        // Clear say/asset on hang_up steps (no goodbye speech).
        let mut cleaned: Vec<ScriptStep> = Vec::new();
        for s in &base_steps {
            if s.action == "hang_up" && (!s.say.is_empty() || s.asset.is_some()) {
                let mut c = s.clone();
                c.say = "".into();
                c.delivery = "gemini_text".into();
                c.asset = None;
                c.barge_in = false;
                c.with_blip = false;
                c.gain = 1.0;
                c.r#loop = false;
                cleaned.push(c);
            } else {
                cleaned.push(s.clone());
            }
        }
        base_steps = cleaned;
    }
    let steps = merge_script_steps(&base_steps, &compiled);
    let verify = default_verify_for_compiled(&steps, explicit_verify);
    Ok((steps, verify))
}
