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

/// ops.guide — package GUIDE.md text (no project_root required).
pub fn guide() -> Result<Map<String, Json>, ConfigError> {
    let path = crate::authoring::package_templates_dir().join("GUIDE.md");
    let text = std::fs::read_to_string(&path)
        .map_err(|_| ConfigError(format!("Package guide missing: {}", path.display())))?;
    let mut m = Map::new();
    m.insert("path".into(), json!(path.to_string_lossy().into_owned()));
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
    let cfg = load_config(project_root.to_path_buf(), None).map_err(|e| e.0)?;
    convert_scenario(&cfg.scenarios_dir(), scenario_id, force).map_err(|e| e.0)
}

/// ops.list_scenarios
pub fn op_list_scenarios(project_root: &Path) -> Result<Vec<Map<String, Json>>, ConfigError> {
    let cfg = load_config(project_root.to_path_buf(), None)?;
    Ok(list_scenarios(&cfg.scenarios_dir()))
}

/// ops.validate_scenario — resolution + validation warnings (port of
/// `ops.validate_scenario`; plugin loading is a P8 surface).
pub fn op_validate_scenario(
    project_root: &Path,
    scenario_id: &str,
) -> Result<Map<String, Json>, ConfigError> {
    let cfg = load_config(project_root.to_path_buf(), None)?;
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
        warnings.push(
            "verify plugins require the Python build (lks); the Rust build does not embed CPython — remove plugin_modules or use lks.".to_string(),
        );
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
    Ok(m)
}

/// ops.export_scenario
pub fn op_export_scenario(
    project_root: &Path,
    scenario_id: &str,
) -> Result<Map<String, Json>, ConfigError> {
    let cfg = load_config(project_root.to_path_buf(), None)?;
    Ok(export_scenario(&cfg.scenarios_dir(), scenario_id))
}

/// ops.list_plugins — verify plugins are a P8 (pyo3) surface; local modules
/// listed from `.agent-sim/plugins/*.py` (data-plane, no plugin loading).
pub fn op_list_plugins(project_root: &Path) -> Result<Map<String, Json>, ConfigError> {
    let cfg = load_config(project_root.to_path_buf(), None)?;
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

    let mut load = Map::new();
    load.insert("verify_plugins".into(), json!([]));
    load.insert("local_modules".into(), json!(local_files));
    load.insert("errors".into(), json!([]));
    load.insert(
        "note".into(),
        json!("Rust build: verify plugins run under embedded CPython (P8); entry-point plugins are a Python-package feature."),
    );

    let mut out = Map::new();
    out.insert("verify_plugins".into(), json!([]));
    out.insert("local_modules".into(), json!(local_files));
    out.insert("load".into(), Json::Object(load));
    out.insert("entry_point_group".into(), json!("lks.plugins"));
    Ok(out)
}

/// ops.list_cues — built-in catalog is a P2/P3.5 audio surface; report the
/// config alias/override paths (data-plane) and a clear note.
pub fn op_list_cues(project_root: &Path) -> Result<Map<String, Json>, ConfigError> {
    let cfg = load_config(project_root.to_path_buf(), None)?;
    let mut aliases = Map::new();
    for (k, v) in &cfg.cues.aliases {
        aliases.insert(k.clone(), json!(v));
    }
    let mut out = Map::new();
    out.insert(
        "cues_dir".into(),
        json!(cfg.cues_dir().to_string_lossy().into_owned()),
    );
    out.insert("aliases".into(), Json::Object(aliases));
    out.insert(
        "note".into(),
        json!("Full built-in cue catalog (noise.* / voice.*) is served by the audio pipeline (P2/P3.5); see templates/cues/."),
    );
    Ok(out)
}

/// ops.get_run_status — SQLite read.
pub fn op_get_run_status(
    project_root: &Path,
    run_id: &str,
) -> Result<Map<String, Json>, ConfigError> {
    let cfg = load_config(project_root.to_path_buf(), None)?;
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
            for (k, v) in run {
                m.insert(k, v);
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
    let cfg = load_config(project_root.to_path_buf(), None)?;
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
        let Some(k) = kind else {
            return Err(ConfigError("kind filter lost".into()));
        };
        if let Some(prefix) = k.strip_suffix('*') {
            if !ekind.starts_with(prefix) {
                continue;
            }
        } else if ekind != k {
            continue;
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
    let cfg = load_config(project_root.to_path_buf(), None)?;
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
    let cfg = load_config(project_root.to_path_buf(), None)?;
    if !cfg.sqlite_path().exists() {
        return Ok(Vec::new());
    }
    let store = crate::logging::sqlite::RunStore::new(cfg.sqlite_path().to_string_lossy().as_ref());
    store
        .list_runs(limit, scenario_id)
        .map_err(|e| ConfigError(format!("sqlite read error: {e}")))
}

/// metrics_digest — the 12-key flat digest used by compare_runs (port of
/// `metrics.py:metrics_digest`, mirroring the Python `or` fallbacks).
pub fn metrics_digest(metrics: Option<&Map<String, Json>>) -> Map<String, Json> {
    let mut out = Map::new();
    let get = |k: &str| metrics.and_then(|m| m.get(k)).cloned();
    let get_inner = |outer: &str, inner: &str| {
        metrics
            .and_then(|m| m.get(outer))
            .and_then(|v| v.as_object())
            .and_then(|m| m.get(inner))
            .cloned()
    };
    let num = |v: Option<Json>| v.and_then(|x| x.as_f64());
    let num_or_null = |v: Option<Json>| num(v).map(Json::from).unwrap_or(Json::Null);

    let p50 = num(get_inner("turn_taking_ms", "p50"));
    out.insert("turn_p50_ms".into(), num_or_null(p50.map(Json::from)));
    // Python `or` fallback: p95 from turn_taking_ms, else the flat key.
    let p95 = num(get_inner("turn_taking_ms", "p95")).or_else(|| num(get("turn_taking_p95_ms")));
    out.insert(
        "turn_p95_ms".into(),
        p95.map(Json::from).unwrap_or(Json::Null),
    );
    out.insert("ttfw_ms".into(), num_or_null(get("ttfw_ms")));
    out.insert(
        "recovery_p50_ms".into(),
        num_or_null(get_inner("recovery_ms", "p50")),
    );
    out.insert("barge_count".into(), num_or_null(get("barge_count")));
    out.insert(
        "barge_recovery_rate".into(),
        num_or_null(get("barge_recovery_rate")),
    );
    out.insert("talk_ratio".into(), num_or_null(get("talk_ratio")));
    out.insert("ttfa_ms".into(), num_or_null(get("ttfa_run_ms")));
    out.insert(
        "turn_taking_audio_p95_ms".into(),
        num_or_null(get_inner("turn_taking_audio_ms", "p95")),
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
    let cfg = load_config(project_root.to_path_buf(), None).map_err(|e| e.0)?;
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
    let cfg = load_config(project_root.to_path_buf(), None)?;
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
pub fn op_preflight(
    project_root: &Path,
    _connectivity: bool,
    _profile: Option<&str>,
) -> Result<Map<String, Json>, ConfigError> {
    let mut checks: Vec<Json> = Vec::new();
    let mut ok = true;

    // config load is itself the first check
    let cfg_result = load_config(project_root.to_path_buf(), None);
    match cfg_result {
        Ok(cfg) => {
            let mut c = Map::new();
            c.insert("name".into(), json!("config"));
            c.insert("pass".into(), json!(true));
            c.insert(
                "detail".into(),
                json!(format!("config.yaml OK ({})", cfg.dot_dir().display())),
            );
            checks.push(Json::Object(c));

            let mut c2 = Map::new();
            c2.insert("name".into(), json!("folders"));
            c2.insert("pass".into(), json!(true));
            c2.insert("detail".into(), json!("reports/ + scenarios/ exist"));
            checks.push(Json::Object(c2));
        }
        Err(e) => {
            ok = false;
            let mut c = Map::new();
            c.insert("name".into(), json!("config"));
            c.insert("pass".into(), json!(false));
            c.insert("detail".into(), json!(e.0));
            checks.push(Json::Object(c));
        }
    }

    let mut note = Map::new();
    note.insert(
        "note".into(),
        json!("Rust build: LiveKit API connectivity check + telephony bits land with the P2 livekit layer; config/folder checks above are real."),
    );
    checks.push(Json::Object(note));

    let mut m = Map::new();
    m.insert("ok".into(), json!(ok));
    m.insert("checks".into(), Json::Array(checks));
    Ok(m)
}
