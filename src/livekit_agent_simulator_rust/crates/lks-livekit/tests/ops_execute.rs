//! ops_execute parity — execute_scenarios validation + execute_scenario_dict
//! validation shapes (offline; the runner itself needs livekit).

use lks_livekit::ops_execute::{op_execute_scenario_dict, op_execute_scenarios, SuiteOptions};
use serde_json::json;

fn tmp_root() -> tempfile::TempDir {
    let dir = tempfile::tempdir().unwrap();
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
async fn suite_parallel_lt_one_errors() {
    let dir = tmp_root();
    let opts = SuiteOptions {
        parallel: 0,
        ..Default::default()
    };
    let err = op_execute_scenarios(dir.path(), &opts).await.unwrap_err();
    assert!(err.to_string().contains("parallel must be >= 1"), "{err}");
}

#[tokio::test]
async fn suite_wait_lt_zero_errors() {
    let dir = tmp_root();
    let opts = SuiteOptions {
        parallel: 1,
        wait_s: -1.0,
        ..Default::default()
    };
    let err = op_execute_scenarios(dir.path(), &opts).await.unwrap_err();
    assert!(err.to_string().contains("wait_s must be >= 0"), "{err}");
}

#[tokio::test]
async fn suite_no_scenarios_empty_matrix() {
    let dir = tmp_root();
    let opts = SuiteOptions {
        parallel: 1,
        ..Default::default()
    };
    // No scenario files → empty targets → suite with total 0, ok true.
    let result = op_execute_scenarios(dir.path(), &opts).await.unwrap();
    assert_eq!(result.get("count").and_then(|v| v.as_i64()), Some(0));
    let suite = result.get("suite").and_then(|v| v.as_object()).unwrap();
    assert_eq!(suite.get("ok").and_then(|v| v.as_bool()), Some(true));
    assert_eq!(suite.get("exit_code").and_then(|v| v.as_i64()), Some(0));
}

#[tokio::test]
async fn dict_missing_persona_brief_returns_executed_false() {
    let dir = tmp_root();
    let scenario = json!({
        "id": "dict-test",
        "simulator": {"max_turns": 2},
        "persona": {"goals": ["Say hi"]},  // no brief → validation error
    })
    .as_object()
    .unwrap()
    .clone();
    let result = op_execute_scenario_dict(dir.path(), &scenario, None, None, None, None)
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
    assert!(err.contains("persona.brief is required"), "{err}");
}

#[tokio::test]
async fn dict_missing_id_returns_executed_false() {
    let dir = tmp_root();
    let scenario = json!({
        "persona": {"brief": "Hi there"},
    })
    .as_object()
    .unwrap()
    .clone();
    let result = op_execute_scenario_dict(dir.path(), &scenario, None, None, None, None)
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
    let err = validation.get("error").and_then(|v| v.as_str()).unwrap();
    assert!(err.contains("id or metadata.id is required"), "{err}");
}
