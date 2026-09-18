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

/// Dead-endpoint run fails as an ENVELOPE, fast, with no livekit server.
///
/// Regression gate for PR #109/#110 (12 CI attempts): a scenario whose
/// config points at a non-existent LiveKit server must resolve to
/// {executed:true, status:"failed", error: <connect/dispatch failure>}
/// — never a hang, never an Err that a caller would have to map into a
/// protocol error. Runs DIRECTLY against execute_scenario (no stdio hop):
/// the MCP stdio transport cannot flush a tool response after livekit's
/// native webrtc machinery wedges the child's runtime, so the MCP harness
/// deliberately does NOT cover this path — this test is its home.
/// Bounded at 150s (15s room + 15s dispatch + 15s list_participants +
/// bridge ceiling headroom); the pre-fix shape hung past the 45-minute
/// runner timeout, so any regression is unmistakable.
///
/// NOTE: multi_thread flavor is REQUIRED here, not optional. The run path
/// awaits livekit's native webrtc machinery, whose straggler tasks must be
/// polled by a *different* worker than the one parked on the outer future:
/// on #[tokio::test]'s default current_thread runtime the executor has a
/// single thread, and a wedged native await starves the very timeout meant
/// to bound it (PR #110, 13th attempt: 150s outer timeout never fired).
/// This flavor requirement IS the regression mechanism, not trivia.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn dead_endpoint_fails_fast_as_envelope() {
    let dir = tmp_root();
    // Scaffold a VALID scenario so the run reaches the bridge (not the
    // validation early-return above).
    std::fs::write(
        dir.path()
            .join(".agent-sim")
            .join("scenarios")
            .join("smoke.yaml"),
        "apiVersion: agent-sim/v1\nkind: Scenario\nmetadata:\n  id: smoke\npersona:\n  brief: Test caller brief\n  goals:\n    - Say hello\n",
    )
    .unwrap();
    // Point at a dead local endpoint: connection refused (not DNS hang),
    // so the failure is deterministic and fast.
    std::fs::write(
        dir.path().join(".agent-sim").join("config.yaml"),
        "livekit:\n  url: ws://127.0.0.1:9\n  api_key: test-key-0123456789abcdef\n  api_secret: test-secret-0123456789abcdef\n  agent_name: test-agent\nsimulator:\n  provider: openai\n  mode: realtime\n  api_key: sk-test-key-1234567890\n",
    )
    .unwrap();
    let opts = ExecuteOptions::single();
    let result = tokio::time::timeout(
        std::time::Duration::from_secs(150),
        execute_scenario(dir.path(), "smoke", &opts),
    )
    .await
    .expect("dead-endpoint run must resolve within 150s (regression: PR #109/#110 hang)");
    let envelope = result.expect("dead-endpoint run is an envelope, not Err");
    let rendered = serde_json::to_string(&envelope).unwrap_or_default();
    assert_eq!(
        envelope.get("executed").and_then(|v| v.as_bool()),
        Some(true),
        "envelope marks executed: {rendered}"
    );
    assert_eq!(
        envelope.get("status").and_then(|v| v.as_str()),
        Some("failed"),
        "dead endpoint fails: {rendered}"
    );
    assert!(
        rendered.contains("timed out")
            || rendered.contains("connect failed")
            || rendered.contains("dispatch failed")
            || rendered.contains("Connection refused"),
        "error names the dead endpoint: {rendered}"
    );
}
