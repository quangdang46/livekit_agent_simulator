//! Parity regression: cross-source duplicate user finals must not double the
//! run-summary text.
//!
//! A caller utterance is reported twice — the room's own STT
//! (`lk.transcription`, carries a segment id) and the agent republishing its
//! transcript (a data topic, no segment id, ~400 ms later, identical text).
//! The dedupe window deliberately lets the better-ranked data-topic source
//! through, but with identical TEXT the second event carries no new
//! information, and `merge_as_same_turn` treated it as a split-utterance
//! continuation, so every caller line rendered doubled
//! ("Hello. ... Hello. ...") in `summary.json`.
//!
//! Kept in parity with `tests/test_observer.py` and
//! `lks-core/src/observer.rs::on_user_final`.

use std::time::Instant;

use lks_core::logging::event::EventWriter;
use lks_core::observer::{Observer, ObserverConfig};
use serde_json::{Map, Value};

fn make_observer(dir: &std::path::Path) -> (Observer, EventWriter) {
    let w = EventWriter::new("r-test", dir.join("reports").join("r-test"), "UTC", 2500).unwrap();
    let mut cfg = ObserverConfig::default();
    cfg.agent_identity = "agent-1".into();
    cfg.sim_identity = "sim-1".into();
    cfg.first_speaker = "user".into();
    let o = Observer::new(cfg);
    (o, w)
}

fn user_finals(w: &EventWriter) -> Vec<Map<String, Value>> {
    w.events()
        .iter()
        .filter(|e| e.get("kind").and_then(Value::as_str) == Some("transcript.user.final"))
        .cloned()
        .collect()
}

fn spec_text(e: &Map<String, Value>) -> String {
    e.get("spec")
        .and_then(Value::as_object)
        .and_then(|spec| spec.get("text"))
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string()
}

#[test]
fn agent_republished_transcript_does_not_double_user_text() {
    let dir = std::env::temp_dir().join("lks-parity-dedupe-a");
    let _ = std::fs::remove_dir_all(&dir);
    let (mut o, mut w) = make_observer(&dir);
    let line = "Hello. I would like to speak to someone about my building.";
    let t0 = Instant::now();

    o.on_transcript(&mut w, "user", line, true, Some("SG_1"), "lk.transcription", t0, 0);
    // The agent republishes the same line, no segment id, before any reply.
    o.on_transcript(
        &mut w,
        "user",
        line,
        true,
        None,
        "voice_ai.transcript",
        t0 + std::time::Duration::from_millis(405u64),
        405,
    );

    let finals = user_finals(&w);
    assert_eq!(finals.len(), 1, "cross-source duplicate must be dropped");
    assert_eq!(spec_text(&finals[0]), line);
}

#[test]
fn same_source_repeat_is_dropped_by_the_dedupe_window() {
    let dir = std::env::temp_dir().join("lks-parity-dedupe-b");
    let _ = std::fs::remove_dir_all(&dir);
    let (mut o, mut w) = make_observer(&dir);
    let t0 = Instant::now();

    o.on_transcript(&mut w, "user", "yes", true, Some("SG_1"), "lk.transcription", t0, 0);
    o.on_transcript(
        &mut w,
        "user",
        "yes",
        true,
        Some("SG_2"),
        "lk.transcription",
        t0 + std::time::Duration::from_millis(100u64),
        100,
    );

    // Pre-existing behaviour, unchanged by this fix: a same-source, same-text
    // final never reaches the turn-merge branch.
    assert_eq!(user_finals(&w).len(), 1);
}

#[test]
fn cross_source_duplicate_dropped_across_many_turns() {
    let dir = std::env::temp_dir().join("lks-parity-dedupe-c");
    let _ = std::fs::remove_dir_all(&dir);
    let (mut o, mut w) = make_observer(&dir);
    let lines = [
        "Hello.",
        "I am afraid I cannot say.",
        "I really do not know.",
        "That is all I can say.",
    ];
    let t0 = Instant::now();
    let mut clock = 0u64;

    for (i, line) in lines.iter().enumerate() {
        o.on_transcript(
            &mut w,
            "user",
            line,
            true,
            Some(&format!("SG_{i}")),
            "lk.transcription",
            t0 + std::time::Duration::from_millis(clock),
            clock as i64,
        );
        clock += 405;
        o.on_transcript(
            &mut w,
            "user",
            line,
            true,
            None,
            "voice_ai.transcript",
            t0 + std::time::Duration::from_millis(clock),
            clock as i64,
        );
        clock += 405;
        // A real agent answer is a full sentence. A 1-2 word reply would be
        // classified as a backchannel, which legitimately merges the next user
        // final into this turn instead of starting a new one. Unique per turn:
        // an identical agent final would be swallowed by the dedupe window.
        let reply = format!(
            "Thank you. Next, could you please provide your callback phone number, \
             including the area code? (step {i})"
        );
        o.on_transcript(
            &mut w,
            "agent",
            &reply,
            true,
            None,
            "lk.transcription",
            t0 + std::time::Duration::from_millis(clock),
            clock as i64,
        );
        clock += 405;
    }

    let finals = user_finals(&w);
    assert_eq!(finals.len(), lines.len());
    for (e, expected) in finals.iter().zip(lines.iter()) {
        assert_eq!(&spec_text(e), expected);
    }
}
