//! execute_scenario parity — repeat/pass@k validation, gate envelope shapes,
//! transport-drop retry flag. Offline (no livekit): the wrapper's pure logic is
//! tested with an invalid scenario (never reaches the bridge) and the
//! transport-drop helper.

use lks_livekit::run::{execute_scenario, is_transport_drop, ExecuteOptions};
use serde_json::json;

fn tmp_root() -> tempfile::TempDir {
    let dir = tempfile::tempdir().unwrap();
    // Scaffold a minimal .agent-sim so load_config succeeds.
    let dot = dir.path().join(".agent-sim");
    std::fs::create_dir_all(&dot).unwrap();
    std::fs::create_dir_all(dot.join("scenarios")).unwrap();
    std::fs::create_dir_all(dot.join("reports")).unwrap();
    std::fs::write(
        dot.join("config.yaml"),
        "livekit:\n  url: wss://example.livekit.cloud\n  api_key: test-key\n  api_secret: test-secret\n  agent_name: test-agent\nsimulator:\n  provider: openai\n  api_key: sk-test-key-1234567890\n",
    )
    .unwrap();
    dir
}

#[tokio::test]
async fn repeat_lt_one_errors() {
    let dir = tmp_root();
    let opts = ExecuteOptions {
        repeat: 0,
        ..Default::default()
    };
    let err = execute_scenario(dir.path(), "smoke", &opts)
        .await
        .unwrap_err();
    assert!(err.to_string().contains("repeat must be >= 1"), "{err}");
}

#[tokio::test]
async fn pass_at_k_gt_repeat_errors() {
    let dir = tmp_root();
    let opts = ExecuteOptions {
        repeat: 2,
        pass_at_k: Some(3),
        ..Default::default()
    };
    let err = execute_scenario(dir.path(), "smoke", &opts)
        .await
        .unwrap_err();
    assert!(
        err.to_string()
            .contains("pass_at_k (3) cannot exceed repeat (2)"),
        "{err}"
    );
}

#[tokio::test]
async fn invalid_scenario_returns_executed_false() {
    let dir = tmp_root();
    let opts = ExecuteOptions::single();
    // No scenario file → find_scenario fails → executed=false validation envelope.
    let result = execute_scenario(dir.path(), "no-such-scenario", &opts)
        .await
        .unwrap();
    assert_eq!(
        result.get("executed").and_then(|v| v.as_bool()),
        Some(false)
    );
    let validation = result
        .get("validation")
        .and_then(|v| v.as_object())
        .unwrap();
    assert_eq!(
        validation.get("valid").and_then(|v| v.as_bool()),
        Some(false)
    );
    let err = validation.get("error").and_then(|v| v.as_str()).unwrap();
    assert!(err.contains("no-such-scenario"), "{err}");
}

#[test]
fn transport_drop_detection() {
    let mut m = serde_json::Map::new();
    let mut summary = serde_json::Map::new();
    summary.insert("end_reason".into(), json!("gemini_socket_drop"));
    m.insert("summary".into(), json!(summary));
    assert!(is_transport_drop(&m));

    let mut m2 = serde_json::Map::new();
    m2.insert("summary".into(), json!({"end_reason": "sim_end_call"}));
    assert!(!is_transport_drop(&m2));

    let mut m3 = serde_json::Map::new();
    m3.insert("summary".into(), json!({}));
    assert!(!is_transport_drop(&m3));
    assert!(!is_transport_drop(&serde_json::Map::new()));
}

#[test]
fn execute_options_defaults() {
    let o = ExecuteOptions::single();
    assert_eq!(o.repeat, 1);
    assert!(o.pass_at_k.is_none());
    assert!(o.run_name.is_none());
    assert!(o.agent_name.is_none());
    assert!(o.optimized.is_none());
    assert!(o.profile.is_none());
}
