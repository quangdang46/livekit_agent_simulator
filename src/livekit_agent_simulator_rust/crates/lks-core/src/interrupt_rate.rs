//! Interruption-rate policy (port of `interrupt_rate.py`) — Coval-style
//! recurring caller cut-ins while the agent is the active speaker.

use serde_json::{Map, Value as Json};

pub const INTERRUPT_RATE_INTERVALS_MS: [(&str, Option<i64>); 4] = [
    ("none", None),
    ("low", Some(90_000)),
    ("medium", Some(45_000)),
    ("high", Some(30_000)),
];
pub const MIN_INTERVAL_MS: i64 = 1_000;
pub const DEFAULT_SAY: &str = "Sorry — one second —";
pub const DEFAULT_MIN_AGENT_ACTIVE_MS: i64 = 700;

#[derive(Debug, Clone, PartialEq)]
pub struct InterruptRateSpec {
    pub rate: String,
    pub interval_ms: i64,
    pub say: String,
    pub asset: Option<String>,
    pub delivery: String,
    pub interrupt_class: String,
    pub gain: f64,
    pub with_blip: bool,
    pub min_agent_active_ms: i64,
}

fn sc_of(persona: &Map<String, Json>) -> Map<String, Json> {
    persona
        .get("speech_conditions")
        .or_else(|| persona.get("speechConditions"))
        .and_then(|v| v.as_object())
        .cloned()
        .unwrap_or_default()
}

fn silent_mode(persona: &Map<String, Json>) -> bool {
    let sc = sc_of(persona);
    let raw = sc
        .get("silent_mode")
        .or_else(|| sc.get("silentMode"))
        .or_else(|| sc.get("silent"));
    match raw {
        Some(Json::Bool(true)) => true,
        Some(Json::Number(n)) => n.as_i64() == Some(1),
        Some(Json::String(s)) => matches!(
            s.trim().to_lowercase().as_str(),
            "1" | "true" | "yes" | "on" | "silent"
        ),
        _ => false,
    }
}

fn is_voice_asset(asset: &str) -> bool {
    asset.starts_with("voice.") || asset.contains("speech")
}

/// Parse + validate interruption_rate (None when off; ValueError on bad values).
pub fn parse_interrupt_rate(
    persona: &Map<String, Json>,
) -> Result<Option<InterruptRateSpec>, String> {
    let sc = sc_of(persona);
    let raw = sc
        .get("interruption_rate")
        .or_else(|| sc.get("interrupt_rate"));
    let Some(raw) = raw else { return Ok(None) };
    let rate = raw.as_str().unwrap_or("").trim().to_lowercase();
    if matches!(rate.as_str(), "" | "none" | "off" | "0" | "false") {
        return Ok(None);
    }
    let interval = INTERRUPT_RATE_INTERVALS_MS
        .iter()
        .find(|(r, _)| *r == rate)
        .map(|(_, v)| *v)
        .ok_or_else(|| {
            format!(
                "Persona.speech_conditions.interruption_rate must be one of none|low|medium|high (got {rate:?})"
            )
        })?;
    let mut interval_ms = interval;
    if let Some(ov) = sc.get("interruption_interval_ms") {
        interval_ms = Some(ov.as_i64().unwrap_or(0));
    }
    let Some(interval_ms) = interval_ms else {
        return Ok(None);
    };
    if interval_ms < MIN_INTERVAL_MS {
        return Err(format!(
            "Persona.speech_conditions.interruption_interval_ms must be >= {MIN_INTERVAL_MS} (got {interval_ms:?})"
        ));
    }
    let gain = sc
        .get("interruption_gain")
        .and_then(|v| v.as_f64())
        .unwrap_or(1.0);
    if !(0.0..=1.0).contains(&gain) {
        return Err(
            "Persona.speech_conditions.interruption_gain must be between 0.0 and 1.0".into(),
        );
    }
    let icls = sc
        .get("interruption_class")
        .and_then(|v| v.as_str())
        .unwrap_or("correction")
        .to_string();
    let icls =
        crate::script::normalize_interrupt_class(Some(&Json::String(icls)), true, "correction")?
            .unwrap_or_else(|| "correction".to_string());
    // Silent caller never speaks — policy off (validation still ran).
    if silent_mode(persona) {
        return Ok(None);
    }
    let asset = sc
        .get("interruption_asset")
        .and_then(|v| v.as_str())
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty());
    let delivery = if asset.is_some() {
        "room_pcm"
    } else {
        "gemini_text"
    }
    .to_string();
    let default_blip = match &asset {
        Some(a) => !is_voice_asset(a),
        None => true,
    };
    let with_blip = sc
        .get("interruption_with_blip")
        .and_then(|v| v.as_bool())
        .unwrap_or(default_blip);
    let say = sc
        .get("interruption_say")
        .and_then(|v| v.as_str())
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| DEFAULT_SAY.to_string());
    let min_active = sc
        .get("interruption_min_agent_active_ms")
        .and_then(|v| v.as_i64())
        .unwrap_or(DEFAULT_MIN_AGENT_ACTIVE_MS)
        .max(100);
    Ok(Some(InterruptRateSpec {
        rate,
        interval_ms,
        say,
        asset,
        delivery,
        interrupt_class: icls,
        gain,
        with_blip,
        min_agent_active_ms: min_active,
    }))
}
