//! PromptVariant — a structural persona-prompt mutation (port of
//! `optimize/variant.py`). Small JSON object so candidates are diffable and
//! re-applicable under `.agent-sim/optimized/<name>/prompt.yaml`.

use serde_json::{json, Map, Value as Json};
use std::collections::BTreeMap;

/// The 10 section names DefaultCallerPolicy composes.
pub const SECTION_NAMES: [&str; 10] = [
    "Role",
    "Goals",
    "StyleTraits",
    "NaturalSpeech",
    "Constraints",
    "SpeechConditions",
    "Context",
    "ScriptTiming",
    "FirstSpeaker",
    "Guardrails",
];
pub const VALID_VERBOSITY: [&str; 3] = ["quiet", "natural", "chatty"];

#[derive(Debug, Clone, PartialEq)]
pub struct PromptVariant {
    pub id: String,
    pub verbosity: Option<String>,
    pub section_order: Vec<String>,
    pub extra_guardrails: Vec<String>,
    pub extra_lines: BTreeMap<String, Vec<String>>,
    pub parent_id: Option<String>,
    pub description: String,
}

/// Return a list of validation problems (empty = valid).
pub fn validate_variant(v: &PromptVariant) -> Vec<String> {
    let mut problems: Vec<String> = Vec::new();
    if let Some(verb) = &v.verbosity {
        if !VALID_VERBOSITY.contains(&verb.as_str()) {
            problems.push(format!(
                "verbosity {verb:?} must be one of {VALID_VERBOSITY:?}"
            ));
        }
    }
    let known: std::collections::HashSet<&str> = SECTION_NAMES.iter().copied().collect();
    for name in &v.section_order {
        if !known.contains(name.as_str()) {
            problems.push(format!("unknown section {name:?} in section_order"));
        }
    }
    let mut seen = std::collections::HashSet::new();
    for name in &v.section_order {
        if !seen.insert(name) {
            problems.push("section_order has duplicates".to_string());
            break;
        }
    }
    for name in v.extra_lines.keys() {
        if !known.contains(name.as_str()) {
            problems.push(format!("unknown section {name:?} in extra_lines"));
        }
    }
    problems
}

/// Variant → dict, omitting empty fields (mirror variant_to_dict).
pub fn variant_to_dict(v: &PromptVariant) -> Map<String, Json> {
    let mut d = Map::new();
    d.insert("id".into(), json!(v.id));
    if let Some(verb) = &v.verbosity {
        d.insert("verbosity".into(), json!(verb));
    }
    if !v.section_order.is_empty() {
        d.insert("section_order".into(), json!(v.section_order));
    }
    if !v.extra_guardrails.is_empty() {
        d.insert("extra_guardrails".into(), json!(v.extra_guardrails));
    }
    if !v.extra_lines.is_empty() {
        let mut el = Map::new();
        for (k, lines) in &v.extra_lines {
            el.insert(k.clone(), json!(lines));
        }
        d.insert("extra_lines".into(), Json::Object(el));
    }
    if let Some(p) = &v.parent_id {
        d.insert("parent_id".into(), json!(p));
    }
    if !v.description.is_empty() {
        d.insert("description".into(), json!(v.description));
    }
    d
}

/// Dict → Variant (mirror variant_from_dict).
pub fn variant_from_dict(data: &Map<String, Json>) -> PromptVariant {
    let extra_lines = data
        .get("extra_lines")
        .and_then(|v| v.as_object())
        .map(|m| {
            m.iter()
                .map(|(k, v)| {
                    let lines = v
                        .as_array()
                        .map(|a| {
                            a.iter()
                                .map(|x| x.as_str().unwrap_or(&x.to_string()).to_string())
                                .collect()
                        })
                        .unwrap_or_default();
                    (k.clone(), lines)
                })
                .collect()
        })
        .unwrap_or_default();
    PromptVariant {
        id: data
            .get("id")
            .map(|v| v.as_str().unwrap_or(&v.to_string()).to_string())
            .unwrap_or_else(|| "v".to_string()),
        verbosity: data
            .get("verbosity")
            .and_then(|v| v.as_str())
            .map(String::from),
        section_order: data
            .get("section_order")
            .and_then(|v| v.as_array())
            .map(|a| {
                a.iter()
                    .map(|x| x.as_str().unwrap_or(&x.to_string()).to_string())
                    .collect()
            })
            .unwrap_or_default(),
        extra_guardrails: data
            .get("extra_guardrails")
            .and_then(|v| v.as_array())
            .map(|a| {
                a.iter()
                    .map(|x| x.as_str().unwrap_or(&x.to_string()).to_string())
                    .collect()
            })
            .unwrap_or_default(),
        extra_lines,
        parent_id: data
            .get("parent_id")
            .and_then(|v| v.as_str())
            .map(String::from),
        description: data
            .get("description")
            .map(|v| v.as_str().unwrap_or(&v.to_string()).to_string())
            .unwrap_or_default(),
    }
}

/// Validate a variant, returning a user-facing error on the first problem.
pub fn load_variant(data: &Map<String, Json>) -> Result<PromptVariant, String> {
    let v = variant_from_dict(data);
    let problems = validate_variant(&v);
    if !problems.is_empty() {
        return Err(format!(
            "invalid optimized prompt artifact: {}",
            problems.join("; ")
        ));
    }
    Ok(v)
}
