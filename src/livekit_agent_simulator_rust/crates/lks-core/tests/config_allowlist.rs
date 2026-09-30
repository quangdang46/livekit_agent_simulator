//! An unknown config key must be REFUSED, not silently ignored.
//!
//! The loader is fail-open by default: a key it does not recognise is simply
//! not read, so every setting it was meant to carry reverts to its default
//! with no warning. Measured on the Python port 2026-09-30: nesting `observe:`
//! into subgroups loaded clean, `timezone` reset to `UTC` and
//! `silence_threshold_ms` reset 22000 -> 4000, nothing printed. For a harness
//! whose job is reporting what actually happened, a silent default swap is
//! worse than a refused config.
//!
//! The allowlist is the UNION across ports on purpose — see `TOP_LEVEL_KEYS`
//! in `config.rs`. A suite that only asserted "typo rejected" would still pass
//! if the list had been wrongly narrowed to just what `lksr` itself parses, so
//! the last two cases here pin that keys with no consumer on this port are
//! still ACCEPTED.

use lks_core::config::load_config;
use std::fs;
use std::path::PathBuf;
use std::sync::atomic::{AtomicU32, Ordering};

/// A unique scratch root per call. `lks-core` has no `[dev-dependencies]`
/// section, so this deliberately avoids pulling in `tempfile` for one helper:
/// a config test does not justify a new dependency in the published manifest.
fn scratch_root(tag: &str) -> PathBuf {
    static SEQ: AtomicU32 = AtomicU32::new(0);
    let n = SEQ.fetch_add(1, Ordering::SeqCst);
    let dir = std::env::temp_dir().join(format!("lks-cfg-{}-{}-{}", tag, std::process::id(), n));
    let _ = fs::remove_dir_all(&dir);
    fs::create_dir_all(&dir).unwrap();
    dir
}

fn load(body: &str) -> Result<(), String> {
    let dir = scratch_root("allow");
    let dot = dir.join(".agent-sim");
    fs::create_dir_all(&dot).unwrap();
    fs::write(dot.join("config.yaml"), body).unwrap();
    let out = load_config(dir.clone(), None, None)
        .map(|_| ())
        .map_err(|e| e.0);
    let _ = fs::remove_dir_all(&dir);
    out
}

const MINIMAL: &str = r#"
project: demo
livekit:
  url: "wss://example.livekit.cloud"
  api_key: "APIkey"
  api_secret: "secret"
  agent_name: "agent"
simulator:
  provider: google
  api_key: "AIzaTest"
"#;

#[test]
fn the_minimal_config_still_loads() {
    assert_eq!(load(MINIMAL), Ok(()));
}

#[test]
fn a_typo_in_a_top_level_key_is_refused_by_name() {
    let err = load(&format!("{MINIMAL}\nsimilator:\n  provider: google\n")).unwrap_err();
    assert!(
        err.contains("similator"),
        "error must NAME the bad key: {err}"
    );
    assert!(
        err.contains("simulator"),
        "error must list known keys: {err}"
    );
}

#[test]
fn a_mis_nested_observe_block_is_refused() {
    // The exact 2026-09-30 failure: these keys became unreachable silently.
    let body = format!(
        "{MINIMAL}\nobserve:\n  livekit:\n    timezone: \"Asia/Ho_Chi_Minh\"\n  \
         timing:\n    silence_threshold_ms: 22000\n"
    );
    let err = load(&body).unwrap_err();
    assert!(
        err.contains("observe"),
        "error must name the section: {err}"
    );
    assert!(
        err.contains("livekit") && err.contains("timing"),
        "error must name the offending keys: {err}"
    );
}

#[test]
fn a_typo_inside_observe_is_refused() {
    let body = format!("{MINIMAL}\nobserve:\n  time_zone: \"Asia/Ho_Chi_Minh\"\n");
    let err = load(&body).unwrap_err();
    assert!(
        err.contains("time_zone"),
        "error must NAME the bad key: {err}"
    );
}

/// A MISSING key is not an error — only a key that is present and unrecognised
/// is. A config that sets nothing optional must still load.
#[test]
fn omitting_every_optional_block_is_not_an_error() {
    assert_eq!(load(MINIMAL), Ok(()));
}

/// The union rule. `text_planner` and `target_keywords` are Python-only knobs;
/// if the allowlist were narrowed to what `lksr` parses, a valid shared
/// `.agent-sim/` would be refused on the Rust port — a new cross-port
/// divergence, which is the thing the router guard's comment defends against.
#[test]
fn a_key_this_port_has_no_consumer_for_is_still_accepted() {
    let body = format!(
        "{MINIMAL}\ntext_planner:\n  enabled: false\n  provider: openai\n\
         \nrouter:\n  provider: openai\n  timeout_ms: 1500\n"
    );
    assert_eq!(load(&body), Ok(()), "Python-only keys must load on lksr");
}

/// One case per key the Python observer reads, so the list cannot be narrowed
/// without this failing.
///
/// `audio_onset` is given a mapping rather than `null`: the pre-existing
/// "must be a mapping (or absent)" check rejects a bare null, which is correct
/// and unrelated to the allowlist.
#[test]
fn every_observe_key_the_python_loader_reads_is_accepted() {
    for (key, value) in [
        ("timezone", "null"),
        ("lk_transcription", "null"),
        ("lk_agent_session", "null"),
        ("record_audio", "null"),
        ("data_topics", "null"),
        ("transcript_payload_types", "null"),
        ("transcript_dedupe_window_ms", "null"),
        ("flow_topics", "null"),
        ("tool_event_patterns", "null"),
        ("silence_threshold_ms", "null"),
        ("turn_taking_warn_ms", "null"),
        ("audio_onset", "{enabled: false}"),
    ] {
        let body = format!("{MINIMAL}\nobserve:\n  {key}: {value}\n");
        assert_eq!(load(&body), Ok(()), "`observe.{key}` must be accepted");
    }
}
