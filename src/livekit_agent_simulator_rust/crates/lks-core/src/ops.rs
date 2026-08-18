//! Shared project operations — single surface for MCP + CLI (port of `ops.py`,
//! data-plane subset — P5). Pure logic: config/scenario/report reads, sqlite
//! reads, compare/baseline digests, scenario promotion. No livekit dependency.
//!
//! The execute-family (execute_scenario, execute_scenarios,
//! execute_scenario_dict, optimize_persona, preflight, web) lands with the
//! `lks-livekit` run infrastructure (P2/P3.5) and returns an explicit
//! not-implemented error here until then (fail-loud, AGENTS.md).

use std::path::Path;

use serde_json::{json, Map, Value as Json};

use crate::authoring::{init_project, init_scenario};
use crate::config::load_config;
use crate::errors::ConfigError;
use crate::scenario_ops::{convert_scenario, export_scenario, list_scenarios};

/// Fail-loud stub for ops that need the P2/P3.5 run infrastructure.
pub fn not_implemented(name: &str) -> Map<String, Json> {
    let mut m = Map::new();
    m.insert(
        "error".into(),
        json!(format!(
            "{name} is not available in the Rust build yet (needs the P2 run infrastructure: lks-livekit room/dispatch/callers + run.rs)."
        )),
    );
    m
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

/// ops.guide — package GUIDE.md text (no project_root required). Works from
/// any cwd: repo walk when available (dev), else the embedded copy.
pub fn guide() -> Result<Map<String, Json>, ConfigError> {
    let repo_path = crate::authoring::package_templates_dir().map(|t| t.join("GUIDE.md"));
    let (path, text) = match &repo_path {
        Some(p) if p.is_file() => (
            p.to_string_lossy().into_owned(),
            std::fs::read_to_string(p).map_err(|e| {
                ConfigError(format!("Package guide missing: {} ({e})", p.display()))
            })?,
        ),
        _ => (
            "embedded:templates/GUIDE.md".to_string(),
            crate::authoring::embedded_template("GUIDE.md")
                .ok_or_else(|| ConfigError("Package guide missing (not embedded)".into()))?
                .to_string(),
        ),
    };
    let mut m = Map::new();
    m.insert("path".into(), json!(path));
    m.insert("text".into(), json!(text));
    Ok(m)
}

/// ops.init_project
pub fn op_init_project(project_root: &Path) -> Result<Map<String, Json>, ConfigError> {
    init_project(project_root)
}

/// ops.init_scenario
pub fn op_init_scenario(
    project_root: &Path,
    scenario_id: &str,
    force: bool,
) -> Result<Map<String, Json>, ConfigError> {
    init_scenario(project_root, scenario_id, force)
}

/// ops.convert_scenario
pub fn op_convert_scenario(
    project_root: &Path,
    scenario_id: &str,
    force: bool,
) -> Result<Map<String, Json>, String> {
    let cfg = load_config(project_root.to_path_buf(), None, None).map_err(|e| e.0)?;
    convert_scenario(&cfg.scenarios_dir(), scenario_id, force).map_err(|e| e.0)
}

/// ops.list_scenarios
pub fn op_list_scenarios(project_root: &Path) -> Result<Vec<Map<String, Json>>, ConfigError> {
    let cfg = load_config(project_root.to_path_buf(), None, None)?;
    Ok(list_scenarios(&cfg.scenarios_dir()))
}

/// ops.validate_scenario — resolution + validation warnings (port of
/// `ops.validate_scenario`; plugin loading is a P8 surface).
pub fn op_validate_scenario(
    project_root: &Path,
    scenario_id: &str,
) -> Result<Map<String, Json>, ConfigError> {
    let cfg = load_config(project_root.to_path_buf(), None, None)?;
    let scenarios_dir = cfg.scenarios_dir();
    let s = match crate::scenario_ops::find_scenario(&scenarios_dir, scenario_id) {
        Ok(s) => s,
        Err(e) => {
            let mut candidates: Vec<String> = Vec::new();
            for ext in ["jsonl", "yaml", "yml"] {
                if let Ok(rd) = std::fs::read_dir(&scenarios_dir) {
                    let mut names: Vec<String> = rd
                        .flatten()
                        .filter_map(|entry| {
                            let p = entry.path();
                            if p.extension().and_then(|x| x.to_str()) == Some(ext) {
                                p.file_name().map(|n| n.to_string_lossy().into_owned())
                            } else {
                                None
                            }
                        })
                        .collect();
                    names.sort();
                    candidates.extend(names);
                }
            }
            let mut m = Map::new();
            m.insert("valid".into(), json!(false));
            m.insert("error".into(), json!(e.0));
            m.insert("available".into(), json!(candidates));
            return Ok(m);
        }
    };
    let mut warnings: Vec<String> = Vec::new();
    if s.pass_criteria.is_empty() {
        warnings.push("No PassCriteria — judge will be skipped for this scenario.".to_string());
    }
    let run = s.run_spec();
    if run.max_turns > 20 {
        warnings.push(format!("max_turns={} is unusually high.", run.max_turns));
    }
    if run.first_speaker == "agent" && !s.dispatch.as_ref().is_some_and(|d| d.metadata.is_some()) {
        warnings.push(
            "first_speaker=agent with no Dispatch.metadata — many agents wait for caller audio. \
             Add Execute.first_speaker=user or a project-specific Dispatch.metadata JSON."
                .to_string(),
        );
    }
    if !s.plugin_modules.is_empty() {
        // P8 decision: the Rust build does NOT embed CPython (binary size + CI
        // complexity). Plugins fail loudly instead of silently skipping.
        warnings.push(format!(
            "verify plugins require the Python build (lks): {} — the Rust build does not embed CPython. Remove plugin_modules or use lks.",
            s.plugin_modules.join(", ")
        ));
    }
    // P1.G authoring rubric (port of `authoring.collect_authoring_warnings`).
    warnings.extend(crate::authoring_warnings::collect_authoring_warnings(
        &s.persona,
        &s.tags,
        &s.script_steps,
        s.script_verify.as_ref(),
        s.asserts.as_ref(),
        s.execute.as_ref(),
        &s.simulator,
        s.behavior_spec.as_ref(),
    ));

    // Structured authoring payload (port of `authoring.build_authoring_report`).
    let authoring = crate::authoring_warnings::build_authoring_report(
        &s.persona,
        &s.tags,
        &s.script_steps,
        s.script_verify.as_ref(),
        s.asserts.as_ref(),
        s.execute.as_ref(),
        &s.simulator,
        s.behavior_spec.as_ref(),
    );

    let mut m = Map::new();
    m.insert("valid".into(), json!(true));
    m.insert("id".into(), json!(s.id));
    m.insert("locale".into(), json!(s.locale));
    m.insert("max_turns".into(), json!(run.max_turns));
    m.insert("timeout_s".into(), json!(run.timeout_s));
    m.insert("first_speaker".into(), json!(run.first_speaker));
    m.insert("has_execute".into(), json!(s.execute.is_some()));
    m.insert(
        "has_dispatch".into(),
        json!(s.dispatch.as_ref().is_some_and(|d| d.metadata.is_some())),
    );
    m.insert("pass_criteria".into(), json!(s.pass_criteria));
    m.insert("warnings".into(), json!(warnings));
    m.insert("authoring".into(), Json::Object(authoring));
    Ok(m)
}

/// ops.export_scenario
pub fn op_export_scenario(
    project_root: &Path,
    scenario_id: &str,
) -> Result<Map<String, Json>, ConfigError> {
    let cfg = load_config(project_root.to_path_buf(), None, None)?;
    Ok(export_scenario(&cfg.scenarios_dir(), scenario_id))
}

/// ops.list_plugins — verify plugins are a P8 (pyo3) surface; local modules
/// listed from `.agent-sim/plugins/*.py` (data-plane, no plugin loading).
pub fn op_list_plugins(project_root: &Path) -> Result<Map<String, Json>, ConfigError> {
    let cfg = load_config(project_root.to_path_buf(), None, None)?;
    let local_dir = cfg.dot_dir().join("plugins");
    let mut local_files: Vec<String> = Vec::new();
    if let Ok(rd) = std::fs::read_dir(&local_dir) {
        for entry in rd.flatten() {
            let p = entry.path();
            if p.extension().and_then(|e| e.to_str()) == Some("py") {
                if let Some(stem) = p.file_stem().map(|s| s.to_string_lossy().into_owned()) {
                    if !stem.starts_with('_') {
                        local_files.push(stem);
                    }
                }
            }
        }
    }
    local_files.sort();

    // Static scan of local .py modules for @verify_plugin("name") registrations
    // so `verify_plugins` mirrors what Python's import-time decorator registers
    // (the Rust build does not embed CPython — P8 decision — but the list shape
    // must match). Sorted, deduped.
    let mut verify_plugins: Vec<String> = Vec::new();
    if let Ok(rd) = std::fs::read_dir(&local_dir) {
        for entry in rd.flatten() {
            let p = entry.path();
            if p.extension().and_then(|e| e.to_str()) != Some("py") {
                continue;
            }
            let Ok(text) = std::fs::read_to_string(&p) else {
                continue;
            };
            for line in text.lines() {
                let t = line.trim();
                if t.starts_with("@verify_plugin") {
                    if let Some(open) = t.find('(') {
                        let rest = &t[open + 1..];
                        let name = rest
                            .trim()
                            .trim_start_matches('"')
                            .trim_start_matches('\'')
                            .split(['"', '\'', ','])
                            .next()
                            .unwrap_or("")
                            .trim()
                            .to_string();
                        if !name.is_empty() {
                            verify_plugins.push(name);
                        }
                    }
                }
            }
        }
    }
    verify_plugins.sort();
    verify_plugins.dedup();

    // Python load_info: entrypoints loaded first, then local modules (the
    // module stems, not the registered names). The Rust build does not embed
    // CPython (P8), so entry-point plugins are always absent and local modules
    // are statically scanned — but the shape mirrors Python exactly.
    let mut loaded: Vec<String> = Vec::new();
    loaded.push("entrypoints:lks.plugins".to_string());
    loaded.extend(local_files.iter().map(|f| format!("local:{f}")));

    let mut load = Map::new();
    load.insert("loaded".into(), json!(loaded));
    load.insert("errors".into(), json!([]));
    load.insert("verify_plugins".into(), json!(verify_plugins));

    let mut out = Map::new();
    out.insert("verify_plugins".into(), json!(verify_plugins));
    out.insert("local_modules".into(), json!(local_files));
    out.insert("load".into(), Json::Object(load));
    out.insert("entry_point_group".into(), json!("lks.plugins"));
    Ok(out)
}

/// One builtin cue entry: (id, file, description, kind, interrupt_class, locale, text).
type BuiltinCueEntry = (
    &'static str,
    &'static str,
    &'static str,
    Option<&'static str>,
    Option<&'static str>,
    Option<&'static str>,
    Option<&'static str>,
);

/// Builtin cue metadata (port of `audio/cue_catalog.py` BUILTIN_CUES).
const BUILTIN_CUES: [BuiltinCueEntry; 35] = [
    (
        "ambiguous",
        "ambiguous_ja.wav",
        "Legacy JA ambiguous / edge false-interrupt sample.",
        Some("legacy"),
        Some("noise"),
        Some("ja-JP"),
        None,
    ),
    (
        "ambiguous_ja",
        "ambiguous_ja.wav",
        "Legacy JA ambiguous / edge false-interrupt sample.",
        Some("legacy"),
        Some("noise"),
        Some("ja-JP"),
        None,
    ),
    (
        "backchannel",
        "backchannel_ja.wav",
        "Legacy JA backchannel sample.",
        Some("legacy"),
        Some("backchannel"),
        Some("ja-JP"),
        None,
    ),
    (
        "backchannel_ja",
        "backchannel_ja.wav",
        "Legacy JA backchannel sample.",
        Some("legacy"),
        Some("backchannel"),
        Some("ja-JP"),
        None,
    ),
    (
        "interrupt",
        "real_interrupt_ja.wav",
        "Legacy JA real-interrupt speech sample.",
        Some("legacy"),
        Some("correction"),
        Some("ja-JP"),
        None,
    ),
    (
        "noise.ambient",
        "ambient_noise_bed.wav",
        "Soft ambient noise bed (false interrupt / background).",
        Some("noise"),
        Some("noise"),
        None,
        None,
    ),
    (
        "noise.blip",
        "loud_interrupt_blip.wav",
        "Very short cut-in blip before/with a barge.",
        Some("noise"),
        Some("noise"),
        None,
        None,
    ),
    (
        "noise.interrupt",
        "loud_interrupt_blip.wav",
        "Alias of noise.blip — short cut-in blip.",
        Some("noise"),
        Some("noise"),
        None,
        None,
    ),
    (
        "noise.loud",
        "loud_noise_burst.wav",
        "Short loud noise burst (false interrupt).",
        Some("noise"),
        Some("noise"),
        None,
        None,
    ),
    (
        "real_interrupt_ja",
        "real_interrupt_ja.wav",
        "Legacy JA real-interrupt speech sample.",
        Some("legacy"),
        Some("correction"),
        Some("ja-JP"),
        None,
    ),
    (
        "voice.backchannel",
        "backchannel_uhhuh_en.wav",
        "EN backchannel sustain (uh-huh ×5, ~4s).",
        Some("voice"),
        Some("backchannel"),
        Some("en-US"),
        Some("uh-huh"),
    ),
    (
        "voice.backchannel_en",
        "backchannel_uhhuh_en.wav",
        "Alias of voice.backchannel — EN uh-huh sustain.",
        Some("voice"),
        Some("backchannel"),
        Some("en-US"),
        Some("uh-huh"),
    ),
    (
        "voice.backchannel_ja",
        "backchannel_ja.wav",
        "JA backchannel (same file as legacy backchannel_ja).",
        Some("legacy"),
        Some("backchannel"),
        Some("ja-JP"),
        None,
    ),
    (
        "voice.backchannel_vi",
        "backchannel_vi.wav",
        "VI backchannel sustain.",
        Some("voice"),
        Some("backchannel"),
        Some("vi-VN"),
        None,
    ),
    (
        "voice.backchannel_yeah",
        "backchannel_yeah_en.wav",
        "EN backchannel: Yeah / Okay / Mhm.",
        Some("voice"),
        Some("backchannel"),
        Some("en-US"),
        Some("Yeah. Okay. Mhm."),
    ),
    (
        "voice.barge_correction",
        "barge_correction_en.wav",
        "Alias of voice.correction.",
        Some("voice"),
        Some("correction"),
        Some("en-US"),
        Some("No wait - I meant next Friday."),
    ),
    (
        "voice.barge_escalate",
        "barge_escalate_en.wav",
        "Alias of voice.escalate.",
        Some("voice"),
        Some("escalate"),
        Some("en-US"),
        Some("Stop. I need to speak with a human."),
    ),
    (
        "voice.barge_long_vi",
        "barge_long_vi.wav",
        "VI longer stacked barge (stresses VAD / recovery).",
        Some("voice"),
        Some("correction"),
        Some("vi-VN"),
        None,
    ),
    (
        "voice.barge_short",
        "barge_wait_en.wav",
        "EN short barge: “Wait a second…”.",
        Some("voice"),
        Some("correction"),
        Some("en-US"),
        Some("Wait a second…"),
    ),
    (
        "voice.barge_soft",
        "barge_soft_en.wav",
        "Alias of voice.soft.",
        Some("voice"),
        Some("correction"),
        Some("en-US"),
        Some("Um, hang on…"),
    ),
    (
        "voice.barge_sorry",
        "barge_sorry_en.wav",
        "EN barge: “Sorry — one second…”.",
        Some("voice"),
        Some("correction"),
        Some("en-US"),
        Some("Sorry — one second…"),
    ),
    (
        "voice.barge_sorry_en",
        "barge_sorry_en.wav",
        "Alias of voice.barge_sorry.",
        Some("voice"),
        Some("correction"),
        Some("en-US"),
        Some("Sorry — one second…"),
    ),
    (
        "voice.barge_vi",
        "barge_wait_vi.wav",
        "VI short barge (wait / cut-in).",
        Some("voice"),
        Some("correction"),
        Some("vi-VN"),
        None,
    ),
    (
        "voice.barge_wait",
        "barge_wait_en.wav",
        "Alias of voice.barge_short — “Wait a second…”.",
        Some("voice"),
        Some("correction"),
        Some("en-US"),
        Some("Wait a second…"),
    ),
    (
        "voice.barge_wait_en",
        "barge_wait_en.wav",
        "Alias of voice.barge_short — “Wait a second…”.",
        Some("voice"),
        Some("correction"),
        Some("en-US"),
        Some("Wait a second…"),
    ),
    (
        "voice.barge_wait_vi",
        "barge_wait_vi.wav",
        "Alias of voice.barge_vi.",
        Some("voice"),
        Some("correction"),
        Some("vi-VN"),
        None,
    ),
    (
        "voice.correction",
        "barge_correction_en.wav",
        "EN correction barge: “No wait - I meant next Friday.”",
        Some("voice"),
        Some("correction"),
        Some("en-US"),
        Some("No wait - I meant next Friday."),
    ),
    (
        "voice.escalate",
        "barge_escalate_en.wav",
        "EN escalate barge: ask for a human agent.",
        Some("voice"),
        Some("escalate"),
        Some("en-US"),
        Some("Stop. I need to speak with a human."),
    ),
    (
        "voice.human",
        "barge_escalate_en.wav",
        "Alias of voice.escalate — human handoff ask.",
        Some("voice"),
        Some("escalate"),
        Some("en-US"),
        Some("Stop. I need to speak with a human."),
    ),
    (
        "voice.interrupt",
        "barge_wait_en.wav",
        "Alias of voice.barge_short — “Wait a second…”.",
        Some("voice"),
        Some("correction"),
        Some("en-US"),
        Some("Wait a second…"),
    ),
    (
        "voice.interrupt_ja",
        "real_interrupt_ja.wav",
        "JA interrupt speech (same file as legacy real_interrupt_ja).",
        Some("legacy"),
        Some("correction"),
        Some("ja-JP"),
        None,
    ),
    (
        "voice.soft",
        "barge_soft_en.wav",
        "EN soft barge: “Um, hang on…”.",
        Some("voice"),
        Some("correction"),
        Some("en-US"),
        Some("Um, hang on…"),
    ),
    (
        "voice.uhhuh",
        "backchannel_uhhuh_en.wav",
        "Alias of voice.backchannel — EN uh-huh sustain.",
        Some("voice"),
        Some("backchannel"),
        Some("en-US"),
        Some("uh-huh"),
    ),
    (
        "voice.uhhuh_vi",
        "backchannel_vi.wav",
        "Alias of voice.backchannel_vi.",
        Some("voice"),
        Some("backchannel"),
        Some("vi-VN"),
        None,
    ),
    (
        "voice.yeah",
        "backchannel_yeah_en.wav",
        "Alias of voice.backchannel_yeah.",
        Some("voice"),
        Some("backchannel"),
        Some("en-US"),
        Some("Yeah. Okay. Mhm."),
    ),
];

/// ops.list_cues — full catalog (port of `audio/cue_catalog.list_all_cues`):
/// builtin entries (aliases first, then leftover files), target overrides,
/// config aliases + extra dirs, usage.
pub fn op_list_cues(project_root: &Path) -> Result<Map<String, Json>, ConfigError> {
    let cfg = load_config(project_root.to_path_buf(), None, None)?;
    let cues_dir = crate::authoring::package_templates_dir()
        .map(|t| t.join("cues"))
        .unwrap_or_else(|| std::path::PathBuf::from("templates/cues"));

    // Builtin: aliases first (sorted), then leftover *.wav files (sorted).
    let mut builtin: Vec<Json> = Vec::new();
    let mut seen_files: std::collections::HashSet<String> = std::collections::HashSet::new();
    let mut files: Vec<String> = Vec::new();
    if let Ok(rd) = std::fs::read_dir(&cues_dir) {
        for entry in rd.flatten() {
            let p = entry.path();
            if p.extension().and_then(|e| e.to_str()) == Some("wav") {
                if let Some(name) = p.file_name().map(|n| n.to_string_lossy().into_owned()) {
                    files.push(name);
                }
            }
        }
    }
    files.sort();
    for (id, file, desc, kind, icls, locale, text) in BUILTIN_CUES.iter() {
        if !files.iter().any(|f| f == file) {
            continue;
        }
        seen_files.insert(file.to_string());
        let mut m = Map::new();
        m.insert("id".into(), json!(id));
        m.insert("file".into(), json!(file));
        m.insert("source".into(), json!("builtin"));
        m.insert(
            "path".into(),
            json!(cues_dir.join(file).to_string_lossy().into_owned()),
        );
        m.insert("ref".into(), json!(format!("builtin:{id}")));
        m.insert("description".into(), json!(desc));
        m.insert("kind".into(), json!(kind));
        m.insert("interrupt_class".into(), json!(icls));
        m.insert("locale".into(), json!(locale));
        m.insert("text".into(), json!(text));
        builtin.push(Json::Object(m));
    }
    for fname in &files {
        if seen_files.contains(fname) {
            continue;
        }
        let mut m = Map::new();
        m.insert("id".into(), json!(fname));
        m.insert("file".into(), json!(fname));
        m.insert("source".into(), json!("builtin"));
        m.insert(
            "path".into(),
            json!(cues_dir.join(fname).to_string_lossy().into_owned()),
        );
        m.insert("ref".into(), json!(format!("builtin:{fname}")));
        m.insert("description".into(), Json::Null);
        m.insert("kind".into(), Json::Null);
        m.insert("interrupt_class".into(), Json::Null);
        m.insert("locale".into(), Json::Null);
        m.insert("text".into(), Json::Null);
        builtin.push(Json::Object(m));
    }

    // Target overrides from .agent-sim/cues/.
    let target_dir = cfg.cues_dir();
    let mut target: Vec<Json> = Vec::new();
    if let Ok(rd) = std::fs::read_dir(&target_dir) {
        let mut names: Vec<String> = rd
            .flatten()
            .filter_map(|entry| {
                let p = entry.path();
                if p.extension().and_then(|e| e.to_str()) == Some("wav") {
                    p.file_name().map(|n| n.to_string_lossy().into_owned())
                } else {
                    None
                }
            })
            .collect();
        names.sort();
        for name in names {
            let mut m = Map::new();
            m.insert("id".into(), json!(name));
            m.insert("file".into(), json!(name));
            m.insert("source".into(), json!("target"));
            m.insert(
                "path".into(),
                json!(target_dir.join(&name).to_string_lossy().into_owned()),
            );
            m.insert("ref".into(), json!(name));
            m.insert(
                "overrides_builtin".into(),
                json!(cues_dir.join(&name).is_file()),
            );
            target.push(Json::Object(m));
        }
    }

    // Config aliases + extra dirs.
    let mut aliases = Map::new();
    for (k, v) in &cfg.cues.aliases {
        aliases.insert(k.clone(), json!(v));
    }
    let mut extra: Vec<Json> = Vec::new();
    let root = project_root;
    for d in &cfg.cues.dirs {
        let p = std::path::Path::new(d);
        let full = if p.is_absolute() {
            p.to_path_buf()
        } else {
            root.join(p)
        };
        extra.push(json!(full.to_string_lossy().into_owned()));
    }

    // resolve_examples — port of `describe_resolution` for the three example
    // refs (`builtin:noise.loud`, `builtin:noise.ambient`, `@backchannel`).
    let resolve_examples: Vec<(String, Map<String, Json>)> = [
        "builtin:noise.loud",
        "builtin:noise.ambient",
        "@backchannel",
    ]
    .iter()
    .map(|asset| {
        (
            asset.to_string(),
            describe_cue_resolution(project_root, asset),
        )
    })
    .collect();
    let mut rex = Map::new();
    for (k, v) in resolve_examples {
        rex.insert(k, Json::Object(v));
    }

    let mut out = Map::new();
    out.insert(
        "resolve_order".into(),
        json!([
            "absolute path",
            "cues.aliases (config.yaml)",
            "builtin:id / @id",
            "scenario directory",
            ".agent-sim/cues/ (target override)",
            "cues.dirs (config.yaml)",
            "package templates/cues/"
        ]),
    );
    out.insert("builtin".into(), Json::Array(builtin));
    out.insert("target".into(), Json::Array(target));
    out.insert("aliases".into(), Json::Object(aliases));
    out.insert("extra_dirs".into(), Json::Array(extra));
    out.insert(
        "usage".into(),
        json!({
            "scenario_asset_examples": [
                "builtin:voice.barge_short",
                "builtin:voice.backchannel",
                "builtin:noise.loud",
                "@noise.ambient",
                "loud_noise_burst.wav",
                "my_cafe.wav  # place in .agent-sim/cues/",
                "office  # if cues.aliases.office is set"
            ],
            "wav_format": "PCM16 mono @ 24000 Hz",
            "vocal_aliases": [
                "voice.barge_short",
                "voice.barge_sorry",
                "voice.backchannel",
                "voice.barge_vi"
            ]
        }),
    );
    out.insert("resolve_examples".into(), Json::Object(rex));
    Ok(out)
}

/// Resolve one cue asset with metadata (port of `describe_resolution`):
/// builtin alias → BUILTIN_CUES file (target dir override wins), bare name →
/// target dir; error payload when unresolvable.
pub fn describe_cue_resolution(project_root: &Path, asset: &str) -> Map<String, Json> {
    let mut m = Map::new();
    m.insert("asset".into(), json!(asset));
    let cfg = load_config(project_root.to_path_buf(), None, None).ok();
    let cues_dir = crate::authoring::package_templates_dir()
        .map(|t| t.join("cues"))
        .unwrap_or_else(|| std::path::PathBuf::from("templates/cues"));
    let target_dir = cfg.map(|c| c.cues_dir());
    let meta = builtin_cue_meta(asset);
    let name = asset
        .strip_prefix("builtin:")
        .or_else(|| asset.strip_prefix('@'))
        .unwrap_or(asset)
        .trim();
    let file = meta.map(|m| m.1.to_string());
    let pkg_cand = cues_dir.join(file.clone().unwrap_or_else(|| name.to_string()));
    let resolved = if pkg_cand.is_file() {
        Some(pkg_cand)
    } else {
        let tdir = target_dir
            .clone()
            .unwrap_or_else(|| project_root.join(".agent-sim/cues"));
        let tdir_cand = tdir.join(file.clone().unwrap_or_else(|| name.to_string()));
        if tdir_cand.is_file() {
            Some(tdir_cand)
        } else {
            None
        }
    };
    match resolved {
        Some(p) => {
            m.insert("ok".into(), json!(true));
            m.insert("path".into(), json!(p.to_string_lossy().into_owned()));
            if let Some((_, fname, desc, kind, icls, locale, text)) = meta {
                m.insert("description".into(), json!(desc));
                m.insert("kind".into(), json!(kind));
                m.insert("interrupt_class".into(), json!(icls));
                m.insert("locale".into(), json!(locale));
                m.insert("text".into(), json!(text));
                m.insert("file".into(), json!(fname));
            }
        }
        None => {
            m.insert("ok".into(), json!(false));
            m.insert(
                "error".into(),
                json!(format!("Cue asset not found: {asset}")),
            );
        }
    }
    m
}

/// Look up builtin cue metadata by alias (port of `builtin_cue_meta`).
fn builtin_cue_meta(alias: &str) -> Option<BuiltinCueEntry> {
    let key = alias
        .strip_prefix("builtin:")
        .or_else(|| alias.strip_prefix('@'))
        .unwrap_or(alias)
        .trim();
    BUILTIN_CUES
        .iter()
        .find(|(id, ..)| *id == key || *id == key.replace('-', "_"))
        .copied()
}

/// ops.get_run_status — SQLite read.
pub fn op_get_run_status(
    project_root: &Path,
    run_id: &str,
) -> Result<Map<String, Json>, ConfigError> {
    let cfg = load_config(project_root.to_path_buf(), None, None)?;
    if !cfg.sqlite_path().exists() {
        let mut m = Map::new();
        m.insert("found".into(), json!(false));
        m.insert("run_id".into(), json!(run_id));
        return Ok(m);
    }
    let store = crate::logging::sqlite::RunStore::new(cfg.sqlite_path().to_string_lossy().as_ref());
    match store.get_run(run_id) {
        Ok(Some(run)) => {
            let mut m = Map::new();
            m.insert("found".into(), json!(true));
            // Python get_run_status returns exactly these keys (no
            // agent_name/verdict/summary_json) in this order.
            for k in [
                "run_id",
                "status",
                "scenario_id",
                "room_name",
                "started_utc",
                "ended_utc",
                "duration_ms",
                "turn_count",
                "tool_errors",
                "report_dir",
            ] {
                if let Some(v) = run.get(k) {
                    m.insert(k.to_string(), v.clone());
                }
            }
            Ok(m)
        }
        Ok(None) => {
            let mut m = Map::new();
            m.insert("found".into(), json!(false));
            m.insert("run_id".into(), json!(run_id));
            Ok(m)
        }
        Err(e) => Err(ConfigError(format!("sqlite read error: {e}"))),
    }
}

/// ops.get_run_log — events.jsonl with filters. `kind` supports trailing `*`.
pub fn op_get_run_log(
    project_root: &Path,
    run_id: &str,
    kind: Option<&str>,
    turn: Option<i64>,
    source: Option<&str>,
    since_mono_ms: Option<i64>,
    limit: usize,
) -> Result<Map<String, Json>, ConfigError> {
    let cfg = load_config(project_root.to_path_buf(), None, None)?;
    let events_path = cfg.reports_dir().join(run_id).join("events.jsonl");
    if !events_path.exists() {
        let mut m = Map::new();
        m.insert("found".into(), json!(false));
        m.insert("run_id".into(), json!(run_id));
        m.insert(
            "error".into(),
            json!(format!("{} not found", events_path.display())),
        );
        return Ok(m);
    }

    let mut out: Vec<Json> = Vec::new();
    let mut total = 0usize;
    let text = std::fs::read_to_string(&events_path)
        .map_err(|e| ConfigError(format!("{}: read error — {e}", events_path.display())))?;
    for line in text.lines() {
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        let Ok(v) = serde_json::from_str::<Json>(line) else {
            continue;
        };
        let Some(e) = v.as_object() else { continue };
        total += 1;
        let ekind = as_str(e.get("kind").unwrap_or(&Json::Null));
        if let Some(k) = kind {
            if let Some(prefix) = k.strip_suffix('*') {
                if !ekind.starts_with(prefix) {
                    continue;
                }
            } else if ekind != k {
                continue;
            }
        }
        if let Some(t) = turn {
            if e.get("turn").and_then(|v| v.as_i64()) != Some(t) {
                continue;
            }
        }
        if let Some(s) = source {
            if as_str(e.get("source").unwrap_or(&Json::Null)) != s {
                continue;
            }
        }
        if let Some(m) = since_mono_ms {
            if e.get("ts_mono_ms").and_then(|v| v.as_i64()).unwrap_or(0) < m {
                continue;
            }
        }
        out.push(Json::Object(e.clone()));
    }

    let truncated = out.len() > limit;
    let matched = out.len();
    out.truncate(limit);

    let mut m = Map::new();
    m.insert("found".into(), json!(true));
    m.insert("run_id".into(), json!(run_id));
    m.insert("total_events".into(), json!(total));
    m.insert("matched".into(), json!(matched));
    m.insert("truncated".into(), json!(truncated));
    m.insert("events".into(), Json::Array(out));
    Ok(m)
}

/// ops.get_run_report — summary/meta + suspicious turns + paths.
pub fn op_get_run_report(
    project_root: &Path,
    run_id: &str,
) -> Result<Map<String, Json>, ConfigError> {
    let cfg = load_config(project_root.to_path_buf(), None, None)?;
    let report_dir = cfg.reports_dir().join(run_id);
    let summary_path = report_dir.join("summary.json");
    if !summary_path.exists() {
        let status = op_get_run_status(project_root, run_id)?;
        let mut m = Map::new();
        m.insert("found".into(), json!(false));
        m.insert("run_id".into(), json!(run_id));
        m.insert("status".into(), Json::Object(status));
        return Ok(m);
    }

    let summary: Json = match std::fs::read_to_string(&summary_path) {
        Ok(t) => serde_json::from_str(&t).unwrap_or(Json::Null),
        Err(_) => Json::Null,
    };
    let summary_obj = summary.as_object().cloned().unwrap_or_default();
    let meta_path = report_dir.join("meta.json");
    let meta: Json = match std::fs::read_to_string(&meta_path) {
        Ok(t) => serde_json::from_str(&t).unwrap_or(Json::Null),
        Err(_) => Json::Null,
    };

    let mut suspicious: Vec<Json> = Vec::new();
    let warn_ms = cfg.observe.turn_taking_warn_ms;
    if let Some(turns) = summary_obj.get("turns").and_then(|v| v.as_array()) {
        for t in turns {
            let mut reasons: Vec<String> = Vec::new();
            if let Some(te) = t.get("tool_errors").and_then(|v| v.as_i64()) {
                if te > 0 {
                    reasons.push(format!("{te} tool error(s)"));
                }
            }
            if let Some(tm) = t.get("turn_taking_ms").and_then(|v| v.as_i64()) {
                if tm > warn_ms {
                    reasons.push(format!("slow turn-taking {tm}ms > {warn_ms}ms"));
                }
            }
            if t.get("interrupted")
                .and_then(|v| v.as_bool())
                .unwrap_or(false)
            {
                reasons.push("interrupted".to_string());
            }
            if !reasons.is_empty() {
                let mut row = t.as_object().cloned().unwrap_or_default();
                row.insert("reasons".into(), json!(reasons));
                suspicious.push(Json::Object(row));
            }
        }
    }

    let audio_path = report_dir.join("conversation.wav");
    let mut m = Map::new();
    m.insert("found".into(), json!(true));
    m.insert("run_id".into(), json!(run_id));
    m.insert("summary".into(), summary);
    m.insert("meta".into(), meta);
    m.insert("suspicious_turns".into(), Json::Array(suspicious));
    m.insert(
        "timeline_path".into(),
        json!(report_dir
            .join("timeline.md")
            .to_string_lossy()
            .into_owned()),
    );
    m.insert(
        "events_path".into(),
        json!(report_dir
            .join("events.jsonl")
            .to_string_lossy()
            .into_owned()),
    );
    m.insert(
        "audio_path".into(),
        if audio_path.exists() {
            json!(audio_path.to_string_lossy().into_owned())
        } else {
            Json::Null
        },
    );
    Ok(m)
}

/// ops.list_runs — SQLite read, newest first.
pub fn op_list_runs(
    project_root: &Path,
    limit: i64,
    scenario_id: Option<&str>,
) -> Result<Vec<Map<String, Json>>, ConfigError> {
    let cfg = load_config(project_root.to_path_buf(), None, None)?;
    if !cfg.sqlite_path().exists() {
        return Ok(Vec::new());
    }
    let store = crate::logging::sqlite::RunStore::new(cfg.sqlite_path().to_string_lossy().as_ref());
    store
        .list_runs(limit, scenario_id)
        .map_err(|e| ConfigError(format!("sqlite read error: {e}")))
}

/// metrics_digest — the 13-key flat digest used by suite rows / compare_runs
/// (port of `metrics.py:metrics_digest`, exact passthrough — ints stay ints).
pub fn metrics_digest(metrics: Option<&Map<String, Json>>) -> Map<String, Json> {
    let mut out = Map::new();
    let get = |k: &str| metrics.and_then(|m| m.get(k)).cloned();
    let tt = metrics
        .and_then(|m| m.get("turn_taking_ms"))
        .and_then(|v| v.as_object())
        .cloned()
        .unwrap_or_default();
    let rec = metrics
        .and_then(|m| m.get("recovery_ms"))
        .and_then(|v| v.as_object())
        .cloned()
        .unwrap_or_default();
    let att = metrics
        .and_then(|m| m.get("turn_taking_audio_ms"))
        .and_then(|v| v.as_object())
        .cloned()
        .unwrap_or_default();
    out.insert("ttfw_ms".into(), get("ttfw_ms").unwrap_or(Json::Null));
    out.insert(
        "turn_p50_ms".into(),
        tt.get("p50").cloned().unwrap_or(Json::Null),
    );
    out.insert(
        "turn_p95_ms".into(),
        tt.get("p95").cloned().unwrap_or(Json::Null),
    );
    out.insert(
        "recovery_p50_ms".into(),
        rec.get("p50").cloned().unwrap_or(Json::Null),
    );
    out.insert(
        "barge_count".into(),
        get("barge_count").unwrap_or(Json::Null),
    );
    out.insert(
        "barge_recovery_rate".into(),
        get("barge_recovery_rate").unwrap_or(Json::Null),
    );
    out.insert("talk_ratio".into(), get("talk_ratio").unwrap_or(Json::Null));
    out.insert(
        "user_words_p50".into(),
        get("user_words_p50").unwrap_or(Json::Null),
    );
    out.insert(
        "user_words_natural_p50".into(),
        get("user_words_natural_p50").unwrap_or(Json::Null),
    );
    out.insert("ttfa_ms".into(), get("ttfa_run_ms").unwrap_or(Json::Null));
    out.insert(
        "turn_taking_audio_p50_ms".into(),
        att.get("p50").cloned().unwrap_or(Json::Null),
    );
    out.insert(
        "turn_taking_audio_p95_ms".into(),
        att.get("p95").cloned().unwrap_or(Json::Null),
    );
    out
}

fn as_f64(v: &Json) -> Option<f64> {
    v.as_f64()
}

/// compare_runs — digest diff (port of `ops.compare_runs`).
pub fn op_compare_runs(
    project_root: &Path,
    run_id_a: &str,
    run_id_b: &str,
) -> Result<Map<String, Json>, ConfigError> {
    let a = op_get_run_report(project_root, run_id_a)?;
    let b = op_get_run_report(project_root, run_id_b)?;
    if a.get("found").and_then(|v| v.as_bool()) != Some(true)
        || b.get("found").and_then(|v| v.as_bool()) != Some(true)
    {
        let mut m = Map::new();
        m.insert("error".into(), json!("one or both runs not found"));
        m.insert(
            "a".into(),
            json!(a.get("found").cloned().unwrap_or(Json::Null)),
        );
        m.insert(
            "b".into(),
            json!(b.get("found").cloned().unwrap_or(Json::Null)),
        );
        return Ok(m);
    }

    let digest = |r: &Map<String, Json>| -> Map<String, Json> {
        let s = r
            .get("summary")
            .and_then(|v| v.as_object())
            .cloned()
            .unwrap_or_default();
        let metrics = s.get("metrics").and_then(|v| v.as_object());
        let md = metrics_digest(metrics);
        let av = s
            .get("assert_verify")
            .and_then(|v| v.as_object())
            .cloned()
            .unwrap_or_default();
        let mut d = Map::new();
        d.insert(
            "run_id".into(),
            r.get("run_id").cloned().unwrap_or(Json::Null),
        );
        d.insert(
            "status".into(),
            s.get("status").cloned().unwrap_or(Json::Null),
        );
        d.insert(
            "duration_ms".into(),
            s.get("duration_ms").cloned().unwrap_or(Json::Null),
        );
        d.insert(
            "turn_count".into(),
            s.get("turn_count").cloned().unwrap_or(Json::Null),
        );
        d.insert(
            "tool_errors".into(),
            s.get("tool_errors").cloned().unwrap_or(Json::Null),
        );
        d.insert(
            "interruptions".into(),
            s.get("interruptions").cloned().unwrap_or(Json::Null),
        );
        let turn_p50 = md.get("turn_p50_ms").and_then(|v| v.as_f64()).or_else(|| {
            s.get("turn_taking_ms")
                .and_then(|v| v.as_object())
                .and_then(|m| m.get("p50"))
                .and_then(|v| v.as_f64())
        });
        d.insert(
            "turn_taking_p50".into(),
            turn_p50.map(Json::from).unwrap_or(Json::Null),
        );
        let turn_p95 = md.get("turn_p95_ms").and_then(|v| v.as_f64()).or_else(|| {
            s.get("turn_taking_ms")
                .and_then(|v| v.as_object())
                .and_then(|m| m.get("p95"))
                .and_then(|v| v.as_f64())
        });
        d.insert(
            "turn_taking_p95".into(),
            turn_p95.map(Json::from).unwrap_or(Json::Null),
        );
        d.insert(
            "ttfw_ms".into(),
            md.get("ttfw_ms").cloned().unwrap_or(Json::Null),
        );
        d.insert(
            "recovery_p50_ms".into(),
            md.get("recovery_p50_ms").cloned().unwrap_or(Json::Null),
        );
        d.insert(
            "barge_count".into(),
            md.get("barge_count").cloned().unwrap_or(Json::Null),
        );
        d.insert(
            "barge_recovery_rate".into(),
            md.get("barge_recovery_rate").cloned().unwrap_or(Json::Null),
        );
        d.insert(
            "talk_ratio".into(),
            md.get("talk_ratio").cloned().unwrap_or(Json::Null),
        );
        d.insert(
            "ttfa_ms".into(),
            md.get("ttfa_ms").cloned().unwrap_or(Json::Null),
        );
        d.insert(
            "turn_taking_audio_p95".into(),
            md.get("turn_taking_audio_p95_ms")
                .cloned()
                .unwrap_or(Json::Null),
        );
        let verdict = s
            .get("verdict")
            .and_then(|v| v.as_object())
            .and_then(|v| v.get("verdict"))
            .cloned();
        d.insert("verdict".into(), verdict.unwrap_or(Json::Null));
        d.insert(
            "assert_pass".into(),
            av.get("pass").cloned().unwrap_or(Json::Null),
        );
        d
    };

    let da = digest(a.get("run_id").map(|_| &a).unwrap_or(&a));
    let db = digest(b.get("run_id").map(|_| &b).unwrap_or(&b));
    let mut deltas = Map::new();
    for (k, va) in &da {
        if k == "run_id" {
            continue;
        }
        let vb = db.get(k).unwrap_or(&Json::Null);
        if va != vb {
            let mut pair = Map::new();
            pair.insert("a".into(), va.clone());
            pair.insert("b".into(), vb.clone());
            deltas.insert(k.clone(), Json::Object(pair));
        }
    }

    let mut m = Map::new();
    m.insert("a".into(), Json::Object(da));
    m.insert("b".into(), Json::Object(db));
    m.insert("deltas".into(), Json::Object(deltas));
    Ok(m)
}

/// evaluate_baseline_gate — hard gate: candidate must not regress vs baseline
/// digest. Port of `ops.evaluate_baseline_gate`; the MCP/CLI surface passes
/// only the 4 public knobs (defaults for the internal ones, mirroring Python
/// where every call site passes just the 4).
#[allow(clippy::too_many_arguments)]
pub fn evaluate_baseline_gate(
    baseline: &Map<String, Json>,
    candidate: &Map<String, Json>,
    max_ttfw_regression_ms: f64,
    max_turn_p95_regression_ms: f64,
    max_duration_regression_ms: f64,
    max_barge_recovery_drop: f64,
    max_ttfa_regression_ms: f64,
    max_turn_audio_p95_regression_ms: f64,
    require_status_done: bool,
) -> Map<String, Json> {
    let mut reasons: Vec<String> = Vec::new();
    let mut checks: Vec<Json> = Vec::new();
    let mut push_check = |m: Map<String, Json>| {
        checks.push(Json::Object(m));
    };

    if require_status_done {
        let st = as_str(candidate.get("status").unwrap_or(&Json::Null));
        let ok = st == "done" || st == "pass" || st == "passed";
        let mut c = Map::new();
        c.insert("check".into(), json!("status_done"));
        c.insert("pass".into(), json!(ok));
        c.insert("actual".into(), json!(st));
        push_check(c);
        if !ok {
            reasons.push(format!("candidate status={st:?} not done"));
        }
    }

    let ap = candidate.get("assert_pass").and_then(|v| v.as_bool());
    if ap == Some(false) {
        let mut c = Map::new();
        c.insert("check".into(), json!("assert_pass"));
        c.insert("pass".into(), json!(false));
        push_check(c);
        reasons.push("candidate assert_verify failed".to_string());
    } else if ap == Some(true) {
        let mut c = Map::new();
        c.insert("check".into(), json!("assert_pass"));
        c.insert("pass".into(), json!(true));
        push_check(c);
    }

    let mut reg = |key: &str, limit: f64, reasons: &mut Vec<String>| {
        let b = baseline.get(key).and_then(as_f64);
        let c = candidate.get(key).and_then(as_f64);
        let mut check = Map::new();
        check.insert("check".into(), json!(format!("regression:{key}")));
        match (b, c) {
            (None, _) | (_, None) => {
                check.insert("pass".into(), json!(true));
                check.insert("skipped".into(), json!(true));
                check.insert("baseline".into(), b.map(Json::from).unwrap_or(Json::Null));
                check.insert("candidate".into(), c.map(Json::from).unwrap_or(Json::Null));
                push_check(check);
            }
            (Some(bv), Some(cv)) => {
                let delta = cv - bv;
                let ok = delta <= limit;
                check.insert("pass".into(), json!(ok));
                check.insert("baseline".into(), json!(bv));
                check.insert("candidate".into(), json!(cv));
                check.insert("delta".into(), json!(delta));
                check.insert("max_delta".into(), json!(limit));
                push_check(check);
                if !ok {
                    reasons.push(format!(
                        "{key} +{delta:.0}ms over baseline (limit +{limit:.0}ms): {bv:.0} → {cv:.0}"
                    ));
                }
            }
        }
    };

    reg("ttfw_ms", max_ttfw_regression_ms, &mut reasons);
    reg("turn_taking_p95", max_turn_p95_regression_ms, &mut reasons);
    reg("duration_ms", max_duration_regression_ms, &mut reasons);
    // Perceived audio latency (audio-onset ground truth). baseline=None AND
    // candidate=value → skip (not a regression) — the reg helper already
    // skips either-side-None.
    reg("ttfa_ms", max_ttfa_regression_ms, &mut reasons);
    reg(
        "turn_taking_audio_p95",
        max_turn_audio_p95_regression_ms,
        &mut reasons,
    );

    let bt = baseline.get("tool_errors").and_then(as_f64);
    let ct = candidate.get("tool_errors").and_then(as_f64);
    if let (Some(bv), Some(cv)) = (bt, ct) {
        let ok = cv <= bv;
        let mut c = Map::new();
        c.insert("check".into(), json!("tool_errors_not_up"));
        c.insert("pass".into(), json!(ok));
        c.insert("baseline".into(), json!(bv));
        c.insert("candidate".into(), json!(cv));
        push_check(c);
        if !ok {
            reasons.push(format!("tool_errors rose {bv:.0} → {cv:.0}"));
        }
    }

    // Rate metric: higher is better — fail if drop exceeds max_barge_recovery_drop.
    let br_b = baseline.get("barge_recovery_rate").and_then(as_f64);
    let br_c = candidate.get("barge_recovery_rate").and_then(as_f64);
    let mut barge_check = Map::new();
    barge_check.insert("check".into(), json!("barge_recovery_rate_not_down"));
    match (br_b, br_c) {
        (Some(bv), Some(cv)) => {
            let drop = bv - cv;
            let ok = drop <= max_barge_recovery_drop;
            barge_check.insert("pass".into(), json!(ok));
            barge_check.insert("baseline".into(), json!(bv));
            barge_check.insert("candidate".into(), json!(cv));
            barge_check.insert("drop".into(), json!(drop));
            barge_check.insert("max_drop".into(), json!(max_barge_recovery_drop));
            push_check(barge_check);
            if !ok {
                reasons.push(format!(
                    "barge_recovery_rate dropped {bv:.2} → {cv:.2} (max drop {max_barge_recovery_drop:.2})"
                ));
            }
        }
        _ => {
            barge_check.insert("pass".into(), json!(true));
            barge_check.insert("skipped".into(), json!(true));
            barge_check.insert(
                "baseline".into(),
                br_b.map(Json::from).unwrap_or(Json::Null),
            );
            barge_check.insert(
                "candidate".into(),
                br_c.map(Json::from).unwrap_or(Json::Null),
            );
            push_check(barge_check);
        }
    }

    let ok = reasons.is_empty();
    let mut m = Map::new();
    m.insert("ok".into(), json!(ok));
    m.insert("pass".into(), json!(ok));
    m.insert("checks".into(), Json::Array(checks));
    m.insert("reasons".into(), json!(reasons));
    m
}

/// compare_runs_with_baseline — attach the hard ``gate`` for CI exit codes.
pub fn op_compare_runs_with_baseline(
    project_root: &Path,
    baseline_run_id: &str,
    candidate_run_id: &str,
    max_ttfw_regression_ms: f64,
    max_turn_p95_regression_ms: f64,
    max_duration_regression_ms: f64,
    max_barge_recovery_drop: f64,
) -> Result<Map<String, Json>, ConfigError> {
    let raw = op_compare_runs(project_root, baseline_run_id, candidate_run_id)?;
    if raw.get("error").is_some() {
        let mut m = raw.clone();
        let mut gate = Map::new();
        gate.insert("ok".into(), json!(false));
        gate.insert("pass".into(), json!(false));
        gate.insert(
            "reasons".into(),
            json!(vec![as_str(raw.get("error").unwrap_or(&Json::Null))]),
        );
        m.insert("gate".into(), Json::Object(gate));
        return Ok(m);
    }
    let a = raw
        .get("a")
        .and_then(|v| v.as_object())
        .cloned()
        .unwrap_or_default();
    let b = raw
        .get("b")
        .and_then(|v| v.as_object())
        .cloned()
        .unwrap_or_default();
    let gate = evaluate_baseline_gate(
        &a,
        &b,
        max_ttfw_regression_ms,
        max_turn_p95_regression_ms,
        max_duration_regression_ms,
        max_barge_recovery_drop,
        2000.0, // max_ttfa_regression_ms (internal default)
        2500.0, // max_turn_audio_p95_regression_ms (internal default)
        true,   // require_status_done (internal default)
    );
    let mut m = raw;
    m.insert("baseline_run_id".into(), json!(baseline_run_id));
    m.insert("candidate_run_id".into(), json!(candidate_run_id));
    m.insert("gate".into(), Json::Object(gate));
    Ok(m)
}

/// ops.scenario_from_run facade.
pub fn op_scenario_from_run(
    project_root: &Path,
    run_id: &str,
    scenario_id: Option<&str>,
    write: bool,
) -> Result<Map<String, Json>, String> {
    let cfg = load_config(project_root.to_path_buf(), None, None).map_err(|e| e.0)?;
    crate::scenario_from_run::scenario_from_run(
        project_root,
        run_id,
        scenario_id,
        write,
        &cfg.simulator.language,
    )
}

/// Render the composed system instruction a saved optimized variant would
/// produce (ops.render_prompt_variant — P9 apply surface, data-plane).
pub fn render_prompt_variant(
    project_root: &Path,
    name: &str,
) -> Result<Map<String, Json>, ConfigError> {
    let cfg = load_config(project_root.to_path_buf(), None, None)?;
    let artifact = cfg.optimized_dir().join(name).join("prompt.yaml");
    if !artifact.exists() {
        return Err(ConfigError(format!(
            "No optimized prompt at {} — run `lks optimize --name {name}` first",
            artifact.display()
        )));
    }
    let text = std::fs::read_to_string(&artifact)
        .map_err(|e| ConfigError(format!("{}: read error — {e}", artifact.display())))?;
    let mut m = Map::new();
    m.insert("name".into(), json!(name));
    m.insert(
        "path".into(),
        json!(artifact.to_string_lossy().into_owned()),
    );
    m.insert("prompt".into(), json!(text));
    Ok(m)
}

/// Preflight — needs the P2 livekit connectivity checks; report the
/// config-level checks (data-plane) with a clear note.
/// Core preflight checks (port of `preflight.py:run_preflight` — the
/// connectivity-dependent `livekit.api` check is added by the caller when
/// `connectivity` is true; this fn covers checks 1–5 + 7).
pub fn op_preflight_core(
    project_root: &Path,
    profile: Option<&str>,
    environment: Option<&str>,
) -> Result<(Map<String, Json>, Option<crate::config::SimConfig>), ConfigError> {
    let mut checks: Vec<Json> = Vec::new();
    let mut ok = true;

    /// Python preflight checks carry `status` ("pass"|"warn"|"fail"), not a
    /// bool — replicate exactly. `fail` flips the aggregate `ok`.
    fn add(checks: &mut Vec<Json>, ok: &mut bool, name: &str, status: &str, detail: String) {
        let mut c = Map::new();
        c.insert("name".into(), json!(name));
        c.insert("status".into(), json!(status));
        c.insert("detail".into(), json!(detail));
        if status == "fail" {
            *ok = false;
        }
        checks.push(Json::Object(c));
    }

    // 1. config (load with profile + environment; fail → early return)
    let cfg = match load_config(project_root.to_path_buf(), profile, environment) {
        Ok(cfg) => {
            add(
                &mut checks,
                &mut ok,
                "config",
                "pass",
                cfg.dot_dir().join("config.yaml").display().to_string(),
            );
            cfg
        }
        Err(e) => {
            add(&mut checks, &mut ok, "config", "fail", e.0);
            let mut m = Map::new();
            m.insert("ok".into(), json!(ok));
            m.insert("checks".into(), Json::Array(checks));
            return Ok((m, None));
        }
    };

    // 2. livekit.url scheme
    let url = &cfg.livekit.url;
    if url.starts_with("ws://")
        || url.starts_with("wss://")
        || url.starts_with("http://")
        || url.starts_with("https://")
    {
        add(&mut checks, &mut ok, "livekit.url", "pass", url.clone());
    } else {
        add(
            &mut checks,
            &mut ok,
            "livekit.url",
            "fail",
            format!("`{url}` must start with wss:// (LiveKit Cloud) or ws://"),
        );
    }

    // 3. observe.timezone IANA validity
    if jiff::tz::TimeZone::get(&cfg.observe.timezone).is_ok() {
        add(
            &mut checks,
            &mut ok,
            "observe.timezone",
            "pass",
            cfg.observe.timezone.clone(),
        );
    } else {
        add(
            &mut checks,
            &mut ok,
            "observe.timezone",
            "fail",
            format!("Unknown IANA timezone `{}`", cfg.observe.timezone),
        );
    }

    // 4. folders (mkdir side effect)
    let _ = std::fs::create_dir_all(cfg.reports_dir());
    let _ = std::fs::create_dir_all(cfg.scenarios_dir());
    add(
        &mut checks,
        &mut ok,
        "folders",
        "pass",
        cfg.dot_dir().display().to_string(),
    );

    // 5. simulator.api_key[provider]
    let key = cfg.simulator.api_key.trim();
    let provider = cfg.simulator.provider.clone();
    if key.is_empty() {
        add(
            &mut checks,
            &mut ok,
            &format!("simulator.api_key[{provider}]"),
            "fail",
            format!("missing — `simulator.api_key` required for provider {provider}"),
        );
    } else if key.len() < 20 {
        add(
            &mut checks,
            &mut ok,
            &format!("simulator.api_key[{provider}]"),
            "warn",
            "Key looks unusually short".into(),
        );
    } else {
        add(
            &mut checks,
            &mut ok,
            &format!("simulator.api_key[{provider}]"),
            "pass",
            "present".into(),
        );
    }

    let mut m = Map::new();
    m.insert("ok".into(), json!(ok));
    m.insert("checks".into(), Json::Array(checks));
    Ok((m, Some(cfg)))
}

/// Append the telephony checks (position 7 — Python order: livekit.api at 6
/// comes from the connectivity caller, then telephony last).
pub fn append_telephony_checks(m: &mut Map<String, Json>, cfg: &crate::config::SimConfig) {
    fn add(checks: &mut Vec<Json>, ok: &mut bool, name: &str, status: &str, detail: String) {
        let mut c = Map::new();
        c.insert("name".into(), json!(name));
        c.insert("status".into(), json!(status));
        c.insert("detail".into(), json!(detail));
        if status == "fail" {
            *ok = false;
        }
        checks.push(Json::Object(c));
    }
    let mut ok = m.get("ok").and_then(|v| v.as_bool()).unwrap_or(false);
    let checks = m.get_mut("checks").and_then(|v| v.as_array_mut()).unwrap();
    let tel = &cfg.telephony;
    if tel.outbound_trunk_id.is_some() || tel.dial_in.is_some() || tel.sim_inbound_number.is_some()
    {
        let mut bits: Vec<String> = Vec::new();
        bits.push(format!(
            "outbound_trunk={}",
            if tel.outbound_trunk_id.is_some() {
                "set"
            } else {
                "missing"
            }
        ));
        bits.push(format!(
            "dial_in={}",
            if tel.dial_in.is_some() {
                "set"
            } else {
                "unset"
            }
        ));
        bits.push(format!(
            "sim_inbound={}",
            if tel.sim_inbound_number.is_some() {
                "set"
            } else {
                "unset"
            }
        ));
        add(
            checks,
            &mut ok,
            "telephony",
            if tel.outbound_trunk_id.is_some() {
                "pass"
            } else {
                "warn"
            },
            bits.join("; "),
        );
        if tel.outbound_trunk_id.is_some() && tel.sim_inbound_number.is_none() {
            add(
                checks,
                &mut ok,
                "telephony.outbound_sim_callee",
                "warn",
                "sim_inbound_number unset — outbound_sim_callee scenarios need a DID that \
                 hairpins into the sim-room (or Telephony.call_to per scenario). \
                 Dialing a real PSTN handset is outbound_human_pickup, not Gemini callee. \
                 See docs/telephony.md + docs/PROBLEM.md."
                    .into(),
            );
        } else if tel.sim_inbound_number.is_some() && tel.outbound_trunk_id.is_none() {
            add(
                checks,
                &mut ok,
                "telephony.outbound_sim_callee",
                "warn",
                "sim_inbound_number set but outbound_trunk_id missing — SIP dial cannot run."
                    .into(),
            );
        } else if tel.sim_inbound_number.is_some() && tel.outbound_trunk_id.is_some() {
            add(
                checks,
                &mut ok,
                "telephony.outbound_sim_callee",
                "warn",
                "trunk + sim_inbound_number present — ensure LiveKit dispatch rule routes \
                 this DID into the lks room (Cloud hairpin). Real PSTN ≠ Gemini without that rule."
                    .into(),
            );
        }
    } else {
        add(
            checks,
            &mut ok,
            "telephony",
            "pass",
            "not configured (WebRTC-only OK)".into(),
        );
    }
    m.insert("ok".into(), json!(ok));
}

/// Full preflight — the core checks plus the LiveKit API connectivity check
/// when `connectivity` is true. The connectivity check needs livekit_api,
/// which lks-core must not depend on — the lks-livekit `preflight::op_preflight`
/// wires this in. This fn keeps the core (non-connectivity) surface and
/// appends telephony (position 7) after the caller's livekit.api (position 6).
pub fn op_preflight(
    project_root: &Path,
    connectivity: bool,
    profile: Option<&str>,
) -> Result<Map<String, Json>, ConfigError> {
    let (mut m, cfg) = op_preflight_core(project_root, profile)?;
    let _ = connectivity; // livekit.api check lands via lks-livekit::preflight
    if let Some(cfg) = cfg {
        append_telephony_checks(&mut m, &cfg);
    }
    Ok(m)
}
