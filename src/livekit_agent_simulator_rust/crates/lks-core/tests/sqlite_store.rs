//! Tests for sqlite RunStore (I2 DDL byte-identical + CRUD).
use lks_core::logging::sqlite::{connect, RunStore, SCHEMA};
use rusqlite::Connection;
use serde_json::{json, Map};
use std::path::PathBuf;

fn temp_db(name: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("lks_p3_sql_{name}.sqlite"));
    let _ = std::fs::remove_file(&d);
    d
}

#[test]
fn schema_creates_tables() {
    let p = temp_db("schema");
    let db: Connection = connect(p.to_str().unwrap()).expect("connect");
    let n: i64 = db
        .query_row("SELECT count(*) FROM sqlite_master WHERE type='table' AND name IN ('runs','run_events','run_turns')", [], |r| r.get(0))
        .unwrap();
    assert_eq!(n, 3);
}

#[test]
fn ddl_byte_identical() {
    // SCHEMA must match the Python source exactly (I2) — whitespace-insensitive compare.
    let py_schema = std::fs::read_to_string(
        "C:/Users/ADMIN/Documents/Projects/livekit-agent-simulator/src/livekit_agent_simulator/logging/sqlite_store.py",
    )
    .unwrap();
    let start = py_schema.find("CREATE TABLE IF NOT EXISTS runs").unwrap();
    let end = py_schema[start..].find("\"\"\"").unwrap();
    let py_ddl = &py_schema[start..start + end];
    let norm = |s: &str| s.split_whitespace().collect::<Vec<_>>().join(" ");
    assert_eq!(
        norm(py_ddl.trim()),
        norm(SCHEMA.trim()),
        "DDL must match Python (whitespace-insensitive)"
    );
}

#[test]
fn create_finish_run_roundtrip() {
    let p = temp_db("crud");
    let s = RunStore::new(p.to_str().unwrap());
    s.create_run(
        "001-smoke-20260813-010000-0000",
        "smoke",
        "lks-room",
        "agent-a",
        "2026-08-13T01:00:00Z",
        "/tmp/r",
    )
    .unwrap();
    let mut summary = Map::new();
    summary.insert("status".into(), json!("done"));
    summary.insert("duration_ms".into(), json!(5000));
    summary.insert("turn_count".into(), json!(2));
    summary.insert("tool_errors".into(), json!(0));
    s.finish_run(
        "001-smoke-20260813-010000-0000",
        "done",
        &summary,
        "2026-08-13T01:00:05Z",
    )
    .unwrap();
    let run = s
        .get_run("001-smoke-20260813-010000-0000")
        .unwrap()
        .unwrap();
    assert_eq!(run["status"], json!("done"));
    assert_eq!(run["scenario_id"], json!("smoke"));
}

#[test]
fn insert_events_and_turns() {
    let p = temp_db("events");
    let s = RunStore::new(p.to_str().unwrap());
    let mut ev = Map::new();
    ev.insert("event_id".into(), json!("evt_abc"));
    ev.insert("seq".into(), json!(1));
    ev.insert("turn".into(), json!(1));
    ev.insert("kind".into(), json!("transcript.user.final"));
    ev.insert("ts".into(), json!(1000));
    ev.insert("datetime_utc".into(), json!("2026-08-13T00:00:01.000Z"));
    ev.insert("source".into(), json!("sim"));
    ev.insert("spec".into(), json!({"text": "Hi"}));
    s.insert_events("r1", &[ev]).unwrap();
    let mut turn = Map::new();
    turn.insert("turn".into(), json!(1));
    turn.insert("user_text".into(), json!("Hi"));
    turn.insert("interrupted".into(), json!(false));
    s.insert_turns("r1", &[turn]).unwrap();
    // verify via raw query
    let db = connect(p.to_str().unwrap()).unwrap();
    let n: i64 = db
        .query_row(
            "SELECT count(*) FROM run_events WHERE run_id='r1'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(n, 1);
    let n2: i64 = db
        .query_row(
            "SELECT count(*) FROM run_turns WHERE run_id='r1'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(n2, 1);
}

#[test]
fn list_runs_limit_and_filter() {
    let p = temp_db("list");
    let s = RunStore::new(p.to_str().unwrap());
    s.create_run("a", "s1", "r", "ag", "2026-08-13T00:00:00Z", "/r")
        .unwrap();
    s.create_run("b", "s1", "r", "ag", "2026-08-13T00:00:01Z", "/r")
        .unwrap();
    s.create_run("c", "s2", "r", "ag", "2026-08-13T00:00:02Z", "/r")
        .unwrap();
    let all = s.list_runs(20, None).unwrap();
    assert_eq!(all.len(), 3);
    let s1 = s.list_runs(20, Some("s1")).unwrap();
    assert_eq!(s1.len(), 2);
    let lim = s.list_runs(1, None).unwrap();
    assert_eq!(lim.len(), 1);
}
