//! Hamming-style authoring quality checks for scenarios (P1.G / #27) — port of
//! `authoring.py` findings. Rule-based only — no LLM. Soft by default: never
//! flips `valid` false. `validate_scenario` merges warning messages into the
//! flat `warnings` list.
//!
//! Ported surface (P5 data-plane): the flat `collect_authoring_warnings`
//! message list — the exact strings the Python reference emits, in the same
//! order. The structured `authoring` object (scorecard/tier/codes) is a P1.G
//! follow-up.

use serde_json::{json, Map, Value as Json};

use crate::script::parse::parse_script_verify;

const STRESS_TRAITS: [&str; 8] = [
    "interrupts",
    "impatient",
    "hangup_threat",
    "angry",
    "urgent",
    "backchannel",
    "silent",
    "quiet",
];

const RISK_TAGS: [&str; 6] = [
    "blocking",
    "scheduled",
    "exploratory",
    "draft",
    "smoke",
    "regression",
];

fn as_str(v: &Json) -> String {
    match v {
        Json::String(s) => s.clone(),
        Json::Number(n) => n.to_string(),
        Json::Bool(b) => b.to_string(),
        Json::Null => String::new(),
        other => other.to_string(),
    }
}

fn persona_goals(persona: &Map<String, Json>) -> Vec<String> {
    match persona.get("goals") {
        Some(Json::String(s)) => vec![s.clone()],
        Some(Json::Array(a)) => a.iter().map(as_str).collect(),
        _ => Vec::new(),
    }
    .into_iter()
    .map(|g| g.trim().to_string())
    .filter(|g| !g.is_empty())
    .collect()
}

fn persona_traits(persona: &Map<String, Json>) -> Vec<String> {
    let raw = persona
        .get("traits")
        .or_else(|| persona.get("behaviors"))
        .cloned();
    let list: Vec<String> = match raw {
        Some(Json::String(s)) => vec![s],
        Some(Json::Array(a)) => a.iter().map(as_str).collect(),
        _ => Vec::new(),
    };
    list.into_iter()
        .filter_map(|t| {
            let key = t.trim().to_lowercase().replace([' ', '-'], "_");
            if key.is_empty() {
                None
            } else {
                Some(key)
            }
        })
        .collect()
}

fn persona_constraints(persona: &Map<String, Json>) -> Vec<String> {
    match persona.get("constraints") {
        Some(Json::String(s)) => vec![s.clone()],
        Some(Json::Array(a)) => a.iter().map(as_str).collect(),
        _ => Vec::new(),
    }
    .into_iter()
    .map(|c| c.trim().to_string())
    .filter(|c| !c.is_empty())
    .collect()
}

fn scenario_tags(tags: &[String]) -> Vec<String> {
    tags.iter()
        .map(|t| t.trim().to_lowercase())
        .filter(|t| !t.is_empty())
        .collect()
}

fn first_speaker(
    execute: Option<&crate::scenario::ExecuteSpec>,
    sim: &crate::scenario::SimulatorSpec,
) -> String {
    if let Some(ex) = execute {
        if let Some(fs) = &ex.first_speaker {
            if !fs.is_empty() {
                return fs.clone();
            }
        }
    }
    sim.first_speaker.clone()
}

fn silent_mode(persona: &Map<String, Json>) -> bool {
    let sc = persona
        .get("speech_conditions")
        .or_else(|| persona.get("speechConditions"))
        .and_then(|v| v.as_object());
    let Some(sc) = sc else { return false };
    let raw = sc
        .get("silent_mode")
        .or_else(|| sc.get("silentMode"))
        .or_else(|| sc.get("silent"));
    match raw {
        Some(Json::Bool(true)) => true,
        Some(Json::Number(n)) => n.as_i64() == Some(1),
        Some(Json::String(s)) => {
            let s = s.trim().to_lowercase();
            matches!(s.as_str(), "1" | "true" | "yes" | "on" | "silent")
        }
        _ => false,
    }
}

/// `counts_for_recovery_barge` on raw step JSON (mirror `script/models.py`).
fn counts_for_recovery_barge_step(step: &Json) -> bool {
    let barge_in = step
        .get("barge_in")
        .and_then(|v| v.as_bool())
        .unwrap_or(false);
    if !barge_in {
        return false;
    }
    let cls = step
        .get("interrupt_class")
        .or_else(|| step.get("class"))
        .and_then(|v| v.as_str())
        .unwrap_or("correction");
    crate::script::RECOVERY_BARGE_CLASSES.contains(&cls)
}

fn has_recovery_proof(asserts: Option<&Json>, script_verify: Option<&Json>) -> bool {
    if let Some(a) = asserts {
        if let Some(outcomes) = a.get("outcomes").and_then(|v| v.as_array()) {
            if outcomes
                .iter()
                .any(|o| as_str(o.get("type").unwrap_or(&Json::Null)) == "recovery")
            {
                return true;
            }
        }
    }
    if let Some(sv) = script_verify {
        if let Ok(Some(spec)) = parse_script_verify(sv) {
            if spec.min_agent_finals_after_barge_in > 0 {
                return true;
            }
        }
    }
    false
}

fn has_constraint_proof(asserts: Option<&Json>) -> bool {
    asserts
        .and_then(|a| a.get("outcomes").and_then(|v| v.as_array()))
        .map(|outcomes| {
            outcomes
                .iter()
                .any(|o| as_str(o.get("type").unwrap_or(&Json::Null)) == "constraint_respected")
        })
        .unwrap_or(false)
}

fn has_ended_by_proof(asserts: Option<&Json>) -> bool {
    asserts
        .and_then(|a| a.get("outcomes").and_then(|v| v.as_array()))
        .map(|outcomes| {
            outcomes
                .iter()
                .any(|o| as_str(o.get("type").unwrap_or(&Json::Null)) == "ended_by")
        })
        .unwrap_or(false)
}

/// Port of `authoring.collect_authoring_warnings` — the flat soft warning list
/// merged into `validate_scenario` output. Message strings byte-exact with
/// Python (parity-tested against the Python reference).
#[allow(clippy::too_many_arguments)]
pub fn collect_authoring_warnings(
    persona: &Map<String, Json>,
    tags: &[String],
    script_steps: &[Json],
    script_verify: Option<&Json>,
    asserts: Option<&Json>,
    execute: Option<&crate::scenario::ExecuteSpec>,
    sim: &crate::scenario::SimulatorSpec,
    behavior_spec: Option<&Map<String, Json>>,
) -> Vec<String> {
    let mut out: Vec<String> = Vec::new();
    let mut warn = |msg: String| out.push(msg);

    let goals = persona_goals(persona);
    if goals.is_empty() {
        warn(
            "Persona.goals is empty — Hamming: caller needs a job-to-be-done (underspecified personas pass on different agent workflows).".to_string(),
        );
    }

    let brief = as_str(persona.get("brief").unwrap_or(&Json::Null))
        .trim()
        .to_string();
    let situation = as_str(persona.get("situation").unwrap_or(&Json::Null))
        .trim()
        .to_string();
    if brief.is_empty() && situation.is_empty() {
        warn(
            "Persona.brief and Persona.situation are empty — add who is calling and why (dialogue mode prefers situation + outcome).".to_string(),
        );
    }

    let fs = first_speaker(execute, sim);
    if fs == "agent" && script_steps.is_empty() && !silent_mode(persona) {
        warn(
            "Dialogue with first_speaker=agent and no Script: if the agent-under-test also waits for the caller to speak first, both sides stay silent — prefer first_speaker=user, silent_mode, or a Script open cue.".to_string(),
        );
    }

    let tags_norm = scenario_tags(tags);
    let has_risk = tags_norm
        .iter()
        .any(|t| RISK_TAGS.contains(&t.as_str()) || t.starts_with("risk:"));
    if !tags_norm.is_empty() && !has_risk {
        warn(
            "Scenario tags have no risk/lifecycle hint (prefer one of: smoke, draft, blocking, scheduled, exploratory, regression).".to_string(),
        );
    }

    let traits = persona_traits(persona);
    let stress: Vec<&String> = traits
        .iter()
        .filter(|t| STRESS_TRAITS.contains(&t.as_str()))
        .collect();
    let has_interaction = !script_steps.is_empty()
        || behavior_spec.is_some()
        || persona
            .get("speech_conditions")
            .and_then(|v| v.as_object())
            .map(|sc| {
                sc.get("barge_policy").is_some()
                    || sc.get("noise").is_some()
                    || sc.get("ambient").is_some()
                    || sc.get("silence_ms").is_some()
                    || sc.get("silent_mode").is_some()
                    || sc.get("interruption_rate").is_some()
            })
            .unwrap_or(false);
    if !stress.is_empty() && !has_interaction && !silent_mode(persona) {
        // Python `str(list)` renders single-quoted: `['quiet']`.
        let py_list = format!(
            "[{}]",
            stress
                .iter()
                .map(|t| format!("'{}'", t))
                .collect::<Vec<_>>()
                .join(", ")
        );
        warn(format!(
            "Traits {py_list} imply interaction stress but there is no Script/Behavior/speech_conditions step — CI cannot hard-prove interrupt/silence/hangup (prompt-only traits are soft)."
        ));
    }

    let barges: Vec<&Json> = script_steps
        .iter()
        .filter(|s| counts_for_recovery_barge_step(s))
        .collect();
    if !barges.is_empty() && !has_recovery_proof(asserts, script_verify) {
        let ids: Vec<String> = barges
            .iter()
            .take(5)
            .map(|s| as_str(s.get("id").unwrap_or(&Json::Null)))
            .collect();
        warn(format!(
            "Recovery barge step(s) present ({}) but no Assert outcome type=recovery and script_verify.min_agent_finals_after_barge_in is 0 — add recovery assert so CI proves agent re-engages.",
            ids.join(", ")
        ));
    }

    let hangups: Vec<&Json> = script_steps
        .iter()
        .filter(|s| as_str(s.get("action").unwrap_or(&Json::Null)).to_lowercase() == "hang_up")
        .collect();
    if !hangups.is_empty() && !has_ended_by_proof(asserts) {
        warn(
            "Script hang_up present but no Assert outcome type=ended_by — add ended_by to prove which side ended the call.".to_string(),
        );
    }

    let constraints = persona_constraints(persona);
    if !constraints.is_empty() && !has_constraint_proof(asserts) {
        warn(
            "Persona.constraints present but no Assert outcome type=constraint_respected — prompt-only constraints are soft; add constraint_respected for hard CI.".to_string(),
        );
    }

    out
}

/// One structured authoring finding (port of `authoring.AuthoringWarning`).
#[derive(Debug, Clone)]
pub struct AuthoringFinding {
    pub code: String,
    pub severity: String, // "warn" | "info"
    pub message: String,
}

/// Collect structured findings (port of `authoring.collect_authoring_findings`).
/// Includes info-severity findings the flat warnings list omits.
#[allow(clippy::too_many_arguments)]
pub fn collect_authoring_findings(
    persona: &Map<String, Json>,
    tags: &[String],
    script_steps: &[Json],
    script_verify: Option<&Json>,
    asserts: Option<&Json>,
    execute: Option<&crate::scenario::ExecuteSpec>,
    sim: &crate::scenario::SimulatorSpec,
    behavior_spec: Option<&Map<String, Json>>,
) -> Vec<AuthoringFinding> {
    let mut out: Vec<AuthoringFinding> = Vec::new();
    let mut push = |code: &str, severity: &str, msg: String| {
        out.push(AuthoringFinding {
            code: code.to_string(),
            severity: severity.to_string(),
            message: msg,
        });
    };

    let goals = persona_goals(persona);
    if goals.is_empty() {
        push(
            "empty_goals",
            "warn",
            "Persona.goals is empty — Hamming: caller needs a job-to-be-done (underspecified personas pass on different agent workflows).".to_string(),
        );
    }

    let brief = as_str(persona.get("brief").unwrap_or(&Json::Null))
        .trim()
        .to_string();
    let situation = as_str(persona.get("situation").unwrap_or(&Json::Null))
        .trim()
        .to_string();
    let steps = script_steps;
    if brief.is_empty() && situation.is_empty() {
        push(
            "empty_brief",
            "warn",
            "Persona.brief and Persona.situation are empty — add who is calling and why (dialogue mode prefers situation + outcome).".to_string(),
        );
    } else if situation.is_empty() && steps.is_empty() {
        push(
            "dialogue_missing_situation",
            "info",
            "Dialogue scenario (no Script): consider Persona.situation + Persona.outcome so the caller has a world problem and a clear done-state.".to_string(),
        );
    }

    let outcome = as_str(
        persona
            .get("outcome")
            .or_else(|| persona.get("desired_outcome"))
            .unwrap_or(&Json::Null),
    )
    .trim()
    .to_string();
    if !situation.is_empty() && outcome.is_empty() && steps.is_empty() {
        push(
            "situation_without_outcome",
            "info",
            "Persona.situation set without Persona.outcome — add what “done” looks like for PassCriteria/Judge.".to_string(),
        );
    }

    let fs = first_speaker(execute, sim);
    if fs == "agent" && steps.is_empty() && !silent_mode(persona) {
        push(
            "agent_first_no_script",
            "warn",
            "Dialogue with first_speaker=agent and no Script: if the agent-under-test also waits for the caller to speak first, both sides stay silent — prefer first_speaker=user, silent_mode, or a Script open cue.".to_string(),
        );
    }

    let tags_norm = scenario_tags(tags);
    let has_risk = tags_norm
        .iter()
        .any(|t| RISK_TAGS.contains(&t.as_str()) || t.starts_with("risk:"));
    if tags_norm.is_empty() {
        push(
            "no_tags",
            "info",
            "Scenario has no metadata.tags — add a risk/lifecycle tag (smoke, draft, blocking, scheduled, exploratory, regression).".to_string(),
        );
    } else if !has_risk {
        push(
            "no_risk_tag",
            "warn",
            "Scenario tags have no risk/lifecycle hint (prefer one of: smoke, draft, blocking, scheduled, exploratory, regression).".to_string(),
        );
    }

    let traits = persona_traits(persona);
    let stress: Vec<&String> = traits
        .iter()
        .filter(|t| STRESS_TRAITS.contains(&t.as_str()))
        .collect();
    let has_interaction = !steps.is_empty()
        || behavior_spec.is_some()
        || persona
            .get("speech_conditions")
            .and_then(|v| v.as_object())
            .map(|sc| {
                sc.get("barge_policy").is_some()
                    || sc.get("noise").is_some()
                    || sc.get("ambient").is_some()
                    || sc.get("silence_ms").is_some()
                    || sc.get("silent_mode").is_some()
                    || sc.get("interruption_rate").is_some()
            })
            .unwrap_or(false);
    if !stress.is_empty() && !has_interaction && !silent_mode(persona) {
        let py_list = format!(
            "[{}]",
            stress
                .iter()
                .map(|t| format!("'{}'", t))
                .collect::<Vec<_>>()
                .join(", ")
        );
        push(
            "stress_trait_without_interaction",
            "warn",
            format!("Traits {py_list} imply interaction stress but there is no Script/Behavior/speech_conditions step — CI cannot hard-prove interrupt/silence/hangup (prompt-only traits are soft)."),
        );
    }

    let barges: Vec<&Json> = steps
        .iter()
        .filter(|s| counts_for_recovery_barge_step(s))
        .collect();
    if !barges.is_empty() && !has_recovery_proof(asserts, script_verify) {
        let ids: Vec<String> = barges
            .iter()
            .take(5)
            .map(|s| as_str(s.get("id").unwrap_or(&Json::Null)))
            .collect();
        push(
            "barge_without_recovery",
            "warn",
            format!(
                "Recovery barge step(s) present ({}) but no Assert outcome type=recovery and script_verify.min_agent_finals_after_barge_in is 0 — add recovery assert so CI proves agent re-engages.",
                ids.join(", ")
            ),
        );
    }

    let hangups: Vec<&Json> = steps
        .iter()
        .filter(|s| as_str(s.get("action").unwrap_or(&Json::Null)).to_lowercase() == "hang_up")
        .collect();
    if !hangups.is_empty() && !has_ended_by_proof(asserts) {
        push(
            "hang_up_without_ended_by",
            "warn",
            "Script hang_up present but no Assert outcome type=ended_by — add ended_by to prove which side ended the call.".to_string(),
        );
    }

    let constraints = persona_constraints(persona);
    if !constraints.is_empty() && !has_constraint_proof(asserts) {
        push(
            "constraint_without_assert",
            "warn",
            "Persona.constraints present but no Assert outcome type=constraint_respected — prompt-only constraints are soft; add constraint_respected for hard CI.".to_string(),
        );
    }

    let dtmf_steps: Vec<&Json> = steps
        .iter()
        .filter(|s| as_str(s.get("action").unwrap_or(&Json::Null)).to_lowercase() == "dtmf")
        .collect();
    if !dtmf_steps.is_empty() && !tags_norm.iter().any(|t| t == "draft") {
        push(
            "dtmf_untagged_draft",
            "info",
            "Script action=dtmf present — tag scenario draft until the agent under test handles SIP DTMF (sim can send; many agents only parse spoken digits).".to_string(),
        );
    }

    if silent_mode(persona) {
        push(
            "silent_mode_active",
            "info",
            "silent_mode=true: freestyle/nudge/auto barge-noise are suppressed — assert agent reprompt/timeout/ended_by rather than goals_met speech.".to_string(),
        );
    }

    out
}

/// Port of `authoring.authoring_scorecard` — the 6-dimension 0–2 rubric (max 12).
pub fn authoring_scorecard(
    persona: &Map<String, Json>,
    tags: &[String],
    script_steps: &[Json],
    script_verify: Option<&Json>,
    asserts: Option<&Json>,
    behavior_spec: Option<&Map<String, Json>>,
) -> Map<String, Json> {
    let goals = persona_goals(persona);
    let constraints = persona_constraints(persona);
    let barges: Vec<&Json> = script_steps
        .iter()
        .filter(|s| counts_for_recovery_barge_step(s))
        .collect();
    let has_assert = asserts.is_some();
    let tags_norm = scenario_tags(tags);
    let has_risk = tags_norm
        .iter()
        .any(|t| RISK_TAGS.contains(&t.as_str()) || t.starts_with("risk:"));
    let has_behavior = !barges.is_empty() || behavior_spec.is_some() || !script_steps.is_empty();
    let has_interaction_proof = has_recovery_proof(asserts, script_verify)
        || has_ended_by_proof(asserts)
        || has_constraint_proof(asserts);

    let mut dims = Map::new();
    dims.insert("goals".into(), json!(if goals.is_empty() { 0 } else { 2 }));
    dims.insert(
        "constraints".into(),
        json!(if !constraints.is_empty() {
            2
        } else if !goals.is_empty() {
            1
        } else {
            0
        }),
    );
    dims.insert("behavior".into(), json!(if has_behavior { 2 } else { 0 }));
    dims.insert("assertion".into(), json!(0));
    dims.insert(
        "risk_tags".into(),
        json!(if has_risk {
            2
        } else if !tags_norm.is_empty() {
            1
        } else {
            0
        }),
    );
    dims.insert(
        "interaction_proof".into(),
        json!(if has_interaction_proof {
            2
        } else if has_assert {
            1
        } else {
            0
        }),
    );
    if has_assert && !barges.is_empty() && !has_recovery_proof(asserts, script_verify) {
        dims.insert("assertion".into(), json!(1));
    } else if has_assert {
        dims.insert("assertion".into(), json!(2));
    }
    let total: i64 = dims.values().filter_map(|v| v.as_i64()).sum();
    let mut m = Map::new();
    m.insert("dimensions".into(), Json::Object(dims));
    m.insert("total".into(), json!(total));
    m.insert("max".into(), json!(12));
    m
}

/// Port of `authoring.authoring_tier` — score + warn codes → suite tier.
pub fn authoring_tier(scorecard: &Map<String, Json>, findings: &[AuthoringFinding]) -> String {
    let total = scorecard.get("total").and_then(|v| v.as_i64()).unwrap_or(0);
    let max_s = scorecard.get("max").and_then(|v| v.as_i64()).unwrap_or(12);
    let codes: std::collections::HashSet<&str> = findings
        .iter()
        .filter(|f| f.severity == "warn")
        .map(|f| f.code.as_str())
        .collect();
    let critical = [
        "empty_goals",
        "barge_without_recovery",
        "stress_trait_without_interaction",
    ];
    let has_critical = codes.iter().any(|c| critical.contains(c));
    if has_critical || total < std::cmp::max(4, max_s / 3) {
        return "exploratory".to_string();
    }
    if total >= std::cmp::max(8, (max_s * 2) / 3) && !has_critical {
        return "blocking".to_string();
    }
    "scheduled".to_string()
}

/// Full structured authoring payload for validate_scenario (port of
/// `authoring.build_authoring_report`).
#[allow(clippy::too_many_arguments)]
pub fn build_authoring_report(
    persona: &Map<String, Json>,
    tags: &[String],
    script_steps: &[Json],
    script_verify: Option<&Json>,
    asserts: Option<&Json>,
    execute: Option<&crate::scenario::ExecuteSpec>,
    sim: &crate::scenario::SimulatorSpec,
    behavior_spec: Option<&Map<String, Json>>,
) -> Map<String, Json> {
    let findings = collect_authoring_findings(
        persona,
        tags,
        script_steps,
        script_verify,
        asserts,
        execute,
        sim,
        behavior_spec,
    );
    let scorecard = authoring_scorecard(
        persona,
        tags,
        script_steps,
        script_verify,
        asserts,
        behavior_spec,
    );
    let tier = authoring_tier(&scorecard, &findings);
    let warn_findings: Vec<&AuthoringFinding> =
        findings.iter().filter(|f| f.severity == "warn").collect();
    let info_findings: Vec<&AuthoringFinding> =
        findings.iter().filter(|f| f.severity == "info").collect();
    let to_json = |f: &AuthoringFinding| {
        let mut fm = Map::new();
        fm.insert("code".into(), json!(f.code));
        fm.insert("message".into(), json!(f.message));
        fm.insert("severity".into(), json!(f.severity));
        Json::Object(fm)
    };
    let mut m = Map::new();
    m.insert("scorecard".into(), Json::Object(scorecard));
    m.insert("tier".into(), json!(tier));
    m.insert(
        "warnings".into(),
        Json::Array(warn_findings.iter().map(|f| to_json(f)).collect()),
    );
    m.insert(
        "infos".into(),
        Json::Array(info_findings.iter().map(|f| to_json(f)).collect()),
    );
    m.insert(
        "warning_codes".into(),
        json!(warn_findings
            .iter()
            .map(|f| f.code.clone())
            .collect::<Vec<_>>()),
    );
    m.insert(
        "info_codes".into(),
        json!(info_findings
            .iter()
            .map(|f| f.code.clone())
            .collect::<Vec<_>>()),
    );
    m.insert(
        "message".into(),
        json!(format!(
            "authoring tier={tier} score={}/{} warns={} (soft — does not fail valid)",
            m.get("scorecard")
                .and_then(|v| v.as_object())
                .and_then(|s| s.get("total"))
                .and_then(|v| v.as_i64())
                .unwrap_or(0),
            12,
            warn_findings.len()
        )),
    );
    m.insert("soft".into(), json!(true));
    m
}
