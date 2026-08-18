//! Golden tests for config parsing (P1) — byte-exact contracts vs config.py.
//! Uses the real templates/config.yaml as input (copied into a temp .agent-sim/).
use lks_core::config::{load_config, ConfigError};
use std::path::PathBuf;

fn repo_root() -> PathBuf {
    // tests run with cwd = crate dir; repo root is 4 levels up.
    let mut p = std::env::current_dir().unwrap();
    for _ in 0..4 {
        p.pop();
    }
    p
}

/// Copy templates/config.yaml into a fresh temp <dir>/.agent-sim/config.yaml
/// and return that dir (load_config expects `<root>/.agent-sim/config.yaml`).
fn temp_root_with_template(name: &str) -> PathBuf {
    let root = repo_root();
    let src = root.join("templates").join("config.yaml");
    let dir = std::env::temp_dir().join(format!("lks_p1_{name}"));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(dir.join(".agent-sim")).unwrap();
    std::fs::copy(&src, dir.join(".agent-sim").join("config.yaml")).expect("copy template");
    dir
}

/// Write a raw config body into a temp <dir>/.agent-sim/config.yaml.
fn temp_root_with_raw(name: &str, body: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("lks_p1_{name}"));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(dir.join(".agent-sim")).unwrap();
    std::fs::write(dir.join(".agent-sim").join("config.yaml"), body).expect("write config");
    dir
}

#[test]
fn golden_config_template_parses() {
    let dir = temp_root_with_template("template");
    let cfg = match load_config(dir, None, None) {
        Ok(c) => c,
        Err(e) => panic!("template parse failed: {}", e),
    };
    assert_eq!(cfg.project.as_deref(), Some("my-voice-agent"));
    assert_eq!(cfg.livekit.url, "wss://YOUR-PROJECT.livekit.cloud");
    assert_eq!(cfg.livekit.api_key, "APIxxxxxxxxxxxx");
    assert_eq!(cfg.livekit.api_secret, "secretxxxxxxxxxxxxxxxxxxxxxxxx");
    assert_eq!(cfg.livekit.agent_name, "my-voice-agent-local");
    assert_eq!(cfg.livekit.room_prepare_ms, 500);
    assert_eq!(cfg.livekit.agent_join_timeout_ms, 25_000);
    assert_eq!(cfg.livekit.dispatch_metadata, None);
    assert_eq!(cfg.simulator.provider, "google");
    assert_eq!(cfg.simulator.mode, "realtime");
    assert_eq!(cfg.simulator.api_key, "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx");
    assert_eq!(cfg.simulator.language, "en-US");
    assert_eq!(cfg.simulator.voice.model, "gemini-3.1-flash-live-preview");
    assert_eq!(cfg.simulator.voice.voice, "Puck");
    assert_eq!(cfg.simulator.voice.language, "en-US");
    assert_eq!(cfg.simulator.name, "default");
    // judge present (template has judge block)
    let j = cfg.judge.as_ref().expect("judge present");
    assert_eq!(j.model.as_deref(), Some("gemini-3.1-flash-lite"));
    assert_eq!(j.temperature, 0.0);
    assert_eq!(j.base_url, None);
    assert_eq!(j.endpoint_type, "openai");
    // observe
    assert_eq!(cfg.observe.timezone, "UTC");
    assert!(cfg.observe.lk_transcription);
    assert!(cfg.observe.lk_agent_session);
    assert!(cfg.observe.record_audio);
    assert_eq!(cfg.observe.data_topics, Vec::<String>::new());
    assert_eq!(
        cfg.observe.transcript_payload_types,
        vec!["transcript_turn"]
    );
    // Template explicitly sets transcript_dedupe_window_ms: 5000.
    assert_eq!(cfg.observe.transcript_dedupe_window_ms, 5000);
    assert_eq!(cfg.observe.silence_threshold_ms, 4000);
    assert_eq!(cfg.observe.turn_taking_warn_ms, 2500);
    // telephony default (commented out in template)
    assert_eq!(cfg.telephony.outbound_trunk_id, None);
    assert_eq!(cfg.telephony.prepare_ms, 3_000);
    assert!(cfg.telephony.wait_until_answered);
    assert!(!cfg.telephony.krisp_enabled);
}

#[test]
fn config_require_fails_fast() {
    // Empty api_key → ConfigError with byte-exact message.
    let dir = temp_root_with_raw(
        "require",
        "livekit:\n  url: \"wss://x.livekit.cloud\"\n  api_key: \"key\"\n  api_secret: \"sec\"\n  agent_name: \"a\"\nsimulator:\n  provider: google\n  api_key: \"\"\n",
    );
    let err = load_config(dir, None, None).expect_err("empty api_key must fail");
    assert_eq!(
        err.to_string(),
        "Missing `simulator.api_key` in .agent-sim/config.yaml. Copy the value from LiveKit Cloud / your worker and try again."
    );
}

#[test]
fn config_require_zero_passes() {
    // `0`/`false`/`0.0` PASS _require (not None, not whitespace string).
    let dir = temp_root_with_raw(
        "zero",
        "livekit:\n  url: \"wss://x\"\n  api_key: \"k\"\n  api_secret: \"s\"\n  agent_name: \"a\"\n  agent_join_timeout_ms: 0\nsimulator:\n  api_key: \"ak\"\n",
    );
    let cfg = load_config(dir, None, None).expect("0 passes _require");
    assert_eq!(cfg.livekit.agent_join_timeout_ms, 0);
}

#[test]
fn config_snapshot_redacts_secrets() {
    let dir = temp_root_with_template("snapshot");
    let cfg = load_config(dir, None, None).expect("template parses");
    let snap = cfg.config_snapshot();
    // Never contains the api_key/api_secret VALUES.
    let serialized = serde_json::to_string(&snap).unwrap();
    assert!(!serialized.contains("APIxxxxxxxxxxxx"));
    assert!(!serialized.contains("secretxxxxxxxxxxxxxxxxxxxxxxxx"));
    assert!(!serialized.contains("xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"));
    // Key order is a contract.
    let keys: Vec<String> = snap.keys().cloned().collect();
    assert_eq!(
        keys,
        vec![
            "project",
            "livekit",
            "simulator",
            "judge_enabled",
            "judge",
            "cues",
            "observe",
            "telephony",
            "observe_gaps"
        ]
    );
    // url_host derivation
    let livekit = snap["livekit"].as_object().unwrap();
    assert_eq!(livekit["url_host"], "YOUR-PROJECT.livekit.cloud");
}

#[test]
fn config_missing_file_error() {
    let dir = std::env::temp_dir().join("lks_p1_missing");
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let err = load_config(dir, None, None).expect_err("missing file must fail");
    let s = err.to_string();
    assert!(
        s.contains("not found. Run `lks init` (or the `init_project` MCP tool)"),
        "got: {s}"
    );
    assert!(s.contains("to scaffold .agent-sim/ first."), "got: {s}");
}

#[test]
fn config_unknown_provider_fails() {
    let dir = temp_root_with_raw(
        "provider",
        "livekit:\n  url: \"wss://x\"\n  api_key: \"k\"\n  api_secret: \"s\"\n  agent_name: \"a\"\nsimulator:\n  provider: anthropic\n  api_key: \"ak\"\n",
    );
    let err = load_config(dir, None, None).expect_err("unknown provider fails");
    assert!(
        err.to_string()
            .contains("`simulator.provider` must be `google` or `openai`"),
        "got: {err}"
    );
}

#[test]
fn config_require_is_configerror() {
    // The error type is ConfigError (impl Display via thiserror).
    fn takes_configerror(_: &ConfigError) {}
    let dir = temp_root_with_raw(
        "type",
        "livekit:\n  url: \"wss://x\"\n  api_key: \"k\"\n  api_secret: \"s\"\n  agent_name: \"a\"\n",
    );
    match load_config(dir, None, None) {
        Err(e) => takes_configerror(&e),
        Ok(_) => panic!("must fail: missing simulator section"),
    }
}

#[test]
fn config_missing_livekit_section() {
    let dir = temp_root_with_raw("no_livekit", "simulator:\n  api_key: \"ak\"\n");
    let err = load_config(dir, None, None).expect_err("missing livekit section");
    assert!(
        err.to_string().contains("Missing `livekit:` section in"),
        "got: {err}"
    );
}

#[test]
fn config_bool_string_trap() {
    // Python bool("false") == True — a quoted "false" string is TRUTHY.
    let dir = temp_root_with_raw(
        "booltrap",
        "livekit:\n  url: \"wss://x\"\n  api_key: \"k\"\n  api_secret: \"s\"\n  agent_name: \"a\"\nsimulator:\n  api_key: \"ak\"\nobserve:\n  lk_transcription: \"false\"\n  record_audio: \"false\"\n",
    );
    let cfg = load_config(dir, None, None).expect("parses");
    // The string "false" is truthy in Python bool(), so these are TRUE.
    assert!(cfg.observe.lk_transcription, "string \"false\" is truthy");
    assert!(cfg.observe.record_audio, "string \"false\" is truthy");
}

#[test]
fn config_yaml_not_mapping_fails() {
    let dir = temp_root_with_raw("notmap", "- just\n- a\n- list\n");
    let err = load_config(dir, None, None).expect_err("top-level list must fail");
    assert!(
        err.to_string()
            .contains("must be a YAML mapping at the top level"),
        "got: {err}"
    );
}
