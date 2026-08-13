//! Tests for the event writer (I1 byte-compat envelope).
use lks_core::logging::event::EventWriter;
use serde_json::{json, Map};
use std::path::PathBuf;

fn temp_dir(name: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("lks_p3_ev_{name}"));
    let _ = std::fs::remove_dir_all(&d);
    d
}

#[test]
fn emit_envelope_shape() {
    let dir = temp_dir("shape");
    let mut w = EventWriter::new("001-x-20260813-010000-0000", dir.clone(), "UTC", 2500).unwrap();
    let mut spec = Map::new();
    spec.insert("text".into(), json!("Hello"));
    let ev = w.emit(
        "transcript.user.final",
        Some(&spec),
        "sim.gemini",
        None,
        None,
        true,
        None,
    );
    // envelope fields present
    assert!(ev["event_id"].as_str().unwrap().starts_with("evt_"));
    assert_eq!(ev["seq"], json!(1));
    assert_eq!(ev["run_id"], json!("001-x-20260813-010000-0000"));
    assert_eq!(ev["turn"], json!(0));
    assert_eq!(ev["kind"], json!("transcript.user.final"));
    assert_eq!(ev["source"], json!("sim.gemini"));
    assert_eq!(ev["spec"]["text"], json!("Hello"));
    assert!(ev["ts"].is_number());
    assert!(ev["ts_mono_ms"].is_number());
    assert!(ev["datetime_utc"].as_str().unwrap().ends_with('Z'));
    assert!(ev["datetime_local"].as_str().unwrap().contains('T'));
    // dialogue snapshot present (include_dialogue=true)
    assert!(ev["dialogue"].is_object());
    assert!(ev["dialogue"]["user"]["note"]
        .as_str()
        .unwrap()
        .contains("has not spoken yet"));
}

#[test]
fn emit_no_dialogue_when_disabled() {
    let dir = temp_dir("nodlg");
    let mut w = EventWriter::new("r", dir.clone(), "UTC", 2500).unwrap();
    let ev = w.emit("run.started", None, "mcp", None, None, false, None);
    assert!(ev.get("dialogue").is_none());
}

#[test]
fn seq_monotonic_and_events_list() {
    let dir = temp_dir("seq");
    let mut w = EventWriter::new("r", dir, "UTC", 2500).unwrap();
    w.emit("a", None, "s", None, None, false, None);
    w.emit("b", None, "s", None, None, false, None);
    w.emit("c", None, "s", None, None, false, None);
    assert_eq!(w.events().len(), 3);
    assert_eq!(w.events()[1]["seq"], json!(2));
}

#[test]
fn turn_metrics_aggregates() {
    let dir = temp_dir("turns");
    let mut w = EventWriter::new("r", dir, "UTC", 2500).unwrap();
    w.begin_turn(1);
    w.emit(
        "transcript.user.final",
        Some(&json!({"text": "Hi"}).as_object().unwrap().clone()),
        "s",
        Some(1),
        None,
        true,
        None,
    );
    w.emit(
        "transcript.agent.final",
        Some(
            &json!({"text": "Hello", "turn_taking_ms": 800})
                .as_object()
                .unwrap()
                .clone(),
        ),
        "s",
        Some(1),
        None,
        true,
        None,
    );
    w.emit(
        "tool.start",
        Some(&json!({"name": "x"}).as_object().unwrap().clone()),
        "s",
        Some(1),
        None,
        true,
        None,
    );
    let turns = w.turn_metrics();
    assert_eq!(turns.len(), 1);
    assert_eq!(turns[0]["turn"], json!(1));
    assert_eq!(turns[0]["user_text"], json!("Hi"));
    assert_eq!(turns[0]["agent_text"], json!("Hello"));
    assert_eq!(turns[0]["turn_taking_ms"], json!(800));
    assert_eq!(turns[0]["tool_count"], json!(1));
}

#[test]
fn finalize_writes_summary() {
    let dir = temp_dir("finalize");
    let mut w = EventWriter::new("001-x-20260813", dir.clone(), "UTC", 2500).unwrap();
    w.emit(
        "run.started",
        Some(&json!({"scenario_id": "smoke"}).as_object().unwrap().clone()),
        "mcp",
        None,
        None,
        false,
        None,
    );
    w.begin_turn(1);
    w.emit(
        "transcript.agent.final",
        Some(
            &json!({"text": "ok", "turn_taking_ms": 500})
                .as_object()
                .unwrap()
                .clone(),
        ),
        "s",
        Some(1),
        None,
        true,
        None,
    );
    let summary = w.finalize("done", None, None);
    assert_eq!(summary["status"], json!("done"));
    assert_eq!(summary["turn_count"], json!(1));
    assert_eq!(summary["event_count"], json!(3)); // 2 emitted + run.ended
    assert!(summary["metrics"].is_object());
    // files written
    assert!(dir.join("events.jsonl").exists());
    assert!(dir.join("summary.json").exists());
    assert!(dir.join("timeline.md").exists());
    // timeline content
    let tl = std::fs::read_to_string(dir.join("timeline.md")).unwrap();
    assert!(tl.contains("# Timeline — 001-x-20260813"));
    assert!(tl.contains("| local time | +ms | turn | kind | source | detail |"));
    assert!(tl.contains("transcript.agent.final"));
}

#[test]
fn finalize_writes_meta() {
    let dir = temp_dir("meta");
    let mut w = EventWriter::new("r", dir.clone(), "UTC", 2500).unwrap();
    let meta = json!({"scenario_id": "smoke", "agent_name": "a"})
        .as_object()
        .unwrap()
        .clone();
    w.finalize("done", Some(&meta), None);
    let m = std::fs::read_to_string(dir.join("meta.json")).unwrap();
    assert!(m.contains("\"scenario_id\": \"smoke\""));
}
