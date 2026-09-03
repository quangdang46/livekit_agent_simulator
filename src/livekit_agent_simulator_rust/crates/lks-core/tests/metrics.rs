//! Tests for metrics.rs (36-key voice QA metrics).
use lks_core::metrics::compute_voice_metrics;
use serde_json::{json, Map, Value as Json};

fn ev(kind: &str, ts: i64, spec: serde_json::Value) -> Map<String, Json> {
    let mut m = Map::new();
    m.insert("kind".into(), json!(kind));
    m.insert("ts_mono_ms".into(), json!(ts));
    m.insert("spec".into(), spec);
    m
}

#[test]
fn empty_events_safe() {
    let m = compute_voice_metrics(&[]);
    assert_eq!(m["schema"], json!("agent-sim/metrics/v1"));
    assert_eq!(m["agent_finals"], json!(0));
    assert_eq!(m["user_finals"], json!(0));
    assert_eq!(m["barge_count"], json!(0));
    assert_eq!(m["tool_calls"], json!(0));
    assert_eq!(m["tool_error_rate"], Json::Null);
    assert_eq!(m["ttfw_ms"], Json::Null);
}

#[test]
fn basic_finals_and_chars() {
    let events = vec![
        ev(
            "transcript.agent.final",
            100,
            json!({"text": "Hello there", "turn_taking_ms": 800}),
        ),
        ev(
            "transcript.user.final",
            500,
            json!({"text": "Hi, I need help"}),
        ),
        ev("tool.start", 600, json!({"name": "x"})),
    ];
    let m = compute_voice_metrics(&events);
    assert_eq!(m["agent_finals"], json!(1));
    assert_eq!(m["user_finals"], json!(1));
    assert_eq!(m["agent_chars"], json!(11));
    assert_eq!(m["tool_calls"], json!(1));
    assert_eq!(m["ttfw_ms"], json!(100));
    assert_eq!(m["ttfw_source"], json!("transcript.agent.final"));
    assert_eq!(m["user_words_count"], json!(1));
}

#[test]
fn barge_recovery() {
    let events = vec![
        ev(
            "sim.script.cue",
            100,
            json!({"barge_in": true, "class": "correction"}),
        ),
        ev("transcript.agent.final", 700, json!({"text": "ok"})),
    ];
    let m = compute_voice_metrics(&events);
    assert_eq!(m["barge_count"], json!(1));
    assert_eq!(m["barges_recovered"], json!(1));
    // recovery_ms = 700 - 100 = 600
    let rec = m["recovery_ms"].as_object().unwrap();
    assert_eq!(rec["p50"], json!(600.0));
}

#[test]
fn slow_turns_counted() {
    let events = vec![
        ev(
            "transcript.agent.final",
            100,
            json!({"text": "a", "turn_taking_ms": 3000}),
        ),
        ev(
            "transcript.agent.final",
            200,
            json!({"text": "b", "turn_taking_ms": 500}),
        ),
    ];
    let m = compute_voice_metrics(&events);
    assert_eq!(m["slow_turns_over_2500ms"], json!(1));
    assert_eq!(m["slow_turns_over_5000ms"], json!(0));
}

#[test]
fn talk_ratio() {
    let events = vec![
        ev(
            "transcript.agent.final",
            100,
            json!({"text": "a", "turn_taking_ms": 1000}),
        ),
        ev(
            "transcript.agent.final",
            200,
            json!({"text": "b", "turn_taking_ms": 3000}),
        ),
    ];
    let m = compute_voice_metrics(&events);
    // total turn_taking / agent_finals = 4000/2 = 2000
    assert_eq!(m["talk_ratio"], json!(2000.0));
}

#[test]
fn user_word_origin_split() {
    let events = vec![
        ev(
            "transcript.user.final",
            100,
            json!({"text": "one two three", "speech_origin": "natural"}),
        ),
        ev(
            "transcript.user.final",
            200,
            json!({"text": "four five", "speech_origin": "script_cue"}),
        ),
    ];
    let m = compute_voice_metrics(&events);
    assert_eq!(m["user_words_count"], json!(2));
    assert_eq!(m["user_words_natural_count"], json!(1));
    assert_eq!(m["user_words_script_count"], json!(1));
}

#[test]
fn empty_run_metrics_full_pct_blocks() {
    // Python _pct_block keeps the 7-key shape with nulls on empty samples —
    // parity for zero-activity runs (was emitting {} before the fix).
    use lks_core::metrics::compute_voice_metrics;
    let m = compute_voice_metrics(&[]);
    for key in ["turn_taking_ms", "recovery_ms", "turn_taking_audio_ms"] {
        let block = m.get(key).and_then(|v| v.as_object()).unwrap();
        assert_eq!(
            block.get("count").and_then(|v| v.as_i64()),
            Some(0),
            "{key}"
        );
        assert!(
            block.get("p50").map(|v| v.is_null()).unwrap_or(false),
            "{key} p50 null"
        );
        assert!(
            block.get("p95").map(|v| v.is_null()).unwrap_or(false),
            "{key} p95 null"
        );
        assert!(
            block.get("max").map(|v| v.is_null()).unwrap_or(false),
            "{key} max null"
        );
        assert!(
            block.get("min").map(|v| v.is_null()).unwrap_or(false),
            "{key} min null"
        );
        assert!(
            block.get("mean").map(|v| v.is_null()).unwrap_or(false),
            "{key} mean null"
        );
    }
}

#[test]
fn pct_block_nonempty_values() {
    use lks_core::metrics::compute_voice_metrics;
    use serde_json::{json, Map};
    let mut e = Map::new();
    e.insert("kind".into(), json!("transcript.agent.final"));
    e.insert("ts_mono_ms".into(), json!(1000));
    let mut spec = Map::new();
    spec.insert("text".into(), json!("hello world"));
    spec.insert("turn_taking_ms".into(), json!(500.0));
    e.insert("spec".into(), json!(spec));
    let m = compute_voice_metrics(&[e]);
    let tt = m.get("turn_taking_ms").and_then(|v| v.as_object()).unwrap();
    assert_eq!(tt.get("count").and_then(|v| v.as_i64()), Some(1));
    assert_eq!(tt.get("p50").and_then(|v| v.as_f64()), Some(500.0));
    assert_eq!(tt.get("max").and_then(|v| v.as_f64()), Some(500.0));
    assert_eq!(tt.get("min").and_then(|v| v.as_f64()), Some(500.0));
    assert_eq!(tt.get("mean").and_then(|v| v.as_f64()), Some(500.0));
}
