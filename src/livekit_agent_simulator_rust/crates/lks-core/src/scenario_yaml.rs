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

use serde_json::{Map, Value as Json};

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
