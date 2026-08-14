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

/// Parse a variant from YAML text (port of `optimize/variant.py:load_variant`).
pub fn parse_variant_yaml(text: &str) -> Result<PromptVariant, String> {
    let val: Json = yaml_serde::from_str(text).map_err(|e| format!("not valid YAML: {e}"))?;
    let data = match val {
        Json::Object(m) => m,
        _ => return Err("optimized prompt artifact must be a mapping".to_string()),
    };
    load_variant(&data)
}

/// Compose the persona system instruction a saved variant would produce
/// (port of `optimize/apply.py:policy_for_variant` + `build_persona_system_instruction`).
///
/// Applies the variant's verbosity to the persona speech_conditions and
/// reorders sections (subset semantics — unlisted sections appended), then
/// composes the full 10-section prompt. `persona` is the scenario persona map.
pub fn render_variant_prompt_for_persona(
    variant: &PromptVariant,
    persona: &Map<String, Json>,
    locale: &str,
    context: &Map<String, Json>,
    script_steps: &[Json],
    first_speaker: &str,
) -> String {
    // Apply variant verbosity to a persona copy (apply_variant_to_persona).
    let mut persona = persona.clone();
    if let Some(verb) = &variant.verbosity {
        let mut sc = persona
            .get("speech_conditions")
            .or_else(|| persona.get("speechConditions"))
            .and_then(|v| v.as_object())
            .cloned()
            .unwrap_or_default();
        sc.insert("verbosity".into(), Json::String(verb.clone()));
        persona.insert("speech_conditions".into(), Json::Object(sc));
    }

    let ctx = crate::caller_policy::CallerPolicyContext {
        persona,
        locale: locale.to_string(),
        context: context.clone(),
        script_steps: script_steps.to_vec(),
        first_speaker: first_speaker.to_string(),
    };

    // Compose with optional section reorder + guardrail extras.
    let all = crate::prompt_sections::all_sections(&ctx);
    let names: Vec<&str> = SECTION_NAMES.to_vec();
    let mut by_name: std::collections::HashMap<&str, Vec<String>> =
        std::collections::HashMap::new();
    for (name, lines) in names.iter().zip(all.iter()) {
        by_name.insert(name, lines.clone());
    }

    let mut ordered: Vec<&str> = Vec::new();
    if !variant.section_order.is_empty() {
        for name in &variant.section_order {
            if names.contains(&name.as_str()) {
                ordered.push(name.as_str());
            }
        }
        // Append any default sections not listed (subset semantics).
        for name in &names {
            if !variant.section_order.contains(&name.to_string()) {
                ordered.push(name);
            }
        }
    } else {
        ordered = names.clone();
    }

    let mut lines: Vec<String> = Vec::new();
    let mut seen_guardrails = false;
    for name in ordered {
        let mut section_lines = by_name.get(name).cloned().unwrap_or_default();
        if name == "Guardrails" {
            seen_guardrails = true;
            let extras: Vec<String> = variant
                .extra_guardrails
                .iter()
                .chain(
                    variant
                        .extra_lines
                        .get("Guardrails")
                        .map(|v| v.iter())
                        .unwrap_or_else(|| [].iter()),
                )
                .cloned()
                .collect();
            if !extras.is_empty() {
                section_lines.extend(extras);
            }
        }
        lines.extend(section_lines);
    }
    // Guardrails not in the reorder → append at the end (subset semantics keeps it).
    if !seen_guardrails {
        lines.extend(by_name.get("Guardrails").cloned().unwrap_or_default());
    }
    lines.join("\n")
}

/// Parse a variant from YAML text and render the prompt it would produce.
/// Used by the runtime `--optimized` seam (no persona override → builtin compose).
pub fn render_variant_prompt(variant: &PromptVariant) -> String {
    // Without a persona, the variant's structural knobs are the surface:
    // compose the guardrail/extra-line deltas onto a minimal empty prompt.
    // (The full persona-aware path runs through render_variant_prompt_for_persona.)
    let empty = Map::new();
    render_variant_prompt_for_persona(variant, &empty, "en-US", &empty, &[], "agent")
}
