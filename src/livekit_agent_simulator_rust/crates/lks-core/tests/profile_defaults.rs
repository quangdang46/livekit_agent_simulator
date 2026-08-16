//! Profile default-selection parity — port of config.py profile rules:
//! explicit --profile, exactly-one `default: true` auto-select, multiple
//! defaults error, and no-profile+no-flat-creds error.

use lks_core::config::load_config;
use std::path::PathBuf;

fn tmp_root(name: &str, sim_yaml: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("lksr_prof_{name}"));
    let _ = std::fs::remove_dir_all(&dir);
    let dot = dir.join(".agent-sim");
    std::fs::create_dir_all(&dot).unwrap();
    std::fs::write(
        dot.join("config.yaml"),
        format!(
            "livekit:\n  url: wss://example.livekit.cloud\n  api_key: test-key\n  api_secret: test-secret\n  agent_name: test-agent\nsimulator:\n{sim_yaml}\n"
        ),
    )
    .unwrap();
    dir
}

#[test]
fn explicit_profile_selects_and_merges_voice() {
    let root = tmp_root(
        "explicit",
        "  api_key: flat-key\n  voice:\n    model: flat-model\n    voice: flat-voice\n  profiles:\n    google:\n      provider: google\n      api_key: gkey\n      voice:\n        model: gemini-x\n        voice: Aoede\n",
    );
    let cfg = load_config(root, Some("google")).unwrap();
    assert_eq!(cfg.simulator.api_key, "gkey");
    assert_eq!(cfg.simulator.voice.model, "gemini-x");
    assert_eq!(cfg.simulator.voice.voice, "Aoede");
    assert_eq!(cfg.active_profile.as_deref(), Some("google"));
}

#[test]
fn single_default_autoselects() {
    let root = tmp_root(
        "autoselect",
        "  api_key: flat-key\n  profiles:\n    google:\n      default: true\n      provider: google\n      api_key: gkey\n    openai:\n      provider: openai\n      api_key: okey\n",
    );
    let cfg = load_config(root, None).unwrap();
    assert_eq!(cfg.active_profile.as_deref(), Some("google"));
    assert_eq!(cfg.simulator.api_key, "gkey");
}

#[test]
fn no_default_uses_flat_block() {
    let root = tmp_root(
        "flat",
        "  api_key: flat-key\n  profiles:\n    google:\n      provider: google\n      api_key: gkey\n",
    );
    let cfg = load_config(root, None).unwrap();
    assert_eq!(cfg.active_profile, None);
    assert_eq!(cfg.simulator.api_key, "flat-key");
}

#[test]
fn multiple_defaults_is_error() {
    let root = tmp_root(
        "multi_default",
        "  api_key: flat-key\n  profiles:\n    google:\n      default: true\n      api_key: gkey\n    openai:\n      default: true\n      api_key: okey\n",
    );
    let err = load_config(root, None).unwrap_err();
    let msg = err.to_string();
    assert!(
        msg.contains("Multiple profiles marked `default: true`"),
        "got: {msg}"
    );
}

#[test]
fn no_profile_no_flat_creds_is_error() {
    let root = tmp_root(
        "no_creds",
        "  profiles:\n    google:\n      provider: google\n      api_key: gkey\n",
    );
    let err = load_config(root, None).unwrap_err();
    let msg = err.to_string();
    assert!(msg.contains("No default profile configured"), "got: {msg}");
}

#[test]
fn unknown_profile_lists_available() {
    let root = tmp_root(
        "unknown",
        "  api_key: flat-key\n  profiles:\n    google:\n      api_key: gkey\n    openai:\n      api_key: okey\n",
    );
    let err = load_config(root, Some("nope")).unwrap_err();
    let msg = err.to_string();
    assert!(msg.contains("Profile 'nope' not found"), "got: {msg}");
    assert!(msg.contains("google"), "got: {msg}");
    assert!(msg.contains("openai"), "got: {msg}");
}
