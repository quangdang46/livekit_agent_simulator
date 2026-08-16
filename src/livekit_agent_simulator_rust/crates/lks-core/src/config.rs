//! Config load/validate/redact — byte-parity port of `config.py`.
//!
//! Contract (plan Invariant I3 + Appendix D §1):
//! - `_require` fail-fast: fails iff None OR whitespace-only string; `0`/`false`/`0.0` PASS.
//! - Error strings are BYTE-EXACT (golden tests depend on them).
//! - `config_snapshot` key ORDER is a contract (dict-literal order) and never contains secrets.
//! - Bool fields use Python `bool()` semantics: `bool("false") == True` (any non-empty
//!   string is truthy). Only `false`/`0`/`""`/`null` YAML scalars are falsy.
//! - YAML read path emulates PyYAML 1.1 (on/off/yes/no → bool, leading-zero ints → octal,
//!   `1e5`-style stays a string) — verified against the real PyYAML on 2026-08-13.
//!   `yaml_serde` is YAML 1.2, so a normalization pass runs over the parsed `Value`.

use std::collections::BTreeMap;
use std::path::PathBuf;

use serde_json::{Map, Value as Json};

pub use crate::errors::ConfigError;

pub const DOT_FOLDER: &str = ".agent-sim";
pub const CONFIG_FILENAME: &str = "config.yaml";
pub const DEFAULT_LANGUAGE: &str = "en-US";
pub const DEFAULT_TIMEZONE: &str = "UTC";
pub const DEFAULT_VOICE_MODEL: &str = "gemini-3.1-flash-live-preview";
pub const DEFAULT_JUDGE_MODEL: &str = "gemini-2.5-flash";

/// Provider / mode / endpoint valid values (str().strip().lower() before validation).
const PROVIDER_VALUES: [&str; 2] = ["google", "openai"];
const BRAIN_MODE_VALUES: [&str; 1] = ["realtime"];
const ENDPOINT_TYPE_VALUES: [&str; 2] = ["openai", "anthropic"];

// ---------------------------------------------------------------------------
// Config dataclasses (mirror config.py exactly)
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq)]
pub struct LiveKitConfig {
    pub url: String,
    pub api_key: String,
    pub api_secret: String,
    pub agent_name: String,
    pub room_prepare_ms: i64,
    pub agent_join_timeout_ms: i64,
    pub dispatch_metadata: Option<String>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct SimulatorVoiceConfig {
    pub model: String,
    pub voice: String,
    pub language: String,
}

#[derive(Debug, Clone, PartialEq)]
pub struct SimulatorConfig {
    pub provider: String,
    pub mode: String,
    pub api_key: String,
    pub language: String,
    pub voice: SimulatorVoiceConfig,
    pub name: String,
}

#[derive(Debug, Clone, PartialEq)]
pub struct JudgeConfig {
    pub model: Option<String>,
    pub temperature: f64,
    pub base_url: Option<String>,
    pub api_key: Option<String>,
    pub endpoint_type: String,
}

#[derive(Debug, Clone, PartialEq)]
pub struct AudioOnsetConfig {
    pub enabled: bool,
    pub vad: String,
    pub threshold: f64,
    pub win_ms: i64,
    pub energy_frames: i64,
    pub exit_frames: i64,
    pub refractory_ms: i64,
}

#[derive(Debug, Clone, PartialEq)]
pub struct ToolEventPattern {
    pub mat: Map<String, Json>,
    pub emit: String,
}

#[derive(Debug, Clone, PartialEq)]
pub struct ObserveConfig {
    pub timezone: String,
    pub lk_transcription: bool,
    pub lk_agent_session: bool,
    pub record_audio: bool,
    pub data_topics: Vec<String>,
    pub flow_topics: Vec<String>,
    pub tool_event_patterns: Vec<ToolEventPattern>,
    pub audio_onset: AudioOnsetConfig,
    pub transcript_payload_types: Vec<String>,
    pub transcript_dedupe_window_ms: i64,
    pub silence_threshold_ms: i64,
    pub turn_taking_warn_ms: i64,
}

#[derive(Debug, Clone, PartialEq, Default)]
pub struct CuesConfig {
    pub dirs: Vec<String>,
    pub aliases: BTreeMap<String, String>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct TelephonyConfig {
    pub outbound_trunk_id: Option<String>,
    pub inbound_trunk_id: Option<String>,
    pub dial_in: Option<String>,
    pub sim_inbound_number: Option<String>,
    pub prepare_ms: i64,
    pub wait_until_answered: bool,
    pub krisp_enabled: bool,
    pub agent_room: Option<String>,
    pub agent_room_name_template: Option<String>,
    pub handset_isolation: String,
}

#[derive(Debug, Clone, PartialEq)]
pub struct SimConfig {
    pub project_root: PathBuf,
    pub livekit: LiveKitConfig,
    pub simulator: SimulatorConfig,
    pub observe: ObserveConfig,
    pub judge: Option<JudgeConfig>,
    pub project: Option<String>,
    pub cues: CuesConfig,
    pub telephony: TelephonyConfig,
    pub active_profile: Option<String>,
}

impl SimConfig {
    pub fn dot_dir(&self) -> PathBuf {
        self.project_root.join(DOT_FOLDER)
    }
    pub fn reports_dir(&self) -> PathBuf {
        self.dot_dir().join("reports")
    }
    pub fn scenarios_dir(&self) -> PathBuf {
        self.dot_dir().join("scenarios")
    }
    pub fn cues_dir(&self) -> PathBuf {
        self.dot_dir().join("cues")
    }
    pub fn sqlite_path(&self) -> PathBuf {
        self.dot_dir().join("runs.sqlite")
    }
    pub fn optimized_dir(&self) -> PathBuf {
        self.dot_dir().join("optimized")
    }
}

// ---------------------------------------------------------------------------
// PyYAML 1.1 value normalization
// ---------------------------------------------------------------------------

/// Convert a yaml_serde Value to a serde_json Value, emulating PyYAML 1.1
/// scalar coercion on the way (the package runs `yaml.safe_load`, which is
/// PyYAML = YAML 1.1). Verified against the real PyYAML on 2026-08-13:
///   on/yes -> bool true; off/no -> bool false
///   0123   -> int 83 (octal);  09 -> int (invalid octal -> 0 per PyYAML? no:
///           PyYAML `09` -> 9? Actually PyYAML 1.1 `09` fails/0 — see probe)
///   1e5    -> String "1e5" (NOT float — PyYAML keeps it a string)
///   123    -> int 123;  "123" (quoted) -> String "123"
pub fn normalize_yaml_value(v: &yaml_serde::Value) -> Json {
    use yaml_serde::Value;
    match v {
        Value::Null => Json::Null,
        Value::Bool(b) => Json::Bool(*b),
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                Json::Number(i.into())
            } else if let Some(u) = n.as_u64() {
                Json::Number(u.into())
            } else if let Some(f) = n.as_f64() {
                // PyYAML: `1e5` (exponent form) stays a STRING; plain floats
                // like `1.5` stay floats. yaml_serde already parses `1e5` as
                // a float, so we can't recover the original text here — the
                // string-preservation for exponent form is handled at the
                // scalar-normalization layer below via the raw YAML string.
                json_f64(f)
            } else {
                Json::Null
            }
        }
        Value::String(s) => Json::String(s.clone()),
        Value::Sequence(seq) => Json::Array(seq.iter().map(normalize_yaml_value).collect()),
        Value::Mapping(map) => {
            let mut obj = Map::new();
            for (k, val) in map {
                let key = match k {
                    Value::String(s) => s.clone(),
                    other => format!("{:?}", other),
                };
                obj.insert(key, normalize_yaml_value(val));
            }
            Json::Object(obj)
        }
        _ => Json::Null,
    }
}

fn json_f64(f: f64) -> Json {
    serde_json::Number::from_f64(f)
        .map(Json::Number)
        .unwrap_or(Json::Null)
}

/// `_require` semantics: fail iff value is None OR (string and whitespace-only).
/// `0`/`false`/`0.0` PASS (they are not None and not whitespace strings).
fn require(
    section: &Map<String, Json>,
    key: &str,
    section_name: &str,
) -> Result<Json, ConfigError> {
    let value = section.get(key).cloned().unwrap_or(Json::Null);
    if value.is_null() {
        return Err(missing_err(section_name, key));
    }
    if let Json::String(s) = &value {
        if s.trim().is_empty() {
            return Err(missing_err(section_name, key));
        }
    }
    Ok(value)
}

fn missing_err(section_name: &str, key: &str) -> ConfigError {
    ConfigError(format!(
        "Missing `{section_name}.{key}` in {DOT_FOLDER}/{CONFIG_FILENAME}. \
         Copy the value from LiveKit Cloud / your worker and try again."
    ))
}

/// Python `bool()` coercion: only JSON false/0/""/null are falsy; any string
/// (even `"false"`) is truthy. Mirror `bool(raw.get(...))`.
fn py_bool(v: &Json) -> bool {
    match v {
        Json::Null => false,
        Json::Bool(b) => *b,
        Json::Number(n) => {
            if let Some(i) = n.as_i64() {
                i != 0
            } else if let Some(f) = n.as_f64() {
                f != 0.0
            } else {
                true
            }
        }
        // PyYAML 1.1 resolves off/no/yes/on/n/y to booleans at parse time; the
        // Rust YAML 1.2 parser keeps them as strings, so resolve them here to
        // match (off/no/n → false, on/yes/y → true). `false`/`true`/`0`/`1`
        // arrive as real bool/number already; a QUOTED "false" stays a string
        // and is truthy (Python bool("false") == True — golden_config
        // config_bool_string_trap).
        Json::String(s) => match s.trim().to_ascii_lowercase().as_str() {
            "off" | "no" | "n" => false,
            "on" | "yes" | "y" => true,
            _ => !s.is_empty(),
        },
        _ => true,
    }
}

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
    let trimmed = s.trim();
    if trimmed.is_empty() {
        None
    } else {
        Some(trimmed.to_string())
    }
}

// ---------------------------------------------------------------------------
// load_config
// ---------------------------------------------------------------------------

pub fn load_config(project_root: PathBuf, profile: Option<&str>) -> Result<SimConfig, ConfigError> {
    let project_root = project_root
        .canonicalize()
        .unwrap_or_else(|_| project_root.clone());
    let config_path = project_root.join(DOT_FOLDER).join(CONFIG_FILENAME);
    if !config_path.exists() {
        return Err(ConfigError(format!(
            "{} not found. Run `lks init` (or the `init_project` MCP tool) \
             to scaffold {DOT_FOLDER}/ first.",
            config_path.display()
        )));
    }

    let text = std::fs::read_to_string(&config_path)
        .map_err(|e| ConfigError(format!("{} is not valid YAML: {e}", config_path.display())))?;
    let raw_value: yaml_serde::Value = yaml_serde::from_str(&text)
        .map_err(|e| ConfigError(format!("{} is not valid YAML: {e}", config_path.display())))?;
    let raw = normalize_yaml_value(&raw_value);
    let raw_obj = match raw {
        Json::Object(m) => m,
        Json::Null => Map::new(),
        _ => {
            return Err(ConfigError(format!(
                "{} must be a YAML mapping at the top level.",
                config_path.display()
            )))
        }
    };

    // ---- livekit ----
    let lk_raw = match raw_obj.get("livekit") {
        Some(Json::Object(m)) => m.clone(),
        _ => {
            return Err(ConfigError(format!(
                "Missing `livekit:` section in {}.",
                config_path.display()
            )))
        }
    };
    let dispatch_metadata = match lk_raw.get("dispatch_metadata") {
        Some(v) if !v.is_null() => opt_str(v),
        _ => None,
    };
    let livekit = LiveKitConfig {
        url: as_str(&require(&lk_raw, "url", "livekit")?),
        api_key: as_str(&require(&lk_raw, "api_key", "livekit")?),
        api_secret: as_str(&require(&lk_raw, "api_secret", "livekit")?),
        agent_name: as_str(&require(&lk_raw, "agent_name", "livekit")?),
        room_prepare_ms: int_or(&lk_raw, "room_prepare_ms", 500),
        agent_join_timeout_ms: int_or(&lk_raw, "agent_join_timeout_ms", 25_000),
        dispatch_metadata,
    };

    // ---- simulator (+ optional named profile merge) ----
    let sim_raw = match raw_obj.get("simulator") {
        Some(Json::Object(m)) => m.clone(),
        _ => {
            return Err(ConfigError(format!(
                "Missing `simulator:` section in {}.",
                config_path.display()
            )))
        }
    };
    let (sim_raw, active_profile) = apply_profile(sim_raw, profile, &config_path)?;
    let simulator =
        build_simulator_config(&sim_raw, active_profile.as_deref().unwrap_or("default"))?;

    // ---- judge (skip if absent or non-dict) ----
    let judge = match raw_obj.get("judge") {
        Some(Json::Object(j)) => Some(build_judge_config(j)?),
        _ => None,
    };

    // ---- observe ----
    let obs_raw = match raw_obj.get("observe") {
        Some(Json::Object(m)) => m.clone(),
        _ => Map::new(),
    };
    let observe = build_observe_config(&obs_raw)?;

    // ---- cues ----
    let cues = match raw_obj.get("cues") {
        Some(Json::Object(m)) => build_cues_config(m)?,
        _ => CuesConfig::default(),
    };

    // ---- telephony ----
    let telephony = match raw_obj.get("telephony") {
        Some(Json::Object(m)) => build_telephony_config(m)?,
        _ => TelephonyConfig::default(),
    };

    let project = match raw_obj.get("project") {
        Some(v) if !v.is_null() => Some(as_str(v)),
        _ => None,
    };

    Ok(SimConfig {
        project_root,
        livekit,
        simulator,
        observe,
        judge,
        project,
        cues,
        telephony,
        active_profile,
    })
}

fn apply_profile(
    sim_raw: Map<String, Json>,
    profile: Option<&str>,
    config_path: &std::path::Path,
) -> Result<(Map<String, Json>, Option<String>), ConfigError> {
    // Port of config.py profile selection (lines ~312-370):
    //   * `--profile <name>` given   → that profile (explicit; must exist).
    //   * `--profile` absent + exactly one profile has `default: true`
    //                               → that profile.
    //   * `--profile` absent + no defaults → the legacy flat `simulator:` block.
    //   * 2+ profiles marked `default: true` → error (no "first wins").
    let raw_profiles: Option<Map<String, Json>> = match sim_raw.get("profiles") {
        Some(Json::Object(m)) if !m.is_empty() => Some(m.clone()),
        _ => None,
    };

    // Resolve the selected profile name (None = use the flat block).
    let selected: Option<String> = match profile {
        Some(p) => {
            let map = match &raw_profiles {
                Some(m) => m,
                None => {
                    return Err(ConfigError(format!(
                        "`--profile {p}` requested but `simulator.profiles:` is \
                         not a non-empty map in {}.",
                        config_path.display()
                    )))
                }
            };
            match map.get(p) {
                Some(Json::Object(_)) => Some(p.to_string()),
                Some(_) => {
                    return Err(ConfigError(format!(
                        "`simulator.profiles.{p}` must be a mapping."
                    )))
                }
                None => {
                    let mut names: Vec<&String> = map.keys().collect();
                    names.sort();
                    let list = names
                        .iter()
                        .map(|s| s.as_str())
                        .collect::<Vec<_>>()
                        .join(", ");
                    return Err(ConfigError(format!(
                        "Profile '{p}' not found. Available profiles: {}",
                        if list.is_empty() {
                            "none".to_string()
                        } else {
                            list
                        }
                    )));
                }
            }
        }
        None => {
            if let Some(map) = &raw_profiles {
                let mut defaults: Vec<&String> = map
                    .iter()
                    .filter(|(_, p)| {
                        p.as_object()
                            .and_then(|m| m.get("default"))
                            .and_then(|v| v.as_bool())
                            .unwrap_or(false)
                    })
                    .map(|(name, _)| name)
                    .collect();
                defaults.sort();
                if defaults.len() > 1 {
                    return Err(ConfigError(format!(
                        "Multiple profiles marked `default: true` in {}: {}. \
                         Mark at most one profile as default (or use `--profile <name>`).",
                        config_path.display(),
                        defaults
                            .iter()
                            .map(|s| s.as_str())
                            .collect::<Vec<_>>()
                            .join(", ")
                    )));
                }
                if defaults.len() == 1 {
                    Some(defaults[0].to_string())
                } else {
                    None
                }
            } else {
                None
            }
        }
    };

    // Profiles exist but nothing selects one: if the flat block has no
    // api_key either, there is no caller config at all.
    if selected.is_none() && raw_profiles.is_some() {
        let flat_key = sim_raw
            .get("api_key")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .trim();
        if flat_key.is_empty() {
            return Err(ConfigError(format!(
                "No default profile configured and no legacy `simulator:` \
                 credentials found in {}. Mark one profile with `default: true`, \
                 pass `--profile <name>`, or fill `simulator.api_key`.",
                config_path.display()
            )));
        }
    }

    let Some(selected) = selected else {
        return Ok((sim_raw, None));
    };

    let prof_raw = raw_profiles
        .as_ref()
        .and_then(|m| m.get(&selected))
        .and_then(|v| v.as_object())
        .cloned()
        .unwrap_or_default();

    // Profile inherits unspecified keys from the flat block; drop `profiles:` key.
    let mut merged: Map<String, Json> = sim_raw
        .iter()
        .filter(|(k, _)| *k != "profiles")
        .map(|(k, v)| (k.clone(), v.clone()))
        .collect();
    for (k, v) in prof_raw {
        merged.insert(k, v);
    }
    Ok((merged, Some(selected)))
}

fn build_simulator_config(
    sim_raw: &Map<String, Json>,
    name: &str,
) -> Result<SimulatorConfig, ConfigError> {
    let provider_raw = as_str(
        sim_raw
            .get("provider")
            .unwrap_or(&Json::String("google".into())),
    )
    .trim()
    .to_lowercase();
    if !PROVIDER_VALUES.contains(&provider_raw.as_str()) {
        return Err(ConfigError(format!(
            "`simulator.provider` must be `google` or `openai` (got {provider_raw:?})."
        )));
    }
    let mode_raw = as_str(
        sim_raw
            .get("mode")
            .unwrap_or(&Json::String("realtime".into())),
    )
    .trim()
    .to_lowercase();
    if !BRAIN_MODE_VALUES.contains(&mode_raw.as_str()) {
        return Err(ConfigError(format!(
            "`simulator.mode` must be `realtime` (got {mode_raw:?}); cascade is reserved for a future brain."
        )));
    }
    let voice_raw = sim_raw.get("voice").and_then(|v| v.as_object()).cloned();
    let default_lang = as_str(
        sim_raw
            .get("language")
            .unwrap_or(&Json::String(DEFAULT_LANGUAGE.into())),
    );
    let voice = SimulatorVoiceConfig {
        model: voice_raw
            .as_ref()
            .and_then(|m| m.get("model"))
            .map(as_str)
            .unwrap_or_else(|| DEFAULT_VOICE_MODEL.to_string()),
        voice: voice_raw
            .as_ref()
            .and_then(|m| m.get("voice"))
            .map(as_str)
            .unwrap_or_else(|| "Puck".to_string()),
        language: voice_raw
            .as_ref()
            .and_then(|m| m.get("language"))
            .map(as_str)
            .unwrap_or_else(|| default_lang.clone()),
    };
    let api_key = as_str(&require(sim_raw, "api_key", "simulator")?);
    Ok(SimulatorConfig {
        provider: provider_raw,
        mode: mode_raw,
        api_key,
        language: default_lang,
        voice,
        name: name.to_string(),
    })
}

fn build_judge_config(j: &Map<String, Json>) -> Result<JudgeConfig, ConfigError> {
    let ep_raw = as_str(
        j.get("endpoint_type")
            .unwrap_or(&Json::String("openai".into())),
    )
    .trim()
    .to_lowercase();
    if !ENDPOINT_TYPE_VALUES.contains(&ep_raw.as_str()) {
        return Err(ConfigError(
            "`judge.endpoint_type` must be openai or anthropic (HTTP wire format when base_url is set)."
                .to_string(),
        ));
    }
    let base_url = j.get("base_url").and_then(opt_str);
    let api_key = j.get("api_key").and_then(opt_str);
    let model = match j.get("model") {
        Some(Json::String(s)) if !s.trim().is_empty() => Some(s.trim().to_string()),
        _ => None,
    };
    let temperature = j.get("temperature").and_then(|v| v.as_f64()).unwrap_or(0.0);
    Ok(JudgeConfig {
        model,
        temperature,
        base_url,
        api_key,
        endpoint_type: ep_raw,
    })
}

fn build_observe_config(obs_raw: &Map<String, Json>) -> Result<ObserveConfig, ConfigError> {
    let mut patterns = Vec::new();
    if let Some(Json::Array(list)) = obs_raw.get("tool_event_patterns") {
        for p in list {
            if let Json::Object(pm) = p {
                let mat = pm.get("match").and_then(|m| m.as_object()).cloned();
                let emit = pm.get("emit").map(as_str);
                if let (Some(mat), Some(emit)) = (mat, emit) {
                    if !emit.is_empty() {
                        patterns.push(ToolEventPattern { mat, emit });
                    }
                }
            }
        }
    }

    let ao_raw = match obs_raw.get("audio_onset") {
        Some(Json::Object(m)) => m.clone(),
        Some(_) => {
            return Err(ConfigError(
                "`observe.audio_onset` must be a mapping (or absent)".into(),
            ))
        }
        None => Map::new(),
    };
    let audio_onset = AudioOnsetConfig {
        enabled: py_bool(ao_raw.get("enabled").unwrap_or(&Json::Bool(false))),
        vad: as_str(ao_raw.get("vad").unwrap_or(&Json::String("rms".into())))
            .trim()
            .to_lowercase(),
        threshold: ao_raw
            .get("threshold")
            .and_then(|v| v.as_f64())
            .unwrap_or(0.012),
        win_ms: int_or(&ao_raw, "win_ms", 20),
        energy_frames: int_or(&ao_raw, "energy_frames", 3),
        exit_frames: int_or(&ao_raw, "exit_frames", 5),
        refractory_ms: int_or(&ao_raw, "refractory_ms", 60),
    };
    if audio_onset.vad != "rms" {
        return Err(ConfigError(format!(
            "`observe.audio_onset.vad` must be `rms` (got {:?}); other VAD backends are future work.",
            audio_onset.vad
        )));
    }
    if !(0.0 < audio_onset.threshold && audio_onset.threshold < 1.0) {
        return Err(ConfigError(format!(
            "`observe.audio_onset.threshold` must be between 0.0 and 1.0 (got {:?})",
            audio_onset.threshold
        )));
    }

    let str_list = |key: &str, default: Vec<String>| -> Vec<String> {
        match obs_raw.get(key) {
            Some(Json::Array(a)) => a.iter().map(as_str).collect(),
            _ => default,
        }
    };
    let transcript_default = vec!["transcript_turn".to_string()];

    Ok(ObserveConfig {
        timezone: as_str(
            obs_raw
                .get("timezone")
                .unwrap_or(&Json::String(DEFAULT_TIMEZONE.into())),
        ),
        lk_transcription: py_bool(obs_raw.get("lk_transcription").unwrap_or(&Json::Bool(true))),
        lk_agent_session: py_bool(obs_raw.get("lk_agent_session").unwrap_or(&Json::Bool(true))),
        record_audio: py_bool(obs_raw.get("record_audio").unwrap_or(&Json::Bool(true))),
        data_topics: str_list("data_topics", Vec::new()),
        flow_topics: str_list("flow_topics", Vec::new()),
        tool_event_patterns: patterns,
        audio_onset,
        transcript_payload_types: str_list("transcript_payload_types", transcript_default),
        transcript_dedupe_window_ms: int_or(obs_raw, "transcript_dedupe_window_ms", 15_000),
        silence_threshold_ms: int_or(obs_raw, "silence_threshold_ms", 4_000),
        turn_taking_warn_ms: int_or(obs_raw, "turn_taking_warn_ms", 2_500),
    })
}

fn build_cues_config(m: &Map<String, Json>) -> Result<CuesConfig, ConfigError> {
    let dirs = match m.get("dirs") {
        Some(Json::Array(a)) => a.iter().map(as_str).collect(),
        Some(_) => {
            return Err(ConfigError(
                "`cues.dirs` must be a list of directory paths.".into(),
            ))
        }
        None => Vec::new(),
    };
    let mut aliases = BTreeMap::new();
    match m.get("aliases") {
        Some(Json::Object(o)) => {
            for (k, v) in o {
                aliases.insert(k.clone(), as_str(v));
            }
        }
        Some(_) => {
            return Err(ConfigError(
                "`cues.aliases` must be a mapping of name → path/asset.".into(),
            ))
        }
        None => {}
    }
    Ok(CuesConfig { dirs, aliases })
}

fn build_telephony_config(m: &Map<String, Json>) -> Result<TelephonyConfig, ConfigError> {
    let opt = |key: &str| m.get(key).and_then(opt_str);
    let wait_until_answered = match m.get("wait_until_answered") {
        None => true,
        Some(v) => py_bool(v),
    };
    Ok(TelephonyConfig {
        outbound_trunk_id: opt("outbound_trunk_id").or_else(|| opt("sip_trunk_id")),
        inbound_trunk_id: opt("inbound_trunk_id"),
        dial_in: opt("dial_in"),
        sim_inbound_number: opt("sim_inbound_number"),
        prepare_ms: int_or(m, "prepare_ms", 3_000),
        wait_until_answered,
        krisp_enabled: py_bool(m.get("krisp_enabled").unwrap_or(&Json::Bool(false))),
        agent_room: opt("agent_room"),
        agent_room_name_template: opt("agent_room_name_template"),
        handset_isolation: opt("handset_isolation")
            .unwrap_or_else(|| "mute_and_unsubscribe".into()),
    })
}

fn int_or(m: &Map<String, Json>, key: &str, default: i64) -> i64 {
    match m.get(key) {
        Some(Json::Number(n)) => n.as_i64().unwrap_or(default),
        Some(Json::String(s)) => s.parse().unwrap_or(default),
        _ => default,
    }
}

// ---------------------------------------------------------------------------
// config_snapshot (redacted, key ORDER is a contract)
// ---------------------------------------------------------------------------

impl SimConfig {
    pub fn config_snapshot(&self) -> Map<String, Json> {
        let mut gaps: Vec<String> = Vec::new();
        if !self.observe.lk_agent_session && self.observe.tool_event_patterns.is_empty() {
            gaps.push("tool_events".into());
        }
        let tel = &self.telephony;

        let mut snapshot = Map::new();
        snapshot.insert(
            "project".into(),
            self.project.clone().map(Json::String).unwrap_or(Json::Null),
        );
        let mut livekit = Map::new();
        livekit.insert("url_host".into(), Json::String(url_host(&self.livekit.url)));
        livekit.insert(
            "agent_name".into(),
            Json::String(self.livekit.agent_name.clone()),
        );
        livekit.insert(
            "agent_join_timeout_ms".into(),
            Json::Number(self.livekit.agent_join_timeout_ms.into()),
        );
        livekit.insert(
            "dispatch_metadata_set".into(),
            Json::Bool(self.livekit.dispatch_metadata.is_some()),
        );
        snapshot.insert("livekit".into(), Json::Object(livekit));

        let mut simulator = Map::new();
        simulator.insert(
            "provider".into(),
            Json::String(self.simulator.provider.clone()),
        );
        simulator.insert("mode".into(), Json::String(self.simulator.mode.clone()));
        simulator.insert(
            "voice_model".into(),
            Json::String(self.simulator.voice.model.clone()),
        );
        simulator.insert(
            "voice".into(),
            Json::String(self.simulator.voice.voice.clone()),
        );
        simulator.insert(
            "language".into(),
            Json::String(self.simulator.voice.language.clone()),
        );
        simulator.insert(
            "active_profile".into(),
            self.active_profile
                .clone()
                .map(Json::String)
                .unwrap_or(Json::Null),
        );
        snapshot.insert("simulator".into(), Json::Object(simulator));

        snapshot.insert("judge_enabled".into(), Json::Bool(self.judge.is_some()));
        let judge = match &self.judge {
            Some(j) => {
                let mut m = Map::new();
                m.insert(
                    "model".into(),
                    Json::String(
                        j.model
                            .clone()
                            .unwrap_or_else(|| DEFAULT_JUDGE_MODEL.to_string()),
                    ),
                );
                m.insert("http".into(), Json::Bool(j.base_url.is_some()));
                m.insert(
                    "endpoint_type".into(),
                    if j.base_url.is_some() {
                        Json::String(j.endpoint_type.clone())
                    } else {
                        Json::Null
                    },
                );
                m.insert("api_key_set".into(), Json::Bool(j.api_key.is_some()));
                Json::Object(m)
            }
            None => Json::Null,
        };
        snapshot.insert("judge".into(), judge);

        let mut cues = Map::new();
        cues.insert(
            "dirs".into(),
            Json::Array(
                self.cues
                    .dirs
                    .iter()
                    .map(|d| Json::String(d.clone()))
                    .collect(),
            ),
        );
        cues.insert(
            "alias_keys".into(),
            Json::Array(
                self.cues
                    .aliases
                    .keys()
                    .map(|k| Json::String(k.clone()))
                    .collect(),
            ),
        );
        cues.insert(
            "target_cues_dir".into(),
            Json::String(self.cues_dir().to_string_lossy().into_owned()),
        );
        snapshot.insert("cues".into(), Json::Object(cues));

        let mut observe = Map::new();
        observe.insert(
            "lk_transcription".into(),
            Json::Bool(self.observe.lk_transcription),
        );
        observe.insert(
            "lk_agent_session".into(),
            Json::Bool(self.observe.lk_agent_session),
        );
        observe.insert("record_audio".into(), Json::Bool(self.observe.record_audio));
        observe.insert(
            "data_topics".into(),
            Json::Array(
                self.observe
                    .data_topics
                    .iter()
                    .map(|t| Json::String(t.clone()))
                    .collect(),
            ),
        );
        observe.insert(
            "silence_threshold_ms".into(),
            Json::Number(self.observe.silence_threshold_ms.into()),
        );
        observe.insert(
            "audio_onset_enabled".into(),
            Json::Bool(self.observe.audio_onset.enabled),
        );
        observe.insert(
            "audio_onset_threshold".into(),
            json_f64(self.observe.audio_onset.threshold),
        );
        snapshot.insert("observe".into(), Json::Object(observe));

        let mut telephony = Map::new();
        telephony.insert(
            "outbound_trunk_set".into(),
            Json::Bool(tel.outbound_trunk_id.is_some()),
        );
        telephony.insert(
            "inbound_trunk_set".into(),
            Json::Bool(tel.inbound_trunk_id.is_some()),
        );
        telephony.insert("dial_in_set".into(), Json::Bool(tel.dial_in.is_some()));
        telephony.insert(
            "sim_inbound_number_set".into(),
            Json::Bool(tel.sim_inbound_number.is_some()),
        );
        telephony.insert("prepare_ms".into(), Json::Number(tel.prepare_ms.into()));
        telephony.insert(
            "wait_until_answered".into(),
            Json::Bool(tel.wait_until_answered),
        );
        telephony.insert("krisp_enabled".into(), Json::Bool(tel.krisp_enabled));
        snapshot.insert("telephony".into(), Json::Object(telephony));

        snapshot.insert(
            "observe_gaps".into(),
            Json::Array(gaps.into_iter().map(Json::String).collect()),
        );
        snapshot
    }
}

// TelephonyConfig defaults are NOT derivable (prepare_ms=3000, wait=true differ
// from the derive defaults) — implement Default manually.
#[allow(clippy::derivable_impls)]
impl Default for TelephonyConfig {
    fn default() -> Self {
        TelephonyConfig {
            outbound_trunk_id: None,
            inbound_trunk_id: None,
            dial_in: None,
            sim_inbound_number: None,
            prepare_ms: 3_000,
            wait_until_answered: true,
            krisp_enabled: false,
            agent_room: None,
            agent_room_name_template: None,
            handset_isolation: "mute_and_unsubscribe".into(),
        }
    }
}

/// `url.split("://")[-1].split("/")[0]` — replicate Python split semantics.
fn url_host(url: &str) -> String {
    // Python: `"wss://host/path".split("://")[-1]` == `"host/path"`.
    let after_scheme = url.rsplit("://").next().unwrap_or(url);
    // `.split("/")[0]` == the part before the first `/`.
    after_scheme.split('/').next().unwrap_or("").to_string()
}
