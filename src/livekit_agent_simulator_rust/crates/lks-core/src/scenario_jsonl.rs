//! Legacy JSONL scenario parser (agent-sim/v1) — byte-parity port of
//! `scenario.py::parse_scenario` (the JSONL branch).
//!
//! Contract (scenario.py + plan §5.3 / Appendix D §1):
//! - Line-based; full-line `//` (after strip) are comments; inline `//` is a JSON error.
//! - `_`-prefixed keys stripped from every record.
//! - Line 1 must be kind=Scenario + apiVersion == "agent-sim/v1" (BYTE-EXACT errors).
//! - Unknown kind → 12-kind list sorted.
//! - Duplicate kinds last-wins except Plugins EXTENDS / Script REPLACES.
//! - Validation at end mirrors scenario_from_dict but with JSONL-specific messages
//!   (e.g. `Persona.spec.brief is required — the simulator needs a caller brief`).

use std::path::PathBuf;

use serde_json::{Map, Value as Json};

use crate::errors::ScenarioError;
use crate::scenario::{
    parse_hold_timeout, CallerSpec, DispatchSpec, ExecuteSpec, Scenario, SimulatorSpec,
    TelephonySpec, API_VERSION, CALLER_MODES, HANDSET_ISOLATION_MODES, KNOWN_KINDS,
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

fn opt_str(v: &Json) -> Option<String> {
    let s = as_str(v);
    let t = s.trim();
    if t.is_empty() {
        None
    } else {
        Some(t.to_string())
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

/// Drop keys starting with `_` (scaffold notes), mirroring strip_extension_keys.
fn strip_extension_keys(obj: Map<String, Json>) -> Map<String, Json> {
    obj.into_iter()
        .filter(|(k, _)| !k.starts_with('_'))
        .collect()
}

/// Parse a legacy .jsonl scenario file → Scenario.
pub fn parse_scenario_jsonl(path: &PathBuf) -> Result<Scenario, ScenarioError> {
    if !path.exists() {
        return Err(ScenarioError(format!(
            "Scenario file not found: {}",
            path.display()
        )));
    }
    let text = std::fs::read_to_string(path)
        .map_err(|e| ScenarioError(format!("{}: read error — {e}", path.display())))?;

    // records: (line_no, obj) — strip_extension_keys applied at parse.
    let mut records: Vec<(usize, Map<String, Json>)> = Vec::new();
    for (i, raw_line) in text.split('\n').enumerate() {
        let line_no = i + 1;
        let stripped = raw_line.trim();
        if stripped.is_empty() {
            continue;
        }
        if stripped.starts_with("//") {
            continue;
        }
        let obj: Json = serde_json::from_str(stripped).map_err(|e| {
            ScenarioError(format!("{}:{line_no}: invalid JSON — {e}", path.display()))
        })?;
        let obj = match obj {
            Json::Object(m) => m,
            _ => {
                return Err(ScenarioError(format!(
                    "{}:{line_no}: each line must be a JSON object",
                    path.display()
                )))
            }
        };
        records.push((line_no, strip_extension_keys(obj)));
    }

    if records.is_empty() {
        return Err(ScenarioError(format!(
            "{}: empty scenario file",
            path.display()
        )));
    }

    let (header_line, header) = &records[0];
    if header.get("kind").and_then(|k| k.as_str()) != Some("Scenario") {
        return Err(ScenarioError(format!(
            "{}:{header_line}: first line must have kind=Scenario",
            path.display()
        )));
    }
    if header.get("apiVersion").and_then(|v| v.as_str()) != Some(API_VERSION) {
        return Err(ScenarioError(format!(
            "{}:{header_line}: apiVersion must be `{}` (got {:?})",
            path.display(),
            API_VERSION,
            header.get("apiVersion")
        )));
    }
    let metadata = header.get("metadata").and_then(|m| m.as_object()).cloned();
    let scenario_id = metadata
        .as_ref()
        .and_then(|m| m.get("id"))
        .and_then(opt_str);
    let Some(scenario_id) = scenario_id else {
        return Err(ScenarioError(format!(
            "{}:{header_line}: metadata.id is required",
            path.display()
        )));
    };

    let mut scenario = Scenario {
        id: scenario_id.clone(),
        path: path.clone(),
        locale: metadata
            .as_ref()
            .and_then(|m| m.get("locale"))
            .map(as_str)
            .filter(|s| !s.is_empty())
            .unwrap_or_else(|| "en-US".to_string()),
        tags: metadata
            .as_ref()
            .and_then(|m| m.get("tags"))
            .and_then(|v| v.as_array())
            .map(|a| a.iter().map(as_str).collect())
            .unwrap_or_default(),
        persona: Map::new(),
        context: Map::new(),
        simulator: SimulatorSpec {
            max_turns: 6,
            timeout_s: 120,
            first_speaker: "agent".to_string(),
        },
        execute: None,
        dispatch: None,
        caller: None,
        telephony: None,
        pass_criteria: Vec::new(),
        pass_judges: Vec::new(),
        pass_criteria_mode: "all".to_string(),
        script_steps: Vec::new(),
        script_verify: None,
        plugin_modules: Vec::new(),
        asserts: None,
        behavior_spec: None,
        caller_policy: None,
    };

    for (line_no, obj) in &records[1..] {
        let kind = obj.get("kind").and_then(|k| k.as_str());
        let Some(kind) = kind else {
            return Err(ScenarioError(format!(
                "{}:{line_no}: unknown kind {:?} (expected one of {:?})",
                path.display(),
                obj.get("kind"),
                KNOWN_KINDS
            )));
        };
        if !KNOWN_KINDS.contains(&kind) {
            return Err(ScenarioError(format!(
                "{}:{line_no}: unknown kind {:?} (expected one of {:?})",
                path.display(),
                kind,
                KNOWN_KINDS
            )));
        }
        let spec = obj.get("spec").cloned().unwrap_or(Json::Null);
        if !spec.is_object() {
            return Err(ScenarioError(format!(
                "{}:{line_no}: spec must be an object",
                path.display()
            )));
        }
        let spec_obj = spec.as_object().unwrap();
        match kind {
            "Persona" => scenario.persona = spec_obj.clone(),
            "Context" => scenario.context = spec_obj.clone(),
            "Simulator" => {
                scenario.simulator = SimulatorSpec {
                    max_turns: spec_obj.get("max_turns").and_then(as_i64).unwrap_or(6),
                    timeout_s: spec_obj.get("timeout_s").and_then(as_i64).unwrap_or(120),
                    first_speaker: spec_obj
                        .get("first_speaker")
                        .map(as_str)
                        .unwrap_or_else(|| "agent".to_string()),
                };
            }
            "Execute" => {
                let hold = parse_hold_timeout(
                    spec_obj.get("hold_music_timeout_s").and_then(as_f64),
                    "Execute.spec",
                )
                .map_err(|e| ScenarioError(format!("{}:{line_no}: {e}", path.display())))?;
                scenario.execute = Some(ExecuteSpec {
                    max_turns: spec_obj.get("max_turns").and_then(as_i64),
                    timeout_s: spec_obj.get("timeout_s").and_then(as_i64),
                    first_speaker: spec_obj.get("first_speaker").and_then(opt_str),
                    hold_music_timeout_s: hold,
                });
            }
            "Dispatch" => {
                let meta = spec_obj.get("metadata").and_then(opt_str);
                scenario.dispatch = Some(DispatchSpec { metadata: meta });
            }
            "Caller" => {
                let mode = spec_obj
                    .get("mode")
                    .map(as_str)
                    .unwrap_or_else(|| "webrtc_sim".to_string())
                    .trim()
                    .to_lowercase();
                if !CALLER_MODES.contains(&mode.as_str()) {
                    return Err(ScenarioError(format!(
                        "{}:{line_no}: Caller.spec.mode must be one of {:?} (got {mode:?})",
                        path.display(),
                        CALLER_MODES
                    )));
                }
                scenario.caller = Some(CallerSpec { mode });
            }
            "Telephony" => {
                let opt = |k: &str| spec_obj.get(k).and_then(opt_str);
                let handset_iso = opt("handset_isolation");
                if let Some(h) = &handset_iso {
                    if !HANDSET_ISOLATION_MODES.contains(&h.as_str()) {
                        return Err(ScenarioError(format!(
                            "{}:{line_no}: Telephony.spec.handset_isolation must be one of {:?} (got {h:?})",
                            path.display(),
                            HANDSET_ISOLATION_MODES
                        )));
                    }
                }
                scenario.telephony = Some(TelephonySpec {
                    call_to: opt("call_to"),
                    dial_in: opt("dial_in"),
                    sip_trunk_id: opt("sip_trunk_id").or_else(|| opt("outbound_trunk_id")),
                    prepare_ms: spec_obj.get("prepare_ms").and_then(as_i64),
                    wait_until_answered: spec_obj.get("wait_until_answered").map(py_bool),
                    krisp_enabled: spec_obj.get("krisp_enabled").map(py_bool),
                    agent_room: opt("agent_room"),
                    agent_room_name_template: opt("agent_room_name_template"),
                    handset_isolation: handset_iso,
                });
            }
            "PassCriteria" => {
                // Full parse_pass_criteria lives in scenario.rs; inline the
                // criteria/mode/judges extraction here (same as scenario_from_dict).
                let mut criteria = Vec::new();
                if let Some(c) = spec_obj.get("criteria") {
                    match c {
                        Json::Array(a) => criteria = a.iter().map(as_str).collect(),
                        Json::String(s) => criteria = vec![s.clone()],
                        _ => {}
                    }
                }
                let mode = spec_obj
                    .get("mode")
                    .map(as_str)
                    .unwrap_or_else(|| "all".to_string())
                    .trim()
                    .to_lowercase();
                let mut judges = Vec::new();
                if let Some(Json::Array(j)) = spec_obj.get("judges") {
                    judges = j.iter().filter_map(|x| x.as_object()).cloned().collect();
                }
                scenario.pass_criteria = criteria;
                scenario.pass_criteria_mode = mode;
                scenario.pass_judges = judges;
            }
            "Script" => {
                // Full script parse deferred to the script module (P1 script);
                // store raw steps + verify (Script REPLACES, not extends).
                scenario.script_steps = spec_obj
                    .get("steps")
                    .and_then(|v| v.as_array())
                    .cloned()
                    .unwrap_or_default();
                scenario.script_verify = spec_obj.get("verify").cloned();
            }
            "Behavior" => {
                scenario.behavior_spec = Some(spec_obj.clone());
            }
            "Plugins" => {
                let modules = spec_obj
                    .get("modules")
                    .or_else(|| spec_obj.get("load"))
                    .and_then(|v| v.as_array())
                    .ok_or_else(|| {
                        ScenarioError(format!(
                            "{}:{line_no}: Plugins.spec.modules must be an array",
                            path.display()
                        ))
                    })?;
                // Plugins EXTENDS (appends).
                for m in modules {
                    scenario.plugin_modules.push(as_str(m));
                }
            }
            "Assert" => {
                // Full assert parse deferred to asserts module (P4); store raw.
                scenario.asserts = Some(Json::Object(spec_obj.clone()));
            }
            _ => {
                return Err(ScenarioError(format!(
                    "{}:{line_no}: unknown kind {kind:?} (expected one of {:?})",
                    path.display(),
                    KNOWN_KINDS
                )));
            }
        }
    }

    // Final validation (JSONL-specific messages).
    let brief = scenario
        .persona
        .get("brief")
        .map(as_str)
        .unwrap_or_default();
    if brief.is_empty() {
        return Err(ScenarioError(format!(
            "{}: Persona.spec.brief is required — the simulator needs a caller brief",
            path.display()
        )));
    }
    if scenario.simulator.first_speaker != "agent" && scenario.simulator.first_speaker != "user" {
        return Err(ScenarioError(format!(
            "{}: Simulator.spec.first_speaker must be `agent` or `user`",
            path.display()
        )));
    }
    let run_fs = &scenario.run_spec().first_speaker;
    if run_fs != "agent" && run_fs != "user" {
        return Err(ScenarioError(format!(
            "{}: Execute.spec.first_speaker must be `agent` or `user`",
            path.display()
        )));
    }
    if let Some(d) = &scenario.dispatch {
        if let Some(meta) = &d.metadata {
            if serde_json::from_str::<Json>(meta).is_err() {
                return Err(ScenarioError(format!(
                    "{}: Dispatch.spec.metadata must be valid JSON string",
                    path.display()
                )));
            }
        }
    }
    let mode = scenario.effective_caller_mode();
    if !CALLER_MODES.contains(&mode) {
        return Err(ScenarioError(format!(
            "{}: Caller.mode {mode:?} is not supported",
            path.display()
        )));
    }

    Ok(scenario)
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
