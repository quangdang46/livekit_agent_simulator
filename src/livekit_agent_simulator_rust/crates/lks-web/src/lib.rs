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

    /// Cues payload: transcript finals + behavior markers from events.jsonl,
    /// time-aligned to the conversation audio via `meta.audio.t0_mono_ms`.
    ///
    /// Markers/cues carry `start_ms`/`end_ms` in audio-relative ms so the SPA
    /// can interleave them chronologically (parity with Python `web/cues.py`).
    fn cues_for_run(&self, run_id: &str) -> Option<Value> {
        let dir = self.reports_dir.join(run_id);
        let events_path = dir.join("events.jsonl");
        if !events_path.exists() {
            return None;
        }
        let meta: Value = std::fs::read_to_string(dir.join("meta.json"))
            .ok()
            .and_then(|t| serde_json::from_str(&t).ok())
            .unwrap_or(Value::Null);
        let summary: Value = std::fs::read_to_string(dir.join("summary.json"))
            .ok()
            .and_then(|t| serde_json::from_str(&t).ok())
            .unwrap_or(Value::Null);
        let t0 = resolve_audio_t0_ms(&meta, &events_path);
        let duration_ms = wav_duration_ms(&dir.join("conversation.wav"));
        // Audio-relative milliseconds: audio_ms = ts_mono_ms - t0. Events beyond
        // 2 s past the audio end are dropped (parity with `_mono_to_audio_ms`).
        let audio_ms = |mono: i64| -> Option<i64> {
            let start = mono.saturating_sub(t0).max(0);
            if let Some(d) = duration_ms {
                if start > d + 2000 {
                    return None;
                }
            }
            Some(start)
        };
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
            let Some(start_ms) = audio_ms(mono) else {
                continue;
            };
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
                let est = estimate_utterance_ms(&text, role);
                let c_start = start_ms.saturating_sub(est).max(0);
                let c_end = start_ms + 350;
                cues.push(json!({
                    "role": role,
                    "final_ms": start_ms,
                    "start_ms": c_start,
                    "end_ms": c_end,
                    "text": text,
                    "source": e.get("source").cloned().unwrap_or(Value::Null),
                    "turn": e.get("turn").cloned().unwrap_or(Value::Null),
                }));
            }
            if kind == "sim.script.cue" {
                let barge = spec
                    .get("barge_in")
                    .and_then(|v| v.as_bool())
                    .unwrap_or(false);
                let during = spec
                    .get("during_agent_speech")
                    .and_then(|v| v.as_bool())
                    .unwrap_or(false);
                let span = if barge {
                    if during {
                        2200
                    } else {
                        1400
                    }
                } else {
                    1000
                };
                markers.push(json!({
                    "type": if barge { "barge_in" } else { "script_cue" },
                    "start_ms": start_ms,
                    "end_ms": clamp_end(start_ms, start_ms + span, duration_ms),
                    "ms": mono,
                    "label": spec.get("label").cloned().unwrap_or(Value::Null),
                    "say": spec.get("say").cloned().unwrap_or(Value::Null),
                    "during_agent_speech": during,
                    "barge_in": barge,
                }));
            }
            if kind == "sim.script.wait" {
                let waited = spec.get("waited_ms").and_then(|v| v.as_i64()).unwrap_or(0);
                let span = if waited > 0 { waited } else { 1500 };
                let win_start = start_ms.saturating_sub(span).max(0);
                markers.push(json!({
                    "type": "silence_wait",
                    "start_ms": win_start,
                    "end_ms": clamp_end(win_start, start_ms + 200, duration_ms),
                    "ms": mono,
                    "label": spec.get("label").cloned().or_else(|| spec.get("step_id").cloned()).unwrap_or(json!("user pause")),
                }));
            }
            if kind == "silence.detected" {
                let dur = spec
                    .get("duration_ms")
                    .and_then(|v| v.as_i64())
                    .unwrap_or(0);
                let span = if dur > 0 { dur } else { 4000 };
                let win_start = start_ms.saturating_sub(span).max(0);
                markers.push(json!({
                    "type": "silence",
                    "start_ms": win_start,
                    "end_ms": clamp_end(win_start, start_ms, duration_ms),
                    "ms": mono,
                    "label": "silence detected",
                    "duration_ms": span,
                }));
            }
            if kind == "interruption" {
                markers.push(json!({
                    "type": "interruption",
                    "start_ms": start_ms,
                    "end_ms": clamp_end(start_ms, start_ms + 500, duration_ms),
                    "ms": mono,
                    "label": format!("interruption ({})", spec.get("by").and_then(|v| v.as_str()).unwrap_or("unknown")),
                }));
            }
            if kind == "sim.agent.audio_onset" {
                markers.push(json!({
                    "type": "audio_onset",
                    "start_ms": start_ms,
                    "end_ms": clamp_end(start_ms, start_ms + 300, duration_ms),
                    "ms": mono,
                    "label": "agent audio onset",
                }));
            }
            if kind == "sim.caller.audio_source_start" {
                markers.push(json!({
                    "type": "user_audio_source",
                    "start_ms": start_ms,
                    "end_ms": clamp_end(start_ms, start_ms + 300, duration_ms),
                    "ms": mono,
                    "label": "caller audio source",
                }));
            }
        }
        // ---- Dedupe + ghost-STT filter (parity with web/transcript_cues.py) ----
        cues = dedupe_cues(cues);
        cues = ghost_filter(cues);
        // ---- Speech-origin tagging (parity with web/speech_origin.py core) ----
        let markers_for_tag: Vec<(i64, String)> = markers
            .iter()
            .filter(|m| {
                m["type"].as_str() == Some("script_cue") || m["type"].as_str() == Some("barge_in")
            })
            .map(|m| {
                (
                    m["ms"].as_i64().unwrap_or(0),
                    m.get("say")
                        .and_then(|s| s.as_str())
                        .unwrap_or("")
                        .to_string(),
                )
            })
            .collect();
        for c in cues.iter_mut() {
            if c["role"].as_str() == Some("user") {
                let fm = c["final_ms"].as_i64().unwrap_or(0);
                let text = c["text"].as_str().unwrap_or("").to_string();
                let best = markers_for_tag
                    .iter()
                    .filter(|(m, say)| {
                        let delta = fm - m;
                        (-800..=15000).contains(&delta)
                            && !say.is_empty()
                            && texts_similar(&text, say)
                    })
                    .min_by_key(|(m, _)| fm - m);
                if let Some((_, say)) = best {
                    let origin = if say.starts_with('[') {
                        "script_cue"
                    } else {
                        "script_barge"
                    };
                    c["speech_origin"] = json!(origin);
                } else {
                    c["speech_origin"] = json!("natural");
                }
            } else {
                c["speech_origin"] = json!("natural");
            }
        }

        // ---- Dedupe + ghost-STT filter (parity with web/transcript_cues.py) ----
        cues = dedupe_cues(cues);
        cues = ghost_filter(cues);
        // ---- Speech-origin tagging (parity with web/speech_origin.py core) ----
        let markers_for_tag: Vec<(i64, String)> = markers
            .iter()
            .filter(|m| {
                m["type"].as_str() == Some("script_cue") || m["type"].as_str() == Some("barge_in")
            })
            .map(|m| {
                (
                    m["ms"].as_i64().unwrap_or(0),
                    m.get("say")
                        .and_then(|s| s.as_str())
                        .unwrap_or("")
                        .to_string(),
                )
            })
            .collect();
        for c in cues.iter_mut() {
            if c["role"].as_str() == Some("user") {
                let fm = c["final_ms"].as_i64().unwrap_or(0);
                let text = c["text"].as_str().unwrap_or("").to_string();
                let best = markers_for_tag
                    .iter()
                    .filter(|(m, say)| {
                        let delta = fm - m;
                        (-800..=15000).contains(&delta)
                            && !say.is_empty()
                            && texts_similar(&text, say)
                    })
                    .min_by_key(|(m, _)| fm - m);
                if let Some((_, say)) = best {
                    let origin = if say.starts_with('[') {
                        "script_cue"
                    } else {
                        "script_barge"
                    };
                    c["speech_origin"] = json!(origin);
                } else {
                    c["speech_origin"] = json!("natural");
                }
            } else {
                c["speech_origin"] = json!("natural");
            }
        }

        Some(json!({
            "run_id": run_id,
            "scenario_id": meta.get("scenario_id").cloned().or_else(|| summary.get("scenario_id").cloned()).unwrap_or(Value::Null),
            "audio": {
                "file": if dir.join("conversation.wav").exists() { json!("conversation.wav") } else { Value::Null },
                "duration_ms": duration_ms.map(|d| json!(d)).unwrap_or(Value::Null),
                "t0_mono_ms": t0,
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
// Report-time helpers (port of web/report_time.py + web/cue_helpers/windows.py).
// ---------------------------------------------------------------------------

/// Audio length of a WAV in ms (`int(data_bytes * 1000 / byte_rate)`); None on
/// missing/corrupt header. Mirrors Python `wave.getnframes()*1000/getframerate()`
/// where frames = data_bytes / block_align and byte_rate = block_align * rate.
fn wav_duration_ms(path: &Path) -> Option<i64> {
    let bytes = std::fs::read(path).ok()?;
    if bytes.len() < 44 || &bytes[0..4] != b"RIFF" || &bytes[8..12] != b"WAVE" {
        return None;
    }
    let mut fmt: Option<(u32, u32)> = None; // (byte_rate, block_align)
    let mut data_size: Option<u32> = None;
    let mut off = 12usize;
    while off + 8 <= bytes.len() {
        let id = &bytes[off..off + 4];
        let size = u32::from_le_bytes(bytes[off + 4..off + 8].try_into().ok()?) as usize;
        if id == b"fmt " && off + 8 + 16 <= bytes.len() {
            // u16 audio_format, u16 num_channels, u32 sample_rate, u32 byte_rate,
            // u16 block_align, u16 bits_per_sample
            let br = u32::from_le_bytes(bytes[off + 8 + 8..off + 8 + 12].try_into().ok()?);
            let ba = u16::from_le_bytes(bytes[off + 8 + 12..off + 8 + 14].try_into().ok()?);
            fmt = Some((br, ba as u32));
        } else if id == b"data" {
            data_size = Some(u32::from_le_bytes(bytes[off + 4..off + 8].try_into().ok()?));
        }
        off += 8 + size + (size & 1);
    }
    let (byte_rate, _block_align) = fmt?;
    let data_bytes = data_size?;
    if byte_rate > 0 {
        return Some(data_bytes as i64 * 1000 / byte_rate as i64);
    }
    None
}

/// Audio t0 (mono → audio offset): `meta.audio.t0_mono_ms`, else the first
/// transcript-ish event's `ts_mono_ms`, else 0 (parity with `_resolve_audio_t0_ms`).
fn resolve_audio_t0_ms(meta: &Value, events_path: &Path) -> i64 {
    if let Some(t0) = meta
        .get("audio")
        .and_then(|a| a.get("t0_mono_ms"))
        .and_then(|v| v.as_i64())
    {
        return t0.max(0);
    }
    let Ok(text) = std::fs::read_to_string(events_path) else {
        return 0;
    };
    for line in text.lines() {
        let Ok(e) = serde_json::from_str::<Value>(line) else {
            continue;
        };
        let kind = e.get("kind").and_then(|v| v.as_str()).unwrap_or("");
        if kind.starts_with("transcript.")
            || matches!(kind, "sim.mic_published" | "sim.gemini_connected")
        {
            return e
                .get("ts_mono_ms")
                .and_then(|v| v.as_i64())
                .unwrap_or(0)
                .max(0);
        }
    }
    0
}

/// `max(start + 120, end)`, capped at duration when known.
fn clamp_end(start_ms: i64, end_ms: i64, duration_ms: Option<i64>) -> i64 {
    let end = (start_ms + 120).max(end_ms);
    match duration_ms {
        Some(d) => end.min((start_ms + 120).max(d)),
        None => end,
    }
}

/// Estimated utterance length in ms (agent 95 ms/word, user 85 ms/word; empty
/// 800/600; clamps mirror Python).
fn estimate_utterance_ms(text: &str, role: &str) -> i64 {
    let t = text.trim();
    if t.is_empty() {
        return if role == "agent" { 800 } else { 600 };
    }
    let words = t
        .replace('\n', " ")
        .split(' ')
        .filter(|w| !w.is_empty())
        .count() as i64;
    let units = words.max((t.len() as i64) / 4);
    let ms = units * if role == "agent" { 95 } else { 85 };
    if role == "agent" {
        ms.clamp(700, 22_000)
    } else {
        ms.clamp(500, 14_000)
    }
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

#[cfg(test)]
mod tests {
    use super::*;

    /// Build a minimal report dir with events + meta and assert markers/cues
    /// carry `start_ms`/`end_ms` (the SPA interleaves by those — a missing
    /// field previously sank every marker below the conversation).
    #[test]
    fn cues_for_run_emits_start_end_on_markers_and_cues() {
        let tmp = std::env::temp_dir().join(format!("lksweb-test-{}", std::process::id()));
        let dir = tmp.join("run-1");
        std::fs::create_dir_all(&dir).unwrap();
        let t0 = 6515i64;
        let events = [
            format!(r#"{{"kind":"run.started","ts_mono_ms":0,"spec":{{}}}}"#,),
            format!(r#"{{"kind":"sim.mic_published","ts_mono_ms":5000,"spec":{{}}}}"#,),
            format!(
                r#"{{"kind":"transcript.agent.final","ts_mono_ms":{t},"source":"lk.transcription","spec":{{"text":"Hello there"}}}}"#,
                t = t0 + 10_500
            ),
            format!(
                r#"{{"kind":"transcript.user.final","ts_mono_ms":{t},"source":"sim.gemini","spec":{{"text":"Hi, I need help"}}}}"#,
                t = t0 + 13_400
            ),
            format!(
                r#"{{"kind":"sim.agent.audio_onset","ts_mono_ms":{t},"source":"sim","spec":{{"onset_frame_idx":48000}}}}"#,
                t = t0 + 8000
            ),
            format!(
                r#"{{"kind":"sim.caller.audio_source_start","ts_mono_ms":{t},"source":"sim","spec":{{"provider":"gemini"}}}}"#,
                t = t0 + 12_000
            ),
        ];
        std::fs::write(dir.join("events.jsonl"), events.join("\n")).unwrap();
        std::fs::write(
            dir.join("meta.json"),
            format!(r#"{{"run_id":"run-1","audio":{{"t0_mono_ms":{t0},"duration_ms":25000}}}}"#,),
        )
        .unwrap();

        let ws = WebServer {
            player_dir: PathBuf::from("."),
            reports_dir: tmp.clone(),
        };
        let payload = ws.cues_for_run("run-1").expect("payload");
        let cues = payload["cues"].as_array().expect("cues array");
        let markers = payload["markers"].as_array().expect("markers array");

        assert!(!cues.is_empty(), "expected cues");
        for c in cues {
            assert!(
                c.get("start_ms").and_then(|v| v.as_i64()).is_some(),
                "cue missing start_ms: {c}"
            );
            assert!(
                c.get("end_ms").and_then(|v| v.as_i64()).is_some(),
                "cue missing end_ms: {c}"
            );
            assert!(
                c.get("final_ms").and_then(|v| v.as_i64()).is_some(),
                "cue missing final_ms: {c}"
            );
        }
        assert!(!markers.is_empty(), "expected markers");
        for m in markers {
            assert!(
                m.get("start_ms").and_then(|v| v.as_i64()).is_some(),
                "marker missing start_ms: {m}"
            );
            assert!(
                m.get("end_ms").and_then(|v| v.as_i64()).is_some(),
                "marker missing end_ms: {m}"
            );
        }
        assert_eq!(payload["audio"]["t0_mono_ms"].as_i64(), Some(t0));

        // Every marker start_ms must be < its end_ms, and all must be audio-relative.
        for m in markers {
            let s = m["start_ms"].as_i64().unwrap();
            let e = m["end_ms"].as_i64().unwrap();
            assert!(e >= s, "marker end before start: {m}");
            assert!(s >= 0, "negative marker start: {m}");
        }
        // audio_onset must be present (was previously never emitted).
        assert!(
            markers
                .iter()
                .any(|m| m["type"].as_str() == Some("audio_onset")),
            "audio_onset marker missing: {markers:?}"
        );
        let _ = std::fs::remove_dir_all(&tmp);
    }

    /// Real-world regression: the voice-ai-agent run with 149 `audio_onset`
    /// markers must interleave them chronologically — NOT sink all markers
    /// below the conversation (the pre-fix bug: markers had no start_ms/end_ms).
    #[test]
    fn real_run_audio_onset_interleaves_with_cues() {
        let run = std::path::Path::new(
            "C:/Users/ADMIN/Documents/Projects/voice-ai-agent/.agent-sim/reports/\
             001-nikko-en-clone-debug-20260813-031857-4281",
        );
        if !run.join("events.jsonl").exists() {
            eprintln!("skipping: voice-ai-agent run not present");
            return;
        }
        let ws = WebServer {
            player_dir: PathBuf::from("."),
            reports_dir: run.parent().unwrap().to_path_buf(),
        };
        let payload = ws
            .cues_for_run("001-nikko-en-clone-debug-20260813-031857-4281")
            .unwrap();
        let cues = payload["cues"].as_array().unwrap();
        let markers = payload["markers"].as_array().unwrap();
        let audio_onsets: Vec<i64> = markers
            .iter()
            .filter(|m| m["type"].as_str() == Some("audio_onset"))
            .filter_map(|m| m["start_ms"].as_i64())
            .collect();
        assert!(!audio_onsets.is_empty(), "expected audio_onset markers");
        // Monotonic + within audio bounds.
        assert!(
            audio_onsets.windows(2).all(|w| w[0] <= w[1]),
            "audio_onset not sorted"
        );
        let dur = payload["audio"]["duration_ms"].as_i64().unwrap_or(i64::MAX);
        // Parity with `_mono_to_audio_ms`: events within duration + 2000 are kept.
        assert!(
            audio_onsets.iter().all(|s| *s <= dur + 2000),
            "audio_onset beyond audio duration + 2000"
        );
        // First onset should be early (audio-relative ~680ms), not after all cues.
        let first_cue_start = cues
            .iter()
            .filter_map(|c| c["start_ms"].as_i64())
            .min()
            .unwrap_or(i64::MAX);
        assert!(
            audio_onsets.first().unwrap() < &first_cue_start,
            "first audio_onset should precede the first cue start \
             (onset={} first_cue={})",
            audio_onsets.first().unwrap(),
            first_cue_start
        );
    }
}
