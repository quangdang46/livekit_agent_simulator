//! Scenario model — byte-parity port of `scenario.py` + `scenario_from_dict.py`.
//!
//! Contract (plan §5.3 / Appendix D §1):
//! - CALLER_MODES 5-value set; SIP_MODES = all but webrtc_sim.
//! - `persona.brief` is required; `id` or `metadata.id` required.
//! - caller.mode must be in CALLER_MODES.
//! - dispatch.metadata must be valid JSON string.
//! - first_speaker must be agent|user (simulator AND execute paths).
//! - run_spec = Execute overrides Simulator per-field.

use std::path::PathBuf;

use serde_json::{Map, Value as Json};

use crate::errors::ScenarioError;

pub const API_VERSION: &str = "agent-sim/v1";

pub const KNOWN_KINDS: [&str; 12] = [
    "Persona",
    "Context",
    "Simulator",
    "Execute",
    "Dispatch",
    "PassCriteria",
    "Script",
    "Behavior",
    "Plugins",
    "Assert",
    "Caller",
    "Telephony",
];

pub const CALLER_MODES: [&str; 5] = [
    "webrtc_sim",
    "inbound_sip",
    "outbound_human_pickup",
    "outbound_sim_callee",
    "agent_dials",
];

pub const SIP_MODES: [&str; 4] = [
    "inbound_sip",
    "outbound_human_pickup",
    "outbound_sim_callee",
    "agent_dials",
];

pub const HANDSET_ISOLATION_MODES: [&str; 4] =
    ["mute_uplink", "mute_and_unsubscribe", "none", "remove"];

pub const HOLD_TIMEOUT_MIN_S: f64 = 5.0;
pub const HOLD_TIMEOUT_MAX_S: f64 = 300.0;

// ---------------------------------------------------------------------------
// EffectiveTelephony — resolved telephony after scenario > config > built-in
// ---------------------------------------------------------------------------

/// Resolved telephony after scenario > config > built-in merge.
/// Port of scenario.py::EffectiveTelephony (lines 141-153).
#[derive(Debug, Clone, PartialEq)]
pub struct EffectiveTelephony {
    pub outbound_trunk_id: Option<String>,
    pub inbound_trunk_id: Option<String>,
    pub call_to: Option<String>,
    pub dial_in: Option<String>,
    pub prepare_ms: i64,
    pub wait_until_answered: bool,
    pub krisp_enabled: bool,
    pub agent_room: Option<String>,
    pub agent_room_name_template: Option<String>,
    pub handset_isolation: String,
}

/// pick_str: scenario value wins over config value (non-empty string preferred).
/// Port of effective_telephony's inner pick_str closure.
fn pick_str(sc_val: Option<&str>, cfg_val: Option<&str>) -> Option<String> {
    if let Some(v) = sc_val {
        let t = v.trim();
        if !t.is_empty() {
            return Some(t.to_string());
        }
    }
    if let Some(v) = cfg_val {
        let t = v.trim();
        if !t.is_empty() {
            return Some(t.to_string());
        }
    }
    None
}

/// Merge scenario Telephony over config.telephony (scenario wins when set).
/// Port of scenario.py::effective_telephony (lines 556-637).
pub fn effective_telephony(
    scenario: &Scenario,
    tel_cfg: Option<&crate::config::TelephonyConfig>,
) -> EffectiveTelephony {
    let sc = scenario.telephony.as_ref();
    let mode = scenario.effective_caller_mode();

    let outbound = pick_str(
        sc.and_then(|s| s.sip_trunk_id.as_deref()),
        tel_cfg.and_then(|c| c.outbound_trunk_id.as_deref()),
    );

    // inbound_trunk_id is config-only (scenario never supplies it).
    let inbound = pick_str(None, tel_cfg.and_then(|c| c.inbound_trunk_id.as_deref()));

    // sim_inbound_number is only a call_to fallback for Gemini-as-callee hairpin.
    let call_to = if mode == "outbound_sim_callee" {
        pick_str(
            sc.and_then(|s| s.call_to.as_deref()),
            tel_cfg.and_then(|c| c.sim_inbound_number.as_deref()),
        )
    } else {
        pick_str(sc.and_then(|s| s.call_to.as_deref()), None)
    };

    let dial_in = pick_str(
        sc.and_then(|s| s.dial_in.as_deref()),
        tel_cfg.and_then(|c| c.dial_in.as_deref()),
    );

    // prepare_ms: builtin 3000 → config → scenario
    let mut prepare_ms: i64 = 3000;
    if let Some(c) = tel_cfg {
        prepare_ms = c.prepare_ms;
    }
    if let Some(s) = sc.and_then(|s| s.prepare_ms) {
        prepare_ms = s;
    }

    // wait_until_answered: builtin true → config → scenario
    let mut wait_answered = true;
    if let Some(c) = tel_cfg {
        wait_answered = c.wait_until_answered;
    }
    if let Some(s) = sc.and_then(|s| s.wait_until_answered) {
        wait_answered = s;
    }

    // krisp_enabled: builtin false → config → scenario
    let mut krisp = false;
    if let Some(c) = tel_cfg {
        krisp = c.krisp_enabled;
    }
    if let Some(s) = sc.and_then(|s| s.krisp_enabled) {
        krisp = s;
    }

    // handset_isolation: builtin "mute_and_unsubscribe" → config → scenario
    let mut handset_isolation = "mute_and_unsubscribe".to_string();
    if let Some(c) = tel_cfg {
        let v = c.handset_isolation.trim().to_lowercase();
        if !v.is_empty() {
            handset_isolation = v;
        }
    }
    if let Some(s) = sc.and_then(|s| s.handset_isolation.as_deref()) {
        let v = s.trim().to_lowercase();
        if !v.is_empty() {
            handset_isolation = v;
        }
    }
    if !HANDSET_ISOLATION_MODES.contains(&handset_isolation.as_str()) {
        handset_isolation = "mute_and_unsubscribe".to_string();
    }

    let agent_room = pick_str(
        sc.and_then(|s| s.agent_room.as_deref()),
        tel_cfg.and_then(|c| c.agent_room.as_deref()),
    );
    let agent_room_tmpl = pick_str(
        sc.and_then(|s| s.agent_room_name_template.as_deref()),
        tel_cfg.and_then(|c| c.agent_room_name_template.as_deref()),
    );

    EffectiveTelephony {
        outbound_trunk_id: outbound,
        inbound_trunk_id: inbound,
        call_to,
        dial_in,
        prepare_ms,
        wait_until_answered: wait_answered,
        krisp_enabled: krisp,
        agent_room,
        agent_room_name_template: agent_room_tmpl,
        handset_isolation,
    }
}

/// Fail-fast if SIP mode is missing required trunk/number after merge.
/// Port of scenario.py::validate_telephony_for_mode (lines 640-673).
pub fn validate_telephony_for_mode(
    scenario: &Scenario,
    tel_cfg: Option<&crate::config::TelephonyConfig>,
) -> Result<(), ScenarioError> {
    let mode = scenario.effective_caller_mode();
    if !SIP_MODES.contains(&mode) {
        return Ok(());
    }
    let tel = effective_telephony(scenario, tel_cfg);
    if matches!(
        mode,
        "outbound_human_pickup" | "outbound_sim_callee" | "inbound_sip"
    ) && tel.outbound_trunk_id.is_none()
    {
        return Err(ScenarioError(format!(
            "Scenario `{}` mode={} requires telephony.outbound_trunk_id \
             in config or Telephony.sip_trunk_id in the scenario.",
            scenario.id, mode
        )));
    }
    if mode == "outbound_human_pickup" && tel.call_to.is_none() {
        return Err(ScenarioError(format!(
            "Scenario `{}` mode=outbound_human_pickup requires Telephony.call_to \
             (human/PSTN number that will answer). \
             For Gemini-as-callee hairpin use mode=outbound_sim_callee + sim_inbound_number.",
            scenario.id
        )));
    }
    if mode == "outbound_sim_callee" && tel.call_to.is_none() {
        return Err(ScenarioError(format!(
            "Scenario `{}` mode=outbound_sim_callee requires Telephony.call_to \
             or config telephony.sim_inbound_number (sim DID Gemini answers via Cloud hairpin). \
             Do not put a real handset PSTN here — that is mode=outbound_human_pickup. \
             The DID must dispatch into the sim-room where Gemini already sits.",
            scenario.id
        )));
    }
    if mode == "inbound_sip" && tel.dial_in.is_none() {
        return Err(ScenarioError(format!(
            "Scenario `{}` mode=inbound_sip requires Telephony.dial_in \
             or config telephony.dial_in (agent-side inbound DID).",
            scenario.id
        )));
    }
    Ok(())
}

// ---------------------------------------------------------------------------
// Spec dataclasses
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq)]
pub struct SimulatorSpec {
    pub max_turns: i64,
    pub timeout_s: i64,
    pub first_speaker: String,
}

#[derive(Debug, Clone, PartialEq)]
pub struct ExecuteSpec {
    pub max_turns: Option<i64>,
    pub timeout_s: Option<i64>,
    pub first_speaker: Option<String>,
    pub hold_music_timeout_s: Option<f64>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct DispatchSpec {
    pub metadata: Option<String>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct CallerSpec {
    pub mode: String,
}

#[derive(Debug, Clone, PartialEq)]
pub struct TelephonySpec {
    pub call_to: Option<String>,
    pub dial_in: Option<String>,
    pub sip_trunk_id: Option<String>,
    pub prepare_ms: Option<i64>,
    pub wait_until_answered: Option<bool>,
    pub krisp_enabled: Option<bool>,
    pub agent_room: Option<String>,
    pub agent_room_name_template: Option<String>,
    pub handset_isolation: Option<String>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Scenario {
    pub id: String,
    pub path: PathBuf,
    pub locale: String,
    pub tags: Vec<String>,
    pub persona: Map<String, Json>,
    pub context: Map<String, Json>,
    pub simulator: SimulatorSpec,
    pub execute: Option<ExecuteSpec>,
    pub dispatch: Option<DispatchSpec>,
    pub caller: Option<CallerSpec>,
    pub telephony: Option<TelephonySpec>,
    pub pass_criteria: Vec<String>,
    pub pass_judges: Vec<Map<String, Json>>,
    pub pass_criteria_mode: String,
    pub script_steps: Vec<Json>,
    pub script_verify: Option<Json>,
    pub plugin_modules: Vec<String>,
    pub asserts: Option<Json>,
    pub behavior_spec: Option<Map<String, Json>>,
    /// Caller policy override from the optimizer (None = use builtin DefaultCallerPolicy).
    /// Port of Python Scenario.caller_policy: Any = None.
    pub caller_policy: Option<crate::caller_policy::CallerPolicyContext>,
}

impl Scenario {
    pub fn effective_caller_mode(&self) -> &str {
        self.caller
            .as_ref()
            .map(|c| c.mode.as_str())
            .unwrap_or("webrtc_sim")
    }

    /// Effective run params: Execute overrides Simulator per-field.
    pub fn run_spec(&self) -> SimulatorSpec {
        match &self.execute {
            None => self.simulator.clone(),
            Some(ex) => SimulatorSpec {
                max_turns: ex.max_turns.unwrap_or(self.simulator.max_turns),
                timeout_s: ex.timeout_s.unwrap_or(self.simulator.timeout_s),
                first_speaker: ex
                    .first_speaker
                    .clone()
                    .unwrap_or_else(|| self.simulator.first_speaker.clone()),
            },
        }
    }

    /// Effective locale: Persona.language || Persona.locale (stripped) ||
    /// metadata.locale || "en-US".
    pub fn effective_locale(&self) -> String {
        if let Some(lang) = self.persona.get("language").and_then(|v| v.as_str()) {
            if !lang.trim().is_empty() {
                return lang.to_string();
            }
        }
        if let Some(loc) = self.persona.get("locale").and_then(|v| v.as_str()) {
            let trimmed = loc.trim();
            if !trimmed.is_empty() {
                return trimmed.to_string();
            }
        }
        if !self.locale.is_empty() {
            return self.locale.clone();
        }
        "en-US".to_string()
    }
}

/// `_parse_hold_timeout`: clamp [5.0, 300.0] inclusive; `:g` formatting in error.
pub fn parse_hold_timeout(raw: Option<f64>, where_: &str) -> Result<Option<f64>, String> {
    let Some(value) = raw else {
        return Ok(None);
    };
    if !(HOLD_TIMEOUT_MIN_S..=HOLD_TIMEOUT_MAX_S).contains(&value) {
        // Python uses `:g` formatting: 5, 300, not 5.0/300.0.
        let v = format_float_g(value);
        return Err(format!(
            "{where_}: hold_music_timeout_s must be between {HOLD_TIMEOUT_MIN_S} and {HOLD_TIMEOUT_MAX_S} seconds (got {v})"
        ));
    }
    Ok(Some(value))
}

/// Mimic Python's `:g` float formatting (trailing zeros dropped; int-like → no `.0`).
fn format_float_g(v: f64) -> String {
    if v == v.trunc() && v.abs() < 1e15 {
        format!("{}", v as i64)
    } else {
        format!("{}", v)
    }
}

// ---------------------------------------------------------------------------
// scenario_from_dict (the validation core; scenario_yaml delegates to it)
// ---------------------------------------------------------------------------

fn as_str(v: &Json) -> String {
    match v {
        Json::String(s) => s.clone(),
        Json::Number(n) => n.to_string(),
        Json::Bool(b) => b.to_string(),
        Json::Null => String::new(),
        other => other.to_string(),
    }
}

fn opt_str(v: &Json) -> Option<String> {
    let s = as_str(v);
    let t = s.trim();
    if t.is_empty() {
        None
    } else {
        Some(t.to_string())
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

/// Build a Scenario from a dict (same shape as export_scenario / YAML).
pub fn scenario_from_dict(
    data: &Map<String, Json>,
    path: Option<PathBuf>,
    path_label: &str,
) -> Result<Scenario, ScenarioError> {
    let metadata = data.get("metadata").and_then(|m| m.as_object()).cloned();
    let scenario_id = data.get("id").and_then(opt_str).or_else(|| {
        metadata
            .as_ref()
            .and_then(|m| m.get("id"))
            .and_then(opt_str)
    });
    let Some(scenario_id) = scenario_id else {
        return Err(ScenarioError(format!(
            "{path_label}: id or metadata.id is required"
        )));
    };

    let sim_raw = data.get("simulator").and_then(|s| s.as_object()).cloned();
    let simulator = SimulatorSpec {
        max_turns: sim_raw
            .as_ref()
            .and_then(|m| m.get("max_turns"))
            .and_then(as_i64)
            .unwrap_or(6),
        timeout_s: sim_raw
            .as_ref()
            .and_then(|m| m.get("timeout_s"))
            .and_then(as_i64)
            .unwrap_or(120),
        first_speaker: sim_raw
            .as_ref()
            .and_then(|m| m.get("first_speaker"))
            .map(as_str)
            .unwrap_or_else(|| "agent".to_string()),
    };

    // execute (or `run` alias)
    let execute = data
        .get("execute")
        .and_then(|v| v.as_object())
        .or_else(|| {
            if execute_is_none(data) {
                data.get("run").and_then(|v| v.as_object())
            } else {
                None
            }
        })
        .map(|ex| parse_execute(ex, &format!("{path_label}: execute")))
        .transpose()?;

    // dispatch — metadata must be valid JSON (validated here, before move into struct).
    let dispatch = data
        .get("dispatch")
        .and_then(|v| v.as_object())
        .and_then(|m| m.get("metadata"))
        .and_then(opt_str)
        .map(|m| {
            if serde_json::from_str::<Json>(&m).is_err() {
                return Err(ScenarioError(format!(
                    "{path_label}: dispatch.metadata must be valid JSON — parse error"
                )));
            }
            Ok(DispatchSpec { metadata: Some(m) })
        })
        .transpose()?;

    // caller
    let caller = data
        .get("caller")
        .and_then(|v| v.as_object())
        .and_then(|m| m.get("mode"))
        .map(|m| as_str(m).trim().to_lowercase())
        .filter(|m| !m.is_empty())
        .map(|mode| -> Result<CallerSpec, ScenarioError> {
            if !CALLER_MODES.contains(&mode.as_str()) {
                return Err(ScenarioError(format!(
                    "{path_label}: caller.mode must be one of {:?} (got {mode:?})",
                    CALLER_MODES
                )));
            }
            Ok(CallerSpec { mode })
        })
        .transpose()?;

    // telephony
    let telephony = data
        .get("telephony")
        .and_then(|v| v.as_object())
        .map(|tel| {
            let opt = |k: &str| tel.get(k).and_then(opt_str);
            let prepare = tel.get("prepare_ms").and_then(as_i64);
            let wait = tel.get("wait_until_answered").map(py_bool);
            let krisp = tel.get("krisp_enabled").map(py_bool);
            TelephonySpec {
                call_to: opt("call_to"),
                dial_in: opt("dial_in"),
                sip_trunk_id: opt("sip_trunk_id").or_else(|| opt("outbound_trunk_id")),
                prepare_ms: prepare,
                wait_until_answered: wait,
                krisp_enabled: krisp,
                agent_room: opt("agent_room"),
                agent_room_name_template: opt("agent_room_name_template"),
                handset_isolation: opt("handset_isolation"),
            }
        });

    // script (steps + verify). Steps are normalized through the typed parser so
    // exports carry the full Python field set with defaults (P5 export parity);
    // verify stays raw JSON (typed parse happens on demand).
    let mut script_steps = Vec::new();
    let mut script_verify = None;
    if let Some(script) = data.get("script").and_then(|v| v.as_object()) {
        if script.get("steps").and_then(|v| v.as_array()).is_some() {
            match crate::script::parse::parse_script_steps(script, path_label) {
                Ok(typed) => {
                    script_steps = typed
                        .iter()
                        .map(|s| serde_json::to_value(s).unwrap_or(Json::Null))
                        .collect();
                }
                Err(e) => {
                    return Err(ScenarioError(e));
                }
            }
        }
        script_verify = script.get("verify").cloned();
    }

    let persona = data
        .get("persona")
        .and_then(|v| v.as_object())
        .cloned()
        .unwrap_or_default();
    if persona
        .get("brief")
        .map(as_str)
        .unwrap_or_default()
        .is_empty()
    {
        return Err(ScenarioError(format!(
            "{path_label}: persona.brief is required"
        )));
    }

    let plugin_modules: Vec<String> = data
        .get("plugin_modules")
        .or_else(|| data.get("plugins"))
        .and_then(|v| v.as_array())
        .map(|a| a.iter().map(as_str).collect())
        .unwrap_or_default();

    let asserts = data
        .get("assert")
        .or_else(|| data.get("asserts"))
        .and_then(|v| v.as_object())
        .map(|m| Json::Object(m.clone()));

    let behavior_spec = data.get("behavior").and_then(|v| v.as_object()).cloned();

    // PassCriteria
    let mut pass_criteria = Vec::new();
    let mut pass_criteria_mode = "all".to_string();
    let mut pass_judges = Vec::new();
    match data.get("pass_criteria") {
        Some(Json::Object(pc)) => {
            let parsed = parse_pass_criteria(pc, path_label)?;
            pass_criteria = parsed.0;
            pass_criteria_mode = parsed.1;
            pass_judges = parsed.2;
        }
        Some(Json::Array(a)) => {
            pass_criteria = a.iter().map(as_str).collect();
        }
        _ => {}
    }
    if let Some(mode) = data.get("pass_criteria_mode").map(as_str) {
        let mode = mode.trim().to_lowercase();
        if !["all", "majority", "any"].contains(&mode.as_str()) {
            return Err(ScenarioError(format!(
                "{path_label}: pass_criteria_mode must be all|majority|any"
            )));
        }
        pass_criteria_mode = mode;
    }
    if let Some(pj) = data.get("pass_judges") {
        if !pj.is_array() {
            return Err(ScenarioError(format!(
                "{path_label}: pass_judges must be an array"
            )));
        }
        pass_judges = pj
            .as_array()
            .unwrap()
            .iter()
            .filter_map(|j| j.as_object())
            .cloned()
            .collect();
    }

    let locale = data
        .get("locale")
        .map(as_str)
        .or_else(|| metadata.as_ref().and_then(|m| m.get("locale")).map(as_str))
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "en-US".to_string());

    let tags: Vec<String> = data
        .get("tags")
        .or_else(|| metadata.as_ref().and_then(|m| m.get("tags")))
        .and_then(|v| v.as_array())
        .map(|a| a.iter().map(as_str).collect())
        .unwrap_or_default();

    let scenario = Scenario {
        id: scenario_id.clone(),
        path: path.unwrap_or_else(|| PathBuf::from(format!("{scenario_id}.yaml"))),
        locale,
        tags,
        persona,
        context: data
            .get("context")
            .and_then(|v| v.as_object())
            .cloned()
            .unwrap_or_default(),
        simulator,
        execute,
        dispatch,
        caller,
        telephony,
        pass_criteria,
        pass_criteria_mode,
        pass_judges,
        script_steps,
        script_verify,
        plugin_modules,
        asserts,
        behavior_spec,
        caller_policy: None,
    };

    // Hamming-style: compile speech_conditions + Behavior into Script (explicit
    // Script wins by id) — mirrors scenario.py/scenario_from_dict.py parse end.
    let scenario = apply_behavior_compile(scenario, path_label)?;

    // Validation (order matters — mirrors scenario_from_dict.py).
    if scenario.simulator.first_speaker != "agent" && scenario.simulator.first_speaker != "user" {
        return Err(ScenarioError(format!(
            "{path_label}: simulator.first_speaker must be agent or user"
        )));
    }
    let run_fs = &scenario.run_spec().first_speaker;
    if run_fs != "agent" && run_fs != "user" {
        return Err(ScenarioError(format!(
            "{path_label}: execute.first_speaker must be agent or user"
        )));
    }

    Ok(scenario)
}

/// Run `behavior_compile.apply_caller_behavior` over the parsed scenario —
/// speech_conditions auto-steps (auto-ambient/auto-barge/auto-silence) and
/// Behavior.spec compile into script_steps, with explicit steps merged by id.
/// Mirrors the Python parse-end call in scenario.py:543.
pub fn apply_behavior_compile(
    mut scenario: Scenario,
    path_label: &str,
) -> Result<Scenario, ScenarioError> {
    use crate::behavior_compile::apply_caller_behavior;
    use crate::script::parse::{parse_script_steps, parse_script_verify};

    // Typed parse of the raw script section (steps + verify) so
    // apply_caller_behavior can merge/compile; the raw section is reconstructed
    // from the typed steps for the compile call.
    let mut typed_steps: Vec<crate::script::ScriptStep> = Vec::new();
    let mut typed_verify: Option<crate::script::ScriptVerifySpec> = None;
    let script_raw = scenario.script_steps.clone();
    if !script_raw.is_empty() {
        // Wrap the raw steps in a {steps: [...]} spec the parser expects.
        let mut spec = Map::new();
        spec.insert("steps".into(), Json::Array(script_raw));
        typed_steps = parse_script_steps(&spec, path_label).map_err(ScenarioError)?;
    }
    if let Some(sv) = &scenario.script_verify {
        typed_verify = parse_script_verify(sv).map_err(ScenarioError)?;
    }

    let (compiled_steps, compiled_verify) = apply_caller_behavior(
        &scenario.persona,
        scenario.behavior_spec.as_ref(),
        &typed_steps,
        typed_verify.as_ref(),
        path_label,
    )
    .map_err(ScenarioError)?;

    scenario.script_steps = compiled_steps
        .iter()
        .map(|s| serde_json::to_value(s).unwrap_or(Json::Null))
        .collect();
    scenario.script_verify = compiled_verify.map(|v| serde_json::to_value(v).unwrap_or(Json::Null));
    Ok(scenario)
}

fn execute_is_none(data: &Map<String, Json>) -> bool {
    !matches!(data.get("execute"), Some(Json::Object(_)))
}

fn parse_execute(ex: &Map<String, Json>, where_: &str) -> Result<ExecuteSpec, ScenarioError> {
    let hold = parse_hold_timeout(ex.get("hold_music_timeout_s").and_then(as_f64), where_)
        .map_err(ScenarioError)?;
    Ok(ExecuteSpec {
        max_turns: ex.get("max_turns").and_then(as_i64),
        timeout_s: ex.get("timeout_s").and_then(as_i64),
        first_speaker: ex.get("first_speaker").and_then(opt_str),
        hold_music_timeout_s: hold,
    })
}

/// Python `bool()`: any non-empty string (even "false") is truthy.
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

/// Parsed pass_criteria: (criteria, mode, judges).
pub type PassCriteriaParsed = (Vec<String>, String, Vec<Map<String, Json>>);

/// `parse_pass_criteria(spec, where)` → (criteria, mode, judges).
/// dict {criteria, mode, judges}; non-dict handled by caller.
fn parse_pass_criteria(
    pc: &Map<String, Json>,
    where_: &str,
) -> Result<PassCriteriaParsed, ScenarioError> {
    let mut criteria = Vec::new();
    if let Some(c) = pc.get("criteria") {
        match c {
            Json::Array(a) => criteria = a.iter().map(as_str).collect(),
            Json::String(s) => criteria = vec![s.clone()],
            _ => {}
        }
    }
    let mode = pc
        .get("mode")
        .map(as_str)
        .unwrap_or_else(|| "all".to_string())
        .trim()
        .to_lowercase();
    if !["all", "majority", "any"].contains(&mode.as_str()) {
        return Err(ScenarioError(format!(
            "{where_}: pass_criteria.mode must be all|majority|any (got {mode:?})"
        )));
    }
    let mut judges = Vec::new();
    if let Some(Json::Array(j)) = pc.get("judges") {
        for (ji, j) in j.iter().enumerate() {
            let obj = j.as_object().ok_or_else(|| {
                ScenarioError(format!(
                    "{where_}: PassCriteria.judges[{ji}] must be object"
                ))
            })?;
            let jid = obj
                .get("id")
                .or_else(|| obj.get("name"))
                .and_then(opt_str)
                .unwrap_or_else(|| format!("judge-{ji}"));
            let builtin = obj.get("builtin").and_then(opt_str);
            let mut jc: Vec<String> = Vec::new();
            if let Some(c) = obj.get("criteria") {
                match c {
                    Json::Array(a) => jc = a.iter().map(as_str).collect(),
                    Json::String(s) => jc = vec![s.clone()],
                    _ => {
                        return Err(ScenarioError(format!(
                            "{where_}: PassCriteria.judges[{ji}].criteria must be array"
                        )));
                    }
                }
            }
            if jc.is_empty() && builtin.is_none() {
                return Err(ScenarioError(format!(
                    "{where_}: PassCriteria.judges[{ji}] needs criteria[] and/or builtin"
                )));
            }
            let mut entry = Map::new();
            entry.insert("id".into(), Json::String(jid));
            entry.insert(
                "criteria".into(),
                Json::Array(jc.into_iter().map(Json::String).collect()),
            );
            if let Some(b) = builtin {
                entry.insert("builtin".into(), Json::String(b.to_string()));
            }
            judges.push(entry);
        }
    }
    // Backward compatible: flat criteria auto-flattened when judges present but no flat criteria
    if !judges.is_empty() && criteria.is_empty() {
        let mut flat: Vec<String> = Vec::new();
        for j in &judges {
            if let Some(b) = j.get("builtin").and_then(|v| v.as_str()) {
                let jid = j.get("id").and_then(|v| v.as_str()).unwrap_or("");
                flat.push(format!("[{jid}] builtin:{b}"));
            }
            if let Some(arr) = j.get("criteria").and_then(|v| v.as_array()) {
                let jid = j.get("id").and_then(|v| v.as_str()).unwrap_or("");
                for c in arr {
                    if let Some(s) = c.as_str() {
                        flat.push(format!("[{jid}] {s}"));
                    }
                }
            }
        }
        criteria = flat;
    }
    Ok((criteria, mode, judges))
}
