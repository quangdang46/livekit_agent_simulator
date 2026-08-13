//! Web report player (P3.5 minimal slice): serve the built SPA from
//! `web/dist` (walk up ≤6 parents like the Python `_player_dir`) plus a
//! `/api/runs` JSON endpoint derived from report dirs + summary.json.
//!
//! Mirrors the Python report server's key routes:
//! - GET `/` → index.html (no-store)
//! - GET `/assets/<name>` → static files
//! - GET `/api/runs` → run list, newest first, `{run_id, scenario_id, status,
//!   duration_ms, turn_count, tool_count, has_audio, started_utc, mtime_ms}`
//! - GET `/api/runs/<id>/cues` → minimal cues payload (transcripts + markers)
//! - GET `/runs/<id>/<name>` → file bytes (audio etc.)

use std::path::{Path, PathBuf};
use std::sync::Arc;

use axum::extract::Path as AxumPath;
use axum::http::{header, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use axum::Router;
use serde_json::{json, Value};

pub struct WebServer {
    pub player_dir: PathBuf,
    pub reports_dir: PathBuf,
}

/// Locate the built SPA: walk up ≤6 parents looking for `web/dist/index.html`.
pub fn resolve_player_dir() -> PathBuf {
    let mut p = std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
    for _ in 0..6 {
        let cand = p.join("web").join("dist");
        if cand.join("index.html").exists() {
            return cand;
        }
        if !p.pop() {
            break;
        }
    }
    PathBuf::from("web/dist")
}

impl WebServer {
    pub fn new(project_root: &Path) -> Self {
        let cfg = lks_core::config::load_config(project_root.to_path_buf(), None)
            .map(|c| c.reports_dir())
            .unwrap_or_else(|_| project_root.join(".agent-sim/reports"));
        Self {
            player_dir: resolve_player_dir(),
            reports_dir: cfg,
        }
    }

    /// Build the run list (newest first) from report dirs + summary.json.
    fn list_runs(&self) -> Vec<Value> {
        let mut runs: Vec<Value> = Vec::new();
        let Ok(rd) = std::fs::read_dir(&self.reports_dir) else {
            return runs;
        };
        for entry in rd.flatten() {
            let dir = entry.path();
            if !dir.is_dir() || !dir.join("events.jsonl").exists() {
                continue;
            }
            let run_id = dir
                .file_name()
                .map(|n| n.to_string_lossy().into_owned())
                .unwrap_or_default();
            let summary: Value = std::fs::read_to_string(dir.join("summary.json"))
                .ok()
                .and_then(|t| serde_json::from_str(&t).ok())
                .unwrap_or(Value::Null);
            let meta: Value = std::fs::read_to_string(dir.join("meta.json"))
                .ok()
                .and_then(|t| serde_json::from_str(&t).ok())
                .unwrap_or(Value::Null);
            let so = summary.as_object().cloned().unwrap_or_default();
            let mo = meta.as_object().cloned().unwrap_or_default();
            let status = so
                .get("status")
                .cloned()
                .or_else(|| mo.get("status").cloned())
                .unwrap_or(Value::Null);
            let scenario_id = so
                .get("scenario_id")
                .cloned()
                .or_else(|| mo.get("scenario_id").cloned())
                .unwrap_or(Value::Null);
            let duration = so.get("duration_ms").cloned().unwrap_or(Value::Null);
            let turn_count = so.get("turn_count").cloned().unwrap_or(Value::Null);
            let tool_count = so
                .get("tool_calls")
                .cloned()
                .or_else(|| {
                    so.get("metrics")
                        .and_then(|m| m.as_object())
                        .and_then(|m| m.get("tool_calls"))
                        .cloned()
                })
                .unwrap_or(Value::Null);
            let started = so
                .get("started_utc")
                .cloned()
                .or_else(|| mo.get("started_utc").cloned())
                .unwrap_or(Value::Null);
            let mtime = entry
                .metadata()
                .ok()
                .and_then(|m| m.modified().ok())
                .map(|t| {
                    t.duration_since(std::time::UNIX_EPOCH)
                        .map(|d| d.as_millis() as i64)
                        .unwrap_or(0)
                })
                .unwrap_or(0);
            let has_audio = dir.join("conversation.wav").exists();
            runs.push(json!({
                "run_id": run_id,
                "scenario_id": scenario_id,
                "status": status,
                "duration_ms": duration,
                "turn_count": turn_count,
                "tool_count": tool_count,
                "has_audio": has_audio,
                "started_utc": started,
                "mtime_ms": mtime,
            }));
        }
        runs.sort_by(|a, b| {
            let ka = a["mtime_ms"].as_i64().unwrap_or(0);
            let kb = b["mtime_ms"].as_i64().unwrap_or(0);
            kb.cmp(&ka)
                .then_with(|| a["run_id"].as_str().cmp(&b["run_id"].as_str()))
        });
        runs
    }

    /// Minimal cues payload: transcript finals + markers from events.jsonl.
    fn cues_for_run(&self, run_id: &str) -> Option<Value> {
        let dir = self.reports_dir.join(run_id);
        let events_path = dir.join("events.jsonl");
        if !events_path.exists() {
            return None;
        }
        let mut cues: Vec<Value> = Vec::new();
        let mut markers: Vec<Value> = Vec::new();
        let Ok(text) = std::fs::read_to_string(&events_path) else {
            return None;
        };
        for line in text.lines() {
            let line = line.trim();
            if line.is_empty() {
                continue;
            }
            let Ok(e) = serde_json::from_str::<Value>(line) else {
                continue;
            };
            let kind = e.get("kind").and_then(|v| v.as_str()).unwrap_or("");
            let spec = e
                .get("spec")
                .as_ref()
                .and_then(|v| v.as_object())
                .cloned()
                .unwrap_or_default();
            let mono = e.get("ts_mono_ms").and_then(|v| v.as_i64()).unwrap_or(0);
            if kind == "transcript.user.final" || kind == "transcript.agent.final" {
                let role = if kind.contains("agent") {
                    "agent"
                } else {
                    "user"
                };
                let text = spec
                    .get("text")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string();
                cues.push(json!({
                    "role": role,
                    "final_ms": mono,
                    "text": text,
                    "source": e.get("source").cloned().unwrap_or(Value::Null),
                    "turn": e.get("turn").cloned().unwrap_or(Value::Null),
                }));
            }
            if kind == "sim.script.cue" {
                markers.push(json!({
                    "type": if spec.get("barge_in").and_then(|v| v.as_bool()).unwrap_or(false) { "barge_in" } else { "script_cue" },
                    "ms": mono,
                    "label": spec.get("label").cloned().unwrap_or(Value::Null),
                    "say": spec.get("say").cloned().unwrap_or(Value::Null),
                }));
            }
            if kind == "interruption" {
                markers.push(json!({
                    "type": "interruption",
                    "ms": mono,
                    "label": spec.get("class").cloned().unwrap_or(Value::Null),
                }));
            }
        }
        // ---- Dedupe + ghost-STT filter (parity with web/transcript_cues.py) ----
        cues = dedupe_cues(cues);
        cues = ghost_filter(cues);

        // ---- Dedupe + ghost-STT filter (parity with web/transcript_cues.py) ----
        cues = dedupe_cues(cues);
        cues = ghost_filter(cues);

        let summary: Value = std::fs::read_to_string(dir.join("summary.json"))
            .ok()
            .and_then(|t| serde_json::from_str(&t).ok())
            .unwrap_or(Value::Null);
        let meta: Value = std::fs::read_to_string(dir.join("meta.json"))
            .ok()
            .and_then(|t| serde_json::from_str(&t).ok())
            .unwrap_or(Value::Null);
        Some(json!({
            "run_id": run_id,
            "scenario_id": meta.get("scenario_id").cloned().or_else(|| summary.get("scenario_id").cloned()).unwrap_or(Value::Null),
            "audio": {
                "file": if dir.join("conversation.wav").exists() { json!("conversation.wav") } else { Value::Null },
                "duration_ms": Value::Null,
                "t0_mono_ms": 0,
                "channels": {"left": "sim", "right": "agent"},
            },
            "cues": cues,
            "markers": markers,
            "marker_counts": {},
            "script_verify": summary.get("script_verify").cloned().unwrap_or(Value::Null),
            "assert_verify": summary.get("assert_verify").cloned().unwrap_or(Value::Null),
            "tool_events": [],
            "tool_summary": {"tool_count": summary.get("tool_calls").cloned().unwrap_or(json!(0)), "tool_errors": summary.get("tool_errors").cloned().unwrap_or(json!(0))},
            "observe_gaps": [],
        }))
    }
}

/// Build the axum router.
pub fn router(server: Arc<WebServer>) -> Router {
    Router::new()
        .route("/", get(index))
        .route("/index.html", get(index))
        .route("/api/runs", get(api_runs))
        .route("/api/runs/{run_id}/cues", get(api_cues))
        .route("/assets/{name}", get(static_asset))
        .route("/runs/{run_id}/{name}", get(run_file))
        .with_state(server)
}

type S = Arc<WebServer>;

async fn index(axum::extract::State(s): axum::extract::State<S>) -> Response {
    let path = s.player_dir.join("index.html");
    match std::fs::read(&path) {
        Ok(bytes) => (
            StatusCode::OK,
            [
                (header::CONTENT_TYPE, "text/html; charset=utf-8"),
                (header::CACHE_CONTROL, "no-store"),
            ],
            bytes,
        )
            .into_response(),
        Err(_) => (StatusCode::NOT_FOUND, "missing index.html").into_response(),
    }
}

async fn api_runs(axum::extract::State(s): axum::extract::State<S>) -> Response {
    let runs = s.list_runs();
    let body = serde_json::to_string(&runs).unwrap_or_else(|_| "[]".into());
    (
        StatusCode::OK,
        [
            (header::CONTENT_TYPE, "application/json; charset=utf-8"),
            (header::CACHE_CONTROL, "no-store"),
        ],
        body,
    )
        .into_response()
}

async fn api_cues(
    axum::extract::State(s): axum::extract::State<S>,
    AxumPath(run_id): AxumPath<String>,
) -> Response {
    match s.cues_for_run(&run_id) {
        Some(cues) => (
            StatusCode::OK,
            [
                (header::CONTENT_TYPE, "application/json; charset=utf-8"),
                (header::CACHE_CONTROL, "no-store"),
            ],
            serde_json::to_string(&cues).unwrap_or_default(),
        )
            .into_response(),
        None => (StatusCode::NOT_FOUND, "run not found").into_response(),
    }
}

async fn static_asset(
    axum::extract::State(s): axum::extract::State<S>,
    AxumPath(name): AxumPath<String>,
) -> Response {
    let path = s.player_dir.join(&name);
    if !path.starts_with(&s.player_dir) || !path.is_file() {
        return (StatusCode::NOT_FOUND, "asset not found").into_response();
    }
    let ctype = guess_type(&name);
    match std::fs::read(&path) {
        Ok(bytes) => (
            StatusCode::OK,
            [
                (header::CONTENT_TYPE, ctype),
                (header::CACHE_CONTROL, "no-store"),
            ],
            bytes,
        )
            .into_response(),
        Err(_) => (StatusCode::NOT_FOUND, "asset not found").into_response(),
    }
}

async fn run_file(
    axum::extract::State(s): axum::extract::State<S>,
    AxumPath((run_id, name)): AxumPath<(String, String)>,
) -> Response {
    let run_dir = s.reports_dir.join(&run_id);
    if !run_dir.is_dir() {
        return (StatusCode::NOT_FOUND, "run not found").into_response();
    }
    if name == "cues.json" {
        return match s.cues_for_run(&run_id) {
            Some(cues) => (
                StatusCode::OK,
                [
                    (header::CONTENT_TYPE, "application/json; charset=utf-8"),
                    (header::CACHE_CONTROL, "no-store"),
                ],
                serde_json::to_string(&cues).unwrap_or_default(),
            )
                .into_response(),
            None => (StatusCode::NOT_FOUND, "run not found").into_response(),
        };
    }
    let path = run_dir.join(&name);
    if !path.starts_with(&run_dir) || !path.is_file() {
        return (StatusCode::NOT_FOUND, "file not found").into_response();
    }
    let ctype = guess_type(&name);
    match std::fs::read(&path) {
        Ok(bytes) => (
            StatusCode::OK,
            [
                (header::CONTENT_TYPE, ctype),
                (header::CACHE_CONTROL, "no-store"),
            ],
            bytes,
        )
            .into_response(),
        Err(_) => (StatusCode::NOT_FOUND, "file not found").into_response(),
    }
}

fn guess_type(name: &str) -> &'static str {
    if name.ends_with(".js") {
        "text/javascript; charset=utf-8"
    } else if name.ends_with(".css") {
        "text/css; charset=utf-8"
    } else if name.ends_with(".html") {
        "text/html; charset=utf-8"
    } else if name.ends_with(".json") {
        "application/json; charset=utf-8"
    } else if name.ends_with(".svg") {
        "image/svg+xml"
    } else if name.ends_with(".wav") {
        "audio/wav"
    } else {
        "application/octet-stream"
    }
}

/// Start the report server on host:port; returns the bound address.
pub async fn serve(
    server: Arc<WebServer>,
    host: &str,
    port: u16,
) -> anyhow::Result<std::net::SocketAddr> {
    let app = router(server);
    let listener = tokio::net::TcpListener::bind((host, port)).await?;
    let addr = listener.local_addr()?;
    axum::serve(listener, app).await?;
    Ok(addr)
}

// ---------------------------------------------------------------------------
// Transcript dedupe + ghost-STT filter (port of web/transcript_cues.py core).
// ---------------------------------------------------------------------------

fn norm_text(s: &str) -> String {
    s.to_lowercase()
        .chars()
        .filter(|c| c.is_alphanumeric() || c.is_whitespace())
        .collect::<String>()
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
}

fn texts_similar(a: &str, b: &str) -> bool {
    let na = norm_text(a);
    let nb = norm_text(b);
    if na.is_empty() || nb.is_empty() {
        return false;
    }
    if na == nb || na.contains(&nb) || nb.contains(&na) {
        return true;
    }
    // Token-canonical overlap (aliases okey→okay etc.) — subset check.
    let ta: Vec<&str> = na.split(' ').collect();
    let tb: Vec<&str> = nb.split(' ').collect();
    let min_len = ta.len().min(tb.len());
    let inter = ta.iter().filter(|t| tb.contains(t)).count();
    inter >= 1.max(min_len.div_ceil(2))
}

fn source_rank(source: Option<&str>, role: &str) -> i64 {
    let s = source.unwrap_or("");
    match (role, s) {
        ("user", "sim.gemini") | ("user", "sim.openai") => 0,
        ("user", "data") => 2,
        ("user", "lk.transcription") => 3,
        ("agent", "data") => 0,
        ("agent", "lk.transcription") => 1,
        ("agent", "sim.gemini") | ("agent", "sim.openai") => 2,
        _ => 9,
    }
}

fn dedupe_cues(cues: Vec<Value>) -> Vec<Value> {
    let mut sorted = cues.clone();
    sorted.sort_by(|a, b| {
        let ka = a["final_ms"].as_i64().unwrap_or(0);
        let kb = b["final_ms"].as_i64().unwrap_or(0);
        ka.cmp(&kb)
    });
    let mut out: Vec<Value> = Vec::new();
    for c in sorted {
        let role = c["role"].as_str().unwrap_or("").to_string();
        let text = c["text"].as_str().unwrap_or("").to_string();
        let cur_src = c["source"].as_str().unwrap_or("").to_string();
        let mut replaced = false;
        for i in (0..out.len()).rev() {
            let prev = &out[i];
            if prev["role"].as_str() != Some(role.as_str()) {
                continue;
            }
            let delta = (prev["final_ms"].as_i64().unwrap_or(0)
                - c["final_ms"].as_i64().unwrap_or(0))
            .abs();
            let similar = texts_similar(prev["text"].as_str().unwrap_or(""), &text);
            let prev_src = prev["source"].as_str().unwrap_or("").to_string();
            let cross = !prev_src.is_empty() && !cur_src.is_empty() && prev_src != cur_src;
            let max_delta = if similar && cross {
                if role == "user" {
                    15000
                } else {
                    6000
                }
            } else if similar {
                4000
            } else {
                2500
            };
            if delta > max_delta {
                break;
            }
            if !similar {
                continue;
            }
            let pr = source_rank(Some(&prev_src), &role);
            let cr = source_rank(Some(&cur_src), &role);
            if cr < pr || (cr == pr && text.len() > prev["text"].as_str().unwrap_or("").len()) {
                out[i] = c.clone();
            }
            replaced = true;
            break;
        }
        if !replaced {
            out.push(c);
        }
    }
    out
}

fn ghost_filter(cues: Vec<Value>) -> Vec<Value> {
    let has_provider = cues.iter().any(|c| {
        c["role"].as_str() == Some("user")
            && matches!(
                c["source"].as_str(),
                Some("sim.gemini") | Some("sim.openai")
            )
    });
    if !has_provider {
        return cues;
    }
    let provider_ms: Vec<i64> = cues
        .iter()
        .filter(|c| {
            c["role"].as_str() == Some("user")
                && matches!(
                    c["source"].as_str(),
                    Some("sim.gemini") | Some("sim.openai")
                )
        })
        .filter_map(|c| c["final_ms"].as_i64())
        .collect();
    let provider_texts: Vec<String> = cues
        .iter()
        .filter(|c| {
            c["role"].as_str() == Some("user")
                && matches!(
                    c["source"].as_str(),
                    Some("sim.gemini") | Some("sim.openai")
                )
        })
        .filter_map(|c| c["text"].as_str().map(String::from))
        .collect();
    cues.into_iter()
        .filter(|c| {
            if c["role"].as_str() != Some("user") {
                return true;
            }
            let src = c["source"].as_str().unwrap_or("");
            if matches!(src, "sim.gemini" | "sim.openai") {
                return true;
            }
            if src != "lk.transcription" && !src.contains("transcript") {
                return true;
            }
            // Ghost iff NOT similar to a near provider user final (±2.5s).
            let fm = c["final_ms"].as_i64().unwrap_or(0);
            let text = c["text"].as_str().unwrap_or("");
            let near_ms: Vec<i64> = provider_ms
                .iter()
                .copied()
                .filter(|m| (*m - fm).abs() <= 2500)
                .collect();
            if near_ms.is_empty() {
                return true;
            }
            // Similar to ANY near provider final → keep (it's the same utterance).
            let text_owned = text.to_string();
            let near_texts: Vec<String> = provider_texts
                .iter()
                .zip(provider_ms.iter())
                .filter(|(_, m)| (**m - fm).abs() <= 2500)
                .map(|(t, _)| t.clone())
                .collect();
            near_texts.iter().any(|t| texts_similar(&text_owned, t))
        })
        .collect()
}
