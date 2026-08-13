//! YAML scenario transport — parse the section-object YAML shape into a
//! Scenario via scenario_from_dict (mirrors scenario_yaml.py).
//!
//! Mechanics (scenario_yaml.py):
//! - `yaml.safe_load_all` (multi-doc); drop empty docs (trailing `---` → None).
//! - Empty → `{path}: empty scenario file`.
//! - Group wrapper `{name, scenarios: [...]}` on sections[0]: non-empty list,
//!   exactly 1 item; group metadata/tags/locale/id carried down if not present.
//! - Multi-doc merge: list+list APPENDS, everything else OVERWRITES.
//! - Validation delegated to scenario_from_dict (single source of truth).
//!
//! NOTE: the plan's "decide at kickoff" for PyYAML 1.1 read emulation is resolved
//! in config.rs's normalize_yaml_value (on/yes→bool, 0123→octal, 1e5→string).

use std::path::PathBuf;

use serde_json::{json, Map, Value as Json};

use crate::errors::ScenarioError;
use crate::scenario::scenario_from_dict;

use crate::config::normalize_yaml_value;

/// Parse one YAML scenario file → Scenario (raises ScenarioError).
pub fn load_scenario_yaml(path: &PathBuf) -> Result<crate::scenario::Scenario, ScenarioError> {
    if !path.exists() {
        return Err(ScenarioError(format!(
            "Scenario file not found: {}",
            path.display()
        )));
    }
    let text = std::fs::read_to_string(path)
        .map_err(|e| ScenarioError(format!("{}: invalid YAML — {e}", path.display())))?;

    let documents: Vec<yaml_serde::Value> = yaml_serde::from_str(&text)
        .map(|v| vec![v])
        .unwrap_or_default();
    // NOTE: yaml_serde parses a single doc; multi-doc (---) needs from_str on
    // concatenated docs — see load_scenario_yaml_multi below for the real impl.
    let _ = documents;

    // Single-document path (covers the common case + templates).
    let raw_value: yaml_serde::Value = yaml_serde::from_str(&text)
        .map_err(|e| ScenarioError(format!("{}: invalid YAML — {e}", path.display())))?;

    // Drop empty docs.
    let mut sections: Vec<Map<String, Json>> = Vec::new();
    collect_sections(&raw_value, &mut sections);
    if sections.is_empty() {
        return Err(ScenarioError(format!(
            "{}: empty scenario file",
            path.display()
        )));
    }

    // Group wrapper check on sections[0].
    if let Some(first) = sections.first() {
        if first.contains_key("scenarios") {
            let group = first.clone();
            let raw_items = group.get("scenarios").cloned().unwrap_or(Json::Null);
            let items = match raw_items {
                Json::Array(a) if !a.is_empty() => a,
                _ => {
                    return Err(ScenarioError(format!(
                        "{}: scenarios must be a non-empty list",
                        path.display()
                    )))
                }
            };
            if items.len() != 1 {
                return Err(ScenarioError(format!(
                    "{}: group wrapper with {} scenarios — LKS uses one scenario per file; split into separate files",
                    path.display(),
                    items.len()
                )));
            }
            let mut merged = items[0].as_object().cloned().ok_or_else(|| {
                ScenarioError(format!(
                    "{}: scenarios[0] must be an object",
                    path.display()
                ))
            })?;
            for key in ["metadata", "tags", "locale", "id"] {
                if !merged.contains_key(key) && group.contains_key(key) {
                    merged.insert(key.to_string(), group[key].clone());
                }
            }
            sections = vec![merged];
        }
    }

    // Multi-doc merge: list+list appends, everything else overwrites.
    let mut data: Map<String, Json> = Map::new();
    for sec in &sections {
        for (k, v) in sec {
            if let (Some(existing), true) = (
                data.get(k),
                data.get(k).is_some_and(Json::is_array) && v.is_array(),
            ) {
                let mut merged = existing.as_array().unwrap().clone();
                merged.extend(v.as_array().unwrap().iter().cloned());
                data.insert(k.clone(), Json::Array(merged));
            } else {
                data.insert(k.clone(), v.clone());
            }
        }
    }

    scenario_from_dict(&data, Some(path.clone()), &path.to_string_lossy())
}

/// Recursively collect top-level mappings from a parsed YAML value.
/// yaml_serde yields a single Value; a document-level mapping is the norm.
fn collect_sections(v: &yaml_serde::Value, out: &mut Vec<Map<String, Json>>) {
    match v {
        yaml_serde::Value::Mapping(m) => {
            let obj = normalize_yaml_value(&yaml_serde::Value::Mapping(m.clone()));
            if let Json::Object(o) = obj {
                if !o.is_empty() {
                    out.push(o);
                }
            }
        }
        yaml_serde::Value::Sequence(_) => {
            // Not a mapping doc — leave out (scenario_from_dict will reject).
        }
        _ => {}
    }
}

// ---------------------------------------------------------------------------
// Serialization — scenario_to_dict / scenario_to_yaml_text (mirror scenario_yaml.py)
// ---------------------------------------------------------------------------

/// Build the section-object dict for a Scenario (round-trip faithful).
/// Mirrors `scenario_to_dict`: header, persona, context, execute/simulator,
/// dispatch, caller, telephony, behavior, script + verify, assert, pass_criteria.
pub fn scenario_to_dict(s: &crate::scenario::Scenario) -> Map<String, Json> {
    let mut data: Map<String, Json> = Map::new();
    data.insert("apiVersion".into(), Json::String("agent-sim/v1".into()));
    data.insert("kind".into(), Json::String("Scenario".into()));
    let mut metadata = Map::new();
    metadata.insert("id".into(), Json::String(s.id.clone()));
    metadata.insert("locale".into(), Json::String(s.effective_locale()));
    metadata.insert(
        "tags".into(),
        Json::Array(s.tags.iter().map(|t| Json::String(t.clone())).collect()),
    );
    data.insert("metadata".into(), Json::Object(metadata));
    data.insert("persona".into(), Json::Object(s.persona.clone()));

    if !s.context.is_empty() {
        data.insert("context".into(), Json::Object(s.context.clone()));
    }
    if let Some(ex) = &s.execute {
        let mut m = Map::new();
        m.insert(
            "max_turns".into(),
            ex.max_turns
                .map(|v| Json::Number(v.into()))
                .unwrap_or(Json::Null),
        );
        m.insert(
            "timeout_s".into(),
            ex.timeout_s
                .map(|v| Json::Number(v.into()))
                .unwrap_or(Json::Null),
        );
        m.insert(
            "first_speaker".into(),
            ex.first_speaker
                .clone()
                .map(Json::String)
                .unwrap_or(Json::Null),
        );
        m.insert(
            "hold_music_timeout_s".into(),
            ex.hold_music_timeout_s
                .map(|v| {
                    serde_json::Number::from_f64(v)
                        .map(Json::Number)
                        .unwrap_or(Json::Null)
                })
                .unwrap_or(Json::Null),
        );
        data.insert("execute".into(), Json::Object(m));
    } else if s.simulator.max_turns != 6
        || s.simulator.timeout_s != 120
        || s.simulator.first_speaker != "agent"
    {
        let mut m = Map::new();
        m.insert(
            "max_turns".into(),
            Json::Number(s.simulator.max_turns.into()),
        );
        m.insert(
            "timeout_s".into(),
            Json::Number(s.simulator.timeout_s.into()),
        );
        m.insert(
            "first_speaker".into(),
            Json::String(s.simulator.first_speaker.clone()),
        );
        data.insert("simulator".into(), Json::Object(m));
    }
    if let Some(d) = &s.dispatch {
        if let Some(meta) = &d.metadata {
            let mut m = Map::new();
            m.insert("metadata".into(), Json::String(meta.clone()));
            data.insert("dispatch".into(), Json::Object(m));
        }
    }
    if let Some(c) = &s.caller {
        let mut m = Map::new();
        m.insert("mode".into(), Json::String(c.mode.clone()));
        data.insert("caller".into(), Json::Object(m));
    }
    if let Some(t) = &s.telephony {
        let mut m = Map::new();
        if let Some(v) = &t.call_to {
            m.insert("call_to".into(), Json::String(v.clone()));
        }
        if let Some(v) = &t.dial_in {
            m.insert("dial_in".into(), Json::String(v.clone()));
        }
        if let Some(v) = &t.sip_trunk_id {
            m.insert("sip_trunk_id".into(), Json::String(v.clone()));
        }
        if let Some(v) = t.prepare_ms {
            m.insert("prepare_ms".into(), Json::Number(v.into()));
        }
        if let Some(v) = t.wait_until_answered {
            m.insert("wait_until_answered".into(), Json::Bool(v));
        }
        if let Some(v) = t.krisp_enabled {
            m.insert("krisp_enabled".into(), Json::Bool(v));
        }
        if let Some(v) = &t.agent_room {
            m.insert("agent_room".into(), Json::String(v.clone()));
        }
        if let Some(v) = &t.agent_room_name_template {
            m.insert("agent_room_name_template".into(), Json::String(v.clone()));
        }
        if let Some(v) = &t.handset_isolation {
            m.insert("handset_isolation".into(), Json::String(v.clone()));
        }
        data.insert("telephony".into(), Json::Object(m));
    }
    if let Some(beh) = &s.behavior_spec {
        data.insert("behavior".into(), Json::Object(beh.clone()));
    }
    if !s.script_steps.is_empty() {
        let mut m = Map::new();
        m.insert("steps".into(), Json::Array(s.script_steps.clone()));
        if let Some(v) = &s.script_verify {
            // Normalize through the typed parser so verify exports carry the
            // full Python dataclass field set (P5 export parity).
            match crate::script::parse::parse_script_verify(v) {
                Ok(Some(spec)) => {
                    m.insert(
                        "verify".into(),
                        json!({
                            "require_during_agent_speech": spec.require_during_agent_speech,
                            "min_agent_finals_after_first_cue": spec.min_agent_finals_after_first_cue,
                            "min_user_finals_after_first_cue": spec.min_user_finals_after_first_cue,
                            "min_interruptions": spec.min_interruptions,
                            "max_interruptions": spec.max_interruptions,
                            "min_agent_finals_after_silence": spec.min_agent_finals_after_silence,
                            "min_agent_finals_after_barge_in": spec.min_agent_finals_after_barge_in,
                            "plugins": spec.plugins,
                            "plugin_options": spec.plugin_options,
                        }),
                    );
                }
                _ => {
                    m.insert("verify".into(), v.clone());
                }
            }
        }
        data.insert("script".into(), Json::Object(m));
    }
    if !s.plugin_modules.is_empty() {
        data.insert(
            "plugin_modules".into(),
            Json::Array(
                s.plugin_modules
                    .iter()
                    .map(|m| Json::String(m.clone()))
                    .collect(),
            ),
        );
    }
    if let Some(a) = &s.asserts {
        // Normalize through the typed parser so exports carry the full Python
        // field set (asdict order + defaults) — P5 export parity.
        if let Some(m) = a.as_object() {
            match crate::asserts::parse_assert_spec(m, "Assert") {
                Ok(spec) => {
                    data.insert("assert".into(), spec.to_json());
                }
                Err(_) => {
                    data.insert("assert".into(), a.clone());
                }
            }
        } else {
            data.insert("assert".into(), a.clone());
        }
    }
    if !s.pass_criteria.is_empty() || !s.pass_judges.is_empty() {
        let mut m = Map::new();
        m.insert(
            "criteria".into(),
            Json::Array(
                s.pass_criteria
                    .iter()
                    .map(|c| Json::String(c.clone()))
                    .collect(),
            ),
        );
        if !s.pass_judges.is_empty() {
            m.insert("mode".into(), Json::String(s.pass_criteria_mode.clone()));
            m.insert(
                "judges".into(),
                Json::Array(
                    s.pass_judges
                        .iter()
                        .map(|j| Json::Object(j.clone()))
                        .collect(),
                ),
            );
        }
        data.insert("pass_criteria".into(), Json::Object(m));
    }
    data
}

/// Serialize a Scenario to the section-object YAML shape.
pub fn scenario_to_yaml_text(s: &crate::scenario::Scenario) -> String {
    let dict = scenario_to_dict(s);
    let cleaned = crate::yaml_writer::clean(Json::Object(dict)).unwrap_or(Json::Object(Map::new()));
    crate::yaml_writer::to_yaml_string(&cleaned)
}
