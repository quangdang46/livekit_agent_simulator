//! Scenario discovery ops — find_scenario / list_scenarios / export_scenario
//! (mirror scenario.py + ops.py).
//!
//! Contract (scenario.py):
//! - find_scenario: YAML shadows same-stem .jsonl/.yml; direct `<id>.yaml` →
//!   `<id>.yml` → `<id>.jsonl` first, then metadata.id scan (jsonl → yaml → yml,
//!   each sorted/deduped). Scenario-id regex `^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$`
//!   applied to the QUERY only.
//! - list_scenarios: best-effort; YAML shadows same-stem .jsonl/.yml; invalid
//!   files included with `error` field.

use std::path::{Path, PathBuf};

use serde_json::{Map, Value as Json};

use crate::errors::ScenarioError;
use crate::scenario_jsonl::parse_scenario_jsonl;
use crate::scenario_yaml::load_scenario_yaml;

/// Scenario-id regex: `^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$` (applied to QUERY only).
pub fn is_valid_scenario_id(id: &str) -> bool {
    let mut chars = id.chars();
    match chars.next() {
        Some(c) if c.is_ascii_alphanumeric() => {}
        _ => return false,
    }
    let mut len = 1;
    for c in chars {
        if !(c.is_ascii_alphanumeric() || c == '_' || c == '-') {
            return false;
        }
        len += 1;
        if len > 64 {
            return false;
        }
    }
    true
}

/// Yield *.jsonl then *.yaml/yml (sorted, deduped) — mirror `_iter_scenario_files`.
fn iter_scenario_files(scenarios_dir: &Path) -> Vec<PathBuf> {
    let mut seen = std::collections::HashSet::new();
    let mut out = Vec::new();
    for pattern in ["*.jsonl", "*.yaml", "*.yml"] {
        let mut matches: Vec<PathBuf> = glob(scenarios_dir, pattern);
        matches.sort();
        for f in matches {
            if seen.insert(f.clone()) {
                out.push(f);
            }
        }
    }
    out
}

fn glob(dir: &Path, pattern: &str) -> Vec<PathBuf> {
    let mut out = Vec::new();
    let Ok(entries) = std::fs::read_dir(dir) else {
        return out;
    };
    for e in entries.flatten() {
        let name = e.file_name().to_string_lossy().into_owned();
        if name_matches(&name, pattern) && e.path().is_file() {
            out.push(e.path());
        }
    }
    out
}

fn name_matches(name: &str, pattern: &str) -> bool {
    // Pattern is "*.ext" — match any stem with that extension.
    let ext = &pattern[1..]; // ".jsonl" etc.
    name.ends_with(ext)
}

fn parse_scenario(path: &Path) -> Result<crate::scenario::Scenario, ScenarioError> {
    let lower = path
        .extension()
        .and_then(|e| e.to_str())
        .unwrap_or("")
        .to_lowercase();
    if lower == "yaml" || lower == "yml" {
        load_scenario_yaml(&path.to_path_buf())
    } else {
        parse_scenario_jsonl(&path.to_path_buf())
    }
}

/// Find a scenario by id (YAML canonical). Raises ScenarioError if not found.
pub fn find_scenario(
    scenarios_dir: &Path,
    scenario_id: &str,
) -> Result<crate::scenario::Scenario, ScenarioError> {
    if !is_valid_scenario_id(scenario_id) {
        return Err(ScenarioError(format!(
            "Invalid scenario_id {scenario_id:?}: use letters/digits/[_-], start with alnum, max 64 chars"
        )));
    }
    // Direct file preference: yaml → yml → jsonl.
    for ext in ["yaml", "yml", "jsonl"] {
        let direct = scenarios_dir.join(format!("{scenario_id}.{ext}"));
        if direct.exists() {
            return parse_scenario(&direct);
        }
    }
    // metadata.id scan (jsonl → yaml → yml order).
    for f in iter_scenario_files(scenarios_dir) {
        if let Ok(s) = parse_scenario(&f) {
            if s.id == scenario_id {
                return Ok(s);
            }
        }
    }
    Err(ScenarioError(format!(
        "Scenario `{scenario_id}` not found in {} (looked for {scenario_id}.jsonl/.yaml/.yml and metadata.id match)",
        scenarios_dir.display()
    )))
}

/// Best-effort listing — invalid files included with `error` field.
pub fn list_scenarios(scenarios_dir: &Path) -> Vec<Map<String, Json>> {
    let yaml_stems: std::collections::HashSet<String> = glob(scenarios_dir, "*.yaml")
        .into_iter()
        .filter_map(|p| p.file_stem().map(|s| s.to_string_lossy().into_owned()))
        .collect();

    let mut out = Vec::new();
    for f in iter_scenario_files(scenarios_dir) {
        let lower = f
            .extension()
            .and_then(|e| e.to_str())
            .unwrap_or("")
            .to_lowercase();
        if lower == "jsonl" || lower == "yml" {
            if let Some(stem) = f.file_stem() {
                if yaml_stems.contains(&stem.to_string_lossy().into_owned()) {
                    continue; // YAML shadows
                }
            }
        }
        match parse_scenario(&f) {
            Ok(s) => {
                let mut m = Map::new();
                m.insert("id".into(), Json::String(s.id.clone()));
                m.insert(
                    "file".into(),
                    Json::String(
                        f.file_name()
                            .map(|n| n.to_string_lossy().into_owned())
                            .unwrap_or_default(),
                    ),
                );
                m.insert("locale".into(), Json::String(s.locale.clone()));
                m.insert(
                    "tags".into(),
                    Json::Array(s.tags.iter().map(|t| Json::String(t.clone())).collect()),
                );
                let rs = s.run_spec();
                m.insert("max_turns".into(), Json::Number(rs.max_turns.into()));
                m.insert(
                    "first_speaker".into(),
                    Json::String(rs.first_speaker.clone()),
                );
                m.insert("has_execute".into(), Json::Bool(s.execute.is_some()));
                m.insert(
                    "has_dispatch".into(),
                    Json::Bool(s.dispatch.as_ref().is_some_and(|d| d.metadata.is_some())),
                );
                m.insert(
                    "caller_mode".into(),
                    Json::String(s.effective_caller_mode().into()),
                );
                m.insert(
                    "pass_criteria".into(),
                    Json::Number(s.pass_criteria.len().into()),
                );
                m.insert(
                    "script_steps".into(),
                    Json::Number(s.script_steps.len().into()),
                );
                out.push(m);
            }
            Err(e) => {
                let mut m = Map::new();
                m.insert("id".into(), Json::Null);
                m.insert(
                    "file".into(),
                    Json::String(
                        f.file_name()
                            .map(|n| n.to_string_lossy().into_owned())
                            .unwrap_or_default(),
                    ),
                );
                m.insert("error".into(), Json::String(e.to_string()));
                out.push(m);
            }
        }
    }
    out
}

/// Export parsed scenario JSON (found:false with error when not found).
pub fn export_scenario(scenarios_dir: &Path, scenario_id: &str) -> Map<String, Json> {
    match find_scenario(scenarios_dir, scenario_id) {
        Ok(s) => {
            let mut m = Map::new();
            m.insert("found".into(), Json::Bool(true));
            for (k, v) in crate::scenario_yaml::scenario_to_dict(&s) {
                m.insert(k, v);
            }
            m
        }
        Err(e) => {
            let mut m = Map::new();
            m.insert("found".into(), Json::Bool(false));
            m.insert("scenario_id".into(), Json::String(scenario_id.to_string()));
            m.insert("error".into(), Json::String(e.to_string()));
            m
        }
    }
}
