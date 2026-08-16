//! Live-channel delivery: EventWriter::new_with_live clones every emitted
//! envelope (incl. run.ended from finalize) onto an mpsc channel — the hook
//! the lksr TUI live-run view streams through.

use lks_core::logging::event::EventWriter;
use serde_json::{json, Map};
use std::path::PathBuf;

fn temp_dir(name: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("lks_live_ch_{name}"));
    let _ = std::fs::remove_dir_all(&d);
    d
}

#[test]
fn live_channel_delivers_each_envelope_and_run_ended() {
    let dir = temp_dir("deliver");
    let (tx, rx) = std::sync::mpsc::channel();
    let mut w = EventWriter::new_with_live("r-1", dir.clone(), "UTC", 2500, Some(tx)).unwrap();

    let mut spec = Map::new();
    spec.insert("text".into(), json!("Hello"));
    let ev = w.emit(
        "transcript.user.final",
        Some(&spec),
        "sim",
        None,
        None,
        false,
        None,
    );
    let first = rx.recv().unwrap();
    assert_eq!(first.get("kind").and_then(|v| v.as_str()), Some("transcript.user.final"));
    assert_eq!(first.get("run_id").and_then(|v| v.as_str()), Some("r-1"));
    // emitted envelope matches what the writer returned
    assert_eq!(first.get("seq"), ev.get("seq"));

    // A second emit arrives in order.
    let mut spec2 = Map::new();
    spec2.insert("text".into(), json!("Bye"));
    w.emit("transcript.agent.final", Some(&spec2), "sim", None, None, false, None);
    let second = rx.recv().unwrap();
    assert_eq!(second.get("kind").and_then(|v| v.as_str()), Some("transcript.agent.final"));

    // finalize() emits run.ended — that must also flow to the channel.
    let mut meta = Map::new();
    meta.insert("scenario_id".into(), json!("smoke"));
    w.finalize("done", Some(&meta), None);
    let ended = rx.recv().unwrap();
    assert_eq!(ended.get("kind").and_then(|v| v.as_str()), Some("run.ended"));

    // Channel is exhausted (3 events total).
    assert!(rx.try_recv().is_err());
}

#[test]
fn new_without_live_has_no_stream() {
    let dir = temp_dir("none");
    let mut w = EventWriter::new("r-2", dir, "UTC", 2500).unwrap();
    let mut spec = Map::new();
    spec.insert("text".into(), json!("x"));
    w.emit("transcript.user.final", Some(&spec), "sim", None, None, false, None);
    let mut meta = Map::new();
    meta.insert("scenario_id".into(), json!("smoke"));
    w.finalize("done", Some(&meta), None);
    // Nothing observable to assert externally beyond: no panic, no channel
    // (the default new() path is exercised). Compile-time coverage is the point.
}
