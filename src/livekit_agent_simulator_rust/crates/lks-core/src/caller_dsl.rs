//! CallerSteps DSL — Rust side of the Python/Rust parity suite.
//!
//! Mirrors `src/livekit_agent_simulator/caller_contract/dsl.py` (Python, the
//! reference implementation): parses scenario caller steps into CallerAction
//! values with the SAME strictness (unknown keys are hard errors naming
//! file/line/field, never silent ignores).
//!
//! Scope: parse + validate only. No driver/orchestrator/TTS/LiveKit here —
//! those live behind the Python runtime; Rust owns authoring parity (parse
//! the same YAML, reject the same bad specs, preserve fields on export).
//!
//! Field names, defaults, and error shapes MUST match the Python side
//! exactly — see `#[cfg(test)] mod parity_tests` below, which reads the
//! SAME JSON vectors under `tests/fixtures/parity/` that the Python
//! `test_dsl_parity_vectors.py` reads.

use serde_json::{Map, Value as Json};

use crate::errors::ScenarioError;

// ---------------------------------------------------------------------------
// Vocabularies (must match dsl.py exactly)
// ---------------------------------------------------------------------------

pub const DEFAULT_BEHAVIOR_CATALOG: [&str; 11] = [
    "ask",
    "confirm",
    "deny",
    "accept",
    "reject",
    "negotiate",
    "clarify",
    "provide",
    "arrange_visit",
    "interrupt",
    "end",
];

pub const TRIGGER_KINDS: [&str; 3] = ["time", "agent_speaking", "silence"];

pub const INTERRUPT_CLASSES: [&str; 2] = ["correction", "backchannel"];

pub const INTERRUPTION_RATES: [&str; 3] = ["low", "medium", "high"];

pub const ACTION_KINDS: [&str; 9] = [
    "say",
    "do",
    "wait",
    "dtmf",
    "interrupt",
    "play_audio",
    "end",
    "silence",
    "hangup",
];

fn err(file: &str, line: i64, msg: &str, field: Option<&str>) -> ScenarioError {
    let located = format!("{}:{}", file, line);
    ScenarioError(format!(
        "{located}: {msg}{}",
        field.map(|f| format!(" (field={f})")).unwrap_or_default()
    ))
}

// ---------------------------------------------------------------------------
// Parsed values (raw-JSON preserving: round-trip faithful)
// ---------------------------------------------------------------------------

/// One parsed scenario step. Payloads stay as raw JSON maps so export
/// preserves every field byte-faithfully (no silent drops — the parity
/// gap the Python side closed with strict sibling validation).
#[derive(Debug, Clone, PartialEq)]
pub struct CallerAction {
    pub kind: String,
    pub line_no: i64,
    pub payload: Map<String, Json>,
}

impl CallerAction {
    pub fn get(&self, key: &str) -> Option<&Json> {
        self.payload.get(key)
    }
}

// ---------------------------------------------------------------------------
// Parsing (mirrors dsl.py parse_step/parse_steps)
// ---------------------------------------------------------------------------

fn as_i64_nonneg(v: &Json) -> Option<i64> {
    v.as_i64().filter(|n| *n >= 0)
}

fn parse_trigger(
    raw: Option<&Json>,
    file: &str,
    line: i64,
) -> Result<Option<Map<String, Json>>, ScenarioError> {
    let Some(raw) = raw else { return Ok(None) };
    let obj = raw.as_object().ok_or_else(|| {
        err(
            file,
            line,
            "trigger: must be a mapping with a 'kind' key",
            Some("trigger"),
        )
    })?;
    let allowed = ["kind", "delay_ms", "min_agent_active_ms"];
    let mut unknown: Vec<&String> =
        obj.keys().filter(|k| !allowed.contains(&k.as_str())).collect();
    unknown.sort();
    if !unknown.is_empty() {
        return Err(err(
            file,
            line,
            &format!("unknown trigger: key(s): {unknown:?}"),
            Some("trigger"),
        ));
    }
    let kind = obj
        .get("kind")
        .and_then(|v| v.as_str())
        .ok_or_else(|| {
            err(
                file,
                line,
                "trigger: mapping requires a 'kind' key",
                Some("trigger.kind"),
            )
        })?;
    if !TRIGGER_KINDS.contains(&kind) {
        return Err(err(
            file,
            line,
            &format!("unknown trigger kind {kind:?}; expected one of {TRIGGER_KINDS:?}"),
            Some("trigger.kind"),
        ));
    }
    if let Some(d) = obj.get("delay_ms") {
        if as_i64_nonneg(d).is_none() {
            return Err(err(
                file,
                line,
                "trigger.delay_ms must be a non-negative integer (milliseconds)",
                Some("trigger.delay_ms"),
            ));
        }
    }
    if let Some(m) = obj.get("min_agent_active_ms") {
        if as_i64_nonneg(m).is_none() {
            return Err(err(
                file,
                line,
                "trigger.min_agent_active_ms must be a non-negative integer (milliseconds)",
                Some("trigger.min_agent_active_ms"),
            ));
        }
    }
    Ok(Some(obj.clone()))
}

fn parse_interaction(
    raw: Option<&Json>,
    file: &str,
    line: i64,
) -> Result<Option<Map<String, Json>>, ScenarioError> {
    let Some(raw) = raw else { return Ok(None) };
    let obj = raw.as_object().ok_or_else(|| {
        err(
            file,
            line,
            "interaction: must be a mapping",
            Some("interaction"),
        )
    })?;
    let allowed = [
        "pace",
        "hesitation",
        "stumble",
        "pre_delay",
        "pre_delay_ms",
        "backchannel",
        "barge_in",
        "interrupt_class",
        "interruption_rate",
        "interruption_interval_ms",
        "interruption_seed",
    ];
    let mut unknown: Vec<&String> =
        obj.keys().filter(|k| !allowed.contains(&k.as_str())).collect();
    unknown.sort();
    if !unknown.is_empty() {
        return Err(err(
            file,
            line,
            &format!("unknown interaction key(s): {unknown:?}"),
            Some("interaction"),
        ));
    }
    if let Some(cls) = obj.get("interrupt_class").and_then(|v| v.as_str()) {
        if !INTERRUPT_CLASSES.contains(&cls) {
            return Err(err(
                file,
                line,
                &format!(
                    "unknown interrupt_class {cls:?}; expected one of {INTERRUPT_CLASSES:?}"
                ),
                Some("interaction.interrupt_class"),
            ));
        }
    }
    // interruption_rate normalization mirrors Python: ""/none/off -> absent.
    let rate = obj.get("interruption_rate").map(|v| {
        if v.is_string() {
            v.as_str().unwrap_or("").trim().to_lowercase()
        } else {
            String::new()
        }
    });
    if let Some(r) = &rate {
        let raw_present = obj.contains_key("interruption_rate");
        if raw_present && !r.is_empty() && !["none", "off"].contains(&r.as_str()) && !INTERRUPTION_RATES.contains(&r.as_str()) {
            return Err(err(
                file,
                line,
                &format!(
                    "unknown interruption_rate {r:?}; expected one of {INTERRUPTION_RATES:?}"
                ),
                Some("interaction.interruption_rate"),
            ));
        }
    }
    if let Some(iv) = obj.get("interruption_interval_ms") {
        if iv.as_i64().map(|n| n < 1000).unwrap_or(true) {
            return Err(err(
                file,
                line,
                "interaction.interruption_interval_ms must be an integer >= 1000",
                Some("interaction.interruption_interval_ms"),
            ));
        }
    }
    if let Some(seed) = obj.get("interruption_seed") {
        if seed.as_i64().map(|n| n < 0).unwrap_or(true) {
            return Err(err(
                file,
                line,
                "interaction.interruption_seed must be a non-negative integer",
                Some("interaction.interruption_seed"),
            ));
        }
    }
    let has_rate = rate.as_ref().map(|r| !r.is_empty() && !["none", "off"].contains(&r.as_str())).unwrap_or(false);
    if !has_rate
        && (obj.contains_key("interruption_interval_ms")
            || obj.contains_key("interruption_seed"))
    {
        return Err(err(
            file,
            line,
            "interaction.interruption_interval_ms/seed require interruption_rate",
            Some("interaction.interruption_rate"),
        ));
    }
    Ok(Some(obj.clone()))
}

fn parse_constraints(
    raw: Option<&Json>,
    file: &str,
    line: i64,
) -> Result<Map<String, Json>, ScenarioError> {
    let mut out = Map::new();
    let Some(raw) = raw else { return Ok(out) };
    let obj = raw.as_object().ok_or_else(|| {
        err(
            file,
            line,
            "constraints: must be a mapping",
            Some("constraints"),
        )
    })?;
    let allowed = [
        "max_turns",
        "max_budget",
        "max_words",
        "max_duration_s",
        "forbidden_intents",
        "must_not",
    ];
    let mut unknown: Vec<&String> =
        obj.keys().filter(|k| !allowed.contains(&k.as_str())).collect();
    unknown.sort();
    if !unknown.is_empty() {
        return Err(err(
            file,
            line,
            &format!("unknown constraints key(s): {unknown:?}"),
            Some("constraints"),
        ));
    }
    if let Some(mt) = obj.get("max_turns") {
        if mt.as_i64().map(|n| n < 1).unwrap_or(true) {
            return Err(err(
                file,
                line,
                "max_turns must be >= 1",
                Some("constraints"),
            ));
        }
    }
    out = obj.clone();
    Ok(out)
}

/// Parse one step dict (mirrors dsl.py parse_step). `line_no` is 1-based.
pub fn parse_step(
    raw_step: &Map<String, Json>,
    line_no: i64,
    file: &str,
) -> Result<CallerAction, ScenarioError> {
    let present: Vec<&String> =
        raw_step.keys().filter(|k| ACTION_KINDS.contains(&k.as_str())).collect();
    if present.is_empty() {
        return Err(err(
            file,
            line_no,
            &format!(
                "step has no recognized action key; expected one of {:?}",
                ACTION_KINDS
            ),
            None,
        ));
    }
    if present.len() > 1 {
        return Err(err(
            file,
            line_no,
            &format!(
                "step has multiple action keys {:?}; exactly one is allowed",
                present
            ),
            None,
        ));
    }
    let kind = present[0].as_str();

    if kind == "say" {
        let value = raw_step.get("say").and_then(|v| v.as_str()).ok_or_else(|| {
            err(
                file,
                line_no,
                "say: must be a plain string, not a mapping",
                Some("say"),
            )
        })?;
        if value.trim().is_empty() {
            return Err(err(
                file,
                line_no,
                "say: must not be empty",
                Some("say"),
            ));
        }
        let allowed = ["say", "interaction", "trigger", "barge_in"];
        let mut unknown: Vec<&String> = raw_step
            .keys()
            .filter(|k| !allowed.contains(&k.as_str()))
            .collect();
        unknown.sort();
        if !unknown.is_empty() {
            return Err(err(
                file,
                line_no,
                &format!("unknown say: sibling key(s): {unknown:?}"),
                Some("say"),
            ));
        }
        let mut payload = Map::new();
        payload.insert("say".into(), Json::String(value.to_string()));
        if let Some(inter) = parse_interaction(raw_step.get("interaction"), file, line_no)? {
            payload.insert("interaction".into(), Json::Object(inter));
        }
        if let Some(trig) = parse_trigger(raw_step.get("trigger"), file, line_no)? {
            payload.insert("trigger".into(), Json::Object(trig));
        }
        if let Some(bi) = raw_step.get("barge_in") {
            if !bi.is_boolean() {
                return Err(err(
                    file,
                    line_no,
                    "barge_in: must be a boolean",
                    Some("barge_in"),
                ));
            }
            payload.insert("barge_in".into(), bi.clone());
        }
        return Ok(CallerAction {
            kind: kind.to_string(),
            line_no,
            payload,
        });
    }

    if kind == "do" {
        if raw_step.contains_key("trigger") || raw_step.contains_key("barge_in") {
            return Err(err(
                file,
                line_no,
                "trigger:/barge_in: are only supported on say: steps in this slice",
                Some("do"),
            ));
        }
        let allowed = ["behavior", "target", "constraints", "interaction"];
        let mut payload = Map::new();
        match raw_step.get("do") {
            Some(Json::String(behavior)) => {
                if !DEFAULT_BEHAVIOR_CATALOG.contains(&behavior.as_str()) {
                    return Err(err(
                        file,
                        line_no,
                        &format!(
                            "unknown behavior {behavior:?}; known behaviors: {:?}",
                            DEFAULT_BEHAVIOR_CATALOG
                        ),
                        Some("do.behavior"),
                    ));
                }
                payload.insert("behavior".into(), Json::String(behavior.clone()));
            }
            Some(Json::Object(map)) => {
                let mut unknown: Vec<&String> = map
                    .keys()
                    .filter(|k| !allowed.contains(&k.as_str()))
                    .collect();
                unknown.sort();
                if !unknown.is_empty() {
                    return Err(err(
                        file,
                        line_no,
                        &format!("unknown do: key(s): {unknown:?}"),
                        Some("do"),
                    ));
                }
                let behavior = map
                    .get("behavior")
                    .and_then(|v| v.as_str())
                    .ok_or_else(|| {
                        err(
                            file,
                            line_no,
                            "do: mapping requires a 'behavior' key",
                            Some("do.behavior"),
                        )
                    })?;
                if !DEFAULT_BEHAVIOR_CATALOG.contains(&behavior) {
                    return Err(err(
                        file,
                        line_no,
                        &format!(
                            "unknown behavior {behavior:?}; known behaviors: {:?}",
                            DEFAULT_BEHAVIOR_CATALOG
                        ),
                        Some("do.behavior"),
                    ));
                }
                payload.insert("behavior".into(), Json::String(behavior.to_string()));
                if let Some(t) = map.get("target") {
                    payload.insert("target".into(), t.clone());
                }
                let constraints =
                    parse_constraints(map.get("constraints"), file, line_no)?;
                if !constraints.is_empty() {
                    payload.insert(
                        "constraints".into(),
                        Json::Object(constraints),
                    );
                }
                if let Some(inter) =
                    parse_interaction(map.get("interaction"), file, line_no)?
                {
                    payload.insert("interaction".into(), Json::Object(inter));
                }
            }
            _ => {
                return Err(err(
                    file,
                    line_no,
                    "do: must be a string or mapping",
                    Some("do"),
                ));
            }
        }
        return Ok(CallerAction {
            kind: kind.to_string(),
            line_no,
            payload,
        });
    }

    if kind == "dtmf" || kind == "wait" {
        if raw_step.contains_key("trigger") || raw_step.contains_key("barge_in") {
            return Err(err(
                file,
                line_no,
                "trigger:/barge_in: are only supported on say: steps in this slice",
                Some(kind),
            ));
        }
        if kind == "dtmf" {
            let digits = raw_step.get("dtmf").and_then(|v| v.as_str()).unwrap_or("");
            if digits.is_empty() {
                return Err(err(
                    file,
                    line_no,
                    "dtmf: must be a non-empty digit string",
                    Some("dtmf"),
                ));
            }
            let mut payload = Map::new();
            payload.insert("dtmf".into(), Json::String(digits.to_string()));
            return Ok(CallerAction {
                kind: kind.to_string(),
                line_no,
                payload,
            });
        }
        let ms = raw_step.get("wait").and_then(|v| v.as_i64()).unwrap_or(-1);
        if ms < 0 {
            return Err(err(
                file,
                line_no,
                "wait: must be a non-negative integer (milliseconds)",
                Some("wait"),
            ));
        }
        let mut payload = Map::new();
        payload.insert("wait".into(), Json::Number(ms.into()));
        return Ok(CallerAction {
            kind: kind.to_string(),
            line_no,
            payload,
        });
    }

    if raw_step.contains_key("trigger") || raw_step.contains_key("barge_in") {
        // play_audio allows trigger (handled below); everything else rejects.
        if kind != "play_audio" {
            return Err(err(
                file,
                line_no,
                "trigger:/barge_in: are only supported on say: steps in this slice",
                Some(kind),
            ));
        } else if raw_step.contains_key("barge_in") {
            return Err(err(
                file,
                line_no,
                "trigger:/barge_in: are only supported on say: steps in this slice",
                Some(kind),
            ));
        }
    }

    if kind == "interrupt" {
        // interrupt: takes no value — only `true` (or an empty mapping for
        // forward-compat) is accepted; mirrors Python's raw_flag check.
        match raw_step.get("interrupt") {
            Some(Json::Bool(true)) | None => {}
            Some(Json::Object(m)) if m.is_empty() => {}
            _ => {
                return Err(err(
                    file,
                    line_no,
                    "interrupt: takes no value (optionally interaction: {interrupt_class})",
                    Some("interrupt"),
                ));
            }
        }
        let allowed = ["interrupt", "interaction"];
        let mut unknown: Vec<&String> = raw_step
            .keys()
            .filter(|k| !allowed.contains(&k.as_str()))
            .collect();
        unknown.sort();
        if !unknown.is_empty() {
            return Err(err(
                file,
                line_no,
                &format!("unknown interrupt: sibling key(s): {unknown:?}"),
                Some("interrupt"),
            ));
        }
        let mut payload = Map::new();
        if let Some(inter) =
            parse_interaction(raw_step.get("interaction"), file, line_no)?
        {
            payload.insert("interaction".into(), Json::Object(inter));
        }
        return Ok(CallerAction {
            kind: kind.to_string(),
            line_no,
            payload,
        });
    }

    if kind == "play_audio" {
        let spec = raw_step.get("play_audio").and_then(|v| v.as_object()).ok_or_else(|| {
            err(
                file,
                line_no,
                "play_audio: must be a mapping {asset:, gain:, loop:}",
                Some("play_audio"),
            )
        })?;
        let allowed = ["asset", "gain", "loop"];
        let mut unknown: Vec<&String> = spec
            .keys()
            .filter(|k| !allowed.contains(&k.as_str()))
            .collect();
        unknown.sort();
        if !unknown.is_empty() {
            return Err(err(
                file,
                line_no,
                &format!("unknown play_audio: key(s): {unknown:?}"),
                Some("play_audio"),
            ));
        }
        let mut extra: Vec<&String> = raw_step
            .keys()
            .filter(|k| !["play_audio", "trigger"].contains(&k.as_str()))
            .collect();
        extra.sort();
        if !extra.is_empty() {
            return Err(err(
                file,
                line_no,
                &format!("unknown play_audio: sibling key(s): {extra:?}"),
                Some("play_audio"),
            ));
        }
        let asset = spec.get("asset").and_then(|v| v.as_str()).unwrap_or("");
        if asset.trim().is_empty() {
            return Err(err(
                file,
                line_no,
                "play_audio.asset must be a non-empty asset ref",
                Some("play_audio.asset"),
            ));
        }
        if let Some(g) = spec.get("gain") {
            let ok = g.as_f64().map(|f| (0.0..=1.0).contains(&f)).unwrap_or(false);
            if !ok {
                return Err(err(
                    file,
                    line_no,
                    "play_audio.gain must be a number in [0.0, 1.0]",
                    Some("play_audio.gain"),
                ));
            }
        }
        if let Some(l) = spec.get("loop") {
            if !l.is_boolean() {
                return Err(err(
                    file,
                    line_no,
                    "play_audio.loop must be a boolean",
                    Some("play_audio.loop"),
                ));
            }
        }
        let mut payload = Map::new();
        payload.insert(
            "play_audio".into(),
            Json::Object(spec.clone()),
        );
        if let Some(trig) = parse_trigger(raw_step.get("trigger"), file, line_no)? {
            payload.insert("trigger".into(), Json::Object(trig));
        }
        return Ok(CallerAction {
            kind: kind.to_string(),
            line_no,
            payload,
        });
    }

    // end / silence / hangup: presence-only control actions (mirrors
    // dsl.py). Any sibling key or non-empty value is a hard error.
    if kind == "end" || kind == "silence" || kind == "hangup" {
        let mut unknown: Vec<&String> = raw_step
            .keys()
            .filter(|k| k.as_str() != kind)
            .collect();
        unknown.sort();
        if !unknown.is_empty() {
            return Err(err(
                file,
                line_no,
                &format!("unknown {kind}: sibling key(s): {unknown:?}"),
                Some(kind),
            ));
        }
        match raw_step.get(kind) {
            None | Some(Json::Bool(true)) => {}
            Some(Json::Object(m)) if m.is_empty() => {}
            _ => {
                return Err(err(
                    file,
                    line_no,
                    &format!("{kind}: takes no value"),
                    Some(kind),
                ));
            }
        }
        return Ok(CallerAction {
            kind: kind.to_string(),
            line_no,
            payload: Map::new(),
        });
    }
    // end / silence / hangup: presence only.
    Ok(CallerAction {
        kind: kind.to_string(),
        line_no,
        payload: Map::new(),
    })
}

/// Parse an ordered step list (mirrors dsl.py parse_steps).
pub fn parse_steps(
    raw_steps: &[Json],
    file: &str,
) -> Result<Vec<CallerAction>, ScenarioError> {
    raw_steps
        .iter()
        .enumerate()
        .map(|(idx, step)| {
            let map = step.as_object().ok_or_else(|| {
                err(
                    file,
                    (idx + 1) as i64,
                    "step must be a mapping",
                    None,
                )
            })?;
            parse_step(map, (idx + 1) as i64, file)
        })
        .collect()
}

// ---------------------------------------------------------------------------
// Parity tests: SAME vectors as Python test_dsl_parity_vectors.py
// ---------------------------------------------------------------------------

#[cfg(test)]
mod parity_tests {
    use super::*;
    use serde_json::Value;
    use std::fs;
    use std::path::PathBuf;

    fn fixtures_dir() -> PathBuf {
        PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../../../..")
            .join("tests")
            .join("fixtures")
            .join("parity")
            .join("dsl")
    }

    fn load(name: &str) -> Value {
        let path = fixtures_dir().join(name);
        let raw = fs::read_to_string(&path)
            .unwrap_or_else(|e| panic!("failed to read fixture {}: {}", path.display(), e));
        serde_json::from_str(&raw)
            .unwrap_or_else(|e| panic!("invalid JSON in {}: {}", path.display(), e))
    }

    #[test]
    fn dsl_vectors_parse_identically_in_rust() {
        for entry in fs::read_dir(fixtures_dir()).expect("dsl fixtures dir") {
            let entry = entry.expect("dir entry");
            let name = entry.file_name().to_string_lossy().to_string();
            if !name.ends_with(".json") {
                continue;
            }
            let data = load(&name);
            let steps = data["steps"].as_array().expect("steps array");
            let result = parse_steps(steps, "vector");
            match data["expect"].as_str().expect("expect") {
                "ok" => {
                    let actions = result.unwrap_or_else(|e| panic!("{name}: expected ok, got {e}"));
                    let kinds: Vec<String> = actions.iter().map(|a| a.kind.clone()).collect();
                    let expected: Vec<String> = data["expected_kinds"]
                        .as_array()
                        .expect("expected_kinds")
                        .iter()
                        .map(|v| v.as_str().unwrap().to_string())
                        .collect();
                    assert_eq!(kinds, expected, "{name}: kinds mismatch");
                    if let Some(checks) = data["expected_fields"].as_object() {
                        for (idx_s, fields) in checks {
                            let idx: usize = idx_s.parse().expect("field index");
                            for (k, v) in fields.as_object().expect("fields") {
                                let actual = actions[idx]
                                    .payload
                                    .get(k)
                                    .unwrap_or_else(|| panic!("{name}[{idx}]: missing {k}"));
                                assert_eq!(actual, v, "{name}[{idx}].{k} mismatch");
                            }
                        }
                    }
                }
                "error" => {
                    assert!(result.is_err(), "{name}: expected error, parsed ok");
                }
                other => panic!("{name}: unknown expect {other:?}"),
            }
        }
    }
}
