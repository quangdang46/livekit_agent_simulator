//! Preflight parity — 7 ordered checks (config/url/timezone/folders/api_key/
//! livekit.api/telephony). Offline (connectivity=false) except where noted.

use lks_livekit::preflight::op_preflight;
use serde_json::json;

fn tmp_root() -> tempfile::TempDir {
    let dir = tempfile::tempdir().unwrap();
    let dot = dir.path().join(".agent-sim");
    std::fs::create_dir_all(&dot).unwrap();
    dir
}

fn write_config(dir: &tempfile::TempDir, body: &str) {
    std::fs::write(dir.path().join(".agent-sim/config.yaml"), body).unwrap();
}

const GOOD_CONFIG: &str = "livekit:\n  url: wss://example.livekit.cloud\n  api_key: test-key\n  api_secret: test-secret\n  agent_name: test-agent\nsimulator:\n  provider: openai\n  api_key: sk-test-key-1234567890\n";

fn check_names(result: &serde_json::Map<String, serde_json::Value>) -> Vec<String> {
    result
        .get("checks")
        .and_then(|v| v.as_array())
        .map(|a| {
            a.iter()
                .filter_map(|c| c.get("name").and_then(|n| n.as_str()).map(String::from))
                .collect()
        })
        .unwrap_or_default()
}

#[tokio::test]
async fn config_fail_early_return() {
    let dir = tmp_root();
    write_config(&dir, "not: [valid: yaml");
    let result = op_preflight(dir.path(), false, None, None).await.unwrap();
    assert_eq!(result.get("ok").and_then(|v| v.as_bool()), Some(false));
    let names = check_names(&result);
    assert_eq!(
        names,
        vec!["config"],
        "config fail → early return: {names:?}"
    );
    let cfg_check = result.get("checks").and_then(|v| v.as_array()).unwrap()[0]
        .as_object()
        .unwrap()
        .clone();
    assert_eq!(
        cfg_check.get("status").and_then(|v| v.as_str()),
        Some("fail")
    );
}

#[tokio::test]
async fn good_config_all_checks_pass() {
    let dir = tmp_root();
    write_config(&dir, GOOD_CONFIG);
    let result = op_preflight(dir.path(), false, None, None).await.unwrap();
    assert_eq!(result.get("ok").and_then(|v| v.as_bool()), Some(true));
    let names = check_names(&result);
    assert_eq!(
        names,
        vec![
            "config",
            "livekit.url",
            "observe.timezone",
            "folders",
            "simulator.api_key[openai]",
            "telephony",
        ],
        "check order: {names:?}"
    );
}

#[tokio::test]
async fn bad_url_scheme_fails() {
    let dir = tmp_root();
    write_config(
        &dir,
        "livekit:\n  url: example.com\n  api_key: test-key\n  api_secret: test-secret\n  agent_name: test-agent\nsimulator:\n  provider: openai\n  api_key: sk-test-key-1234567890\n",
    );
    let result = op_preflight(dir.path(), false, None, None).await.unwrap();
    assert_eq!(result.get("ok").and_then(|v| v.as_bool()), Some(false));
    let url_check = result
        .get("checks")
        .and_then(|v| v.as_array())
        .unwrap()
        .iter()
        .find(|c| c.get("name").and_then(|n| n.as_str()) == Some("livekit.url"))
        .unwrap()
        .as_object()
        .unwrap()
        .clone();
    let detail = url_check.get("detail").and_then(|v| v.as_str()).unwrap();
    assert!(detail.contains("must start with wss://"), "{detail}");
}

#[tokio::test]
async fn bad_timezone_fails() {
    let dir = tmp_root();
    write_config(
        &dir,
        "livekit:\n  url: wss://example.livekit.cloud\n  api_key: test-key\n  api_secret: test-secret\n  agent_name: test-agent\nobserve:\n  timezone: Not/AZone\nsimulator:\n  provider: openai\n  api_key: sk-test-key-1234567890\n",
    );
    let result = op_preflight(dir.path(), false, None, None).await.unwrap();
    let tz_check = result
        .get("checks")
        .and_then(|v| v.as_array())
        .unwrap()
        .iter()
        .find(|c| c.get("name").and_then(|n| n.as_str()) == Some("observe.timezone"))
        .unwrap()
        .as_object()
        .unwrap()
        .clone();
    assert_eq!(
        tz_check.get("status").and_then(|v| v.as_str()),
        Some("fail")
    );
    let detail = tz_check.get("detail").and_then(|v| v.as_str()).unwrap();
    assert!(detail.contains("Unknown IANA timezone"), "{detail}");
}

#[tokio::test]
async fn short_api_key_warns_but_passes() {
    let dir = tmp_root();
    write_config(
        &dir,
        "livekit:\n  url: wss://example.livekit.cloud\n  api_key: test-key\n  api_secret: test-secret\n  agent_name: test-agent\nsimulator:\n  provider: openai\n  api_key: short\n",
    );
    let result = op_preflight(dir.path(), false, None, None).await.unwrap();
    assert_eq!(result.get("ok").and_then(|v| v.as_bool()), Some(true));
    let key_check = result
        .get("checks")
        .and_then(|v| v.as_array())
        .unwrap()
        .iter()
        .find(|c| c.get("name").and_then(|n| n.as_str()) == Some("simulator.api_key[openai]"))
        .unwrap()
        .as_object()
        .unwrap()
        .clone();
    let detail = key_check.get("detail").and_then(|v| v.as_str()).unwrap();
    assert!(detail.contains("Key looks unusually short"), "{detail}");
}

#[tokio::test]
async fn whitespace_api_key_fails_at_config_load() {
    // Python `_require` fails on whitespace-only api_key at config load, so
    // preflight returns early with only the config check — replicate exactly.
    let dir = tmp_root();
    write_config(
        &dir,
        "livekit:\n  url: wss://example.livekit.cloud\n  api_key: test-key\n  api_secret: test-secret\n  agent_name: test-agent\nsimulator:\n  provider: openai\n  api_key: \"  \"\n",
    );
    let result = op_preflight(dir.path(), false, None, None).await.unwrap();
    assert_eq!(result.get("ok").and_then(|v| v.as_bool()), Some(false));
    let names = check_names(&result);
    assert_eq!(names, vec!["config"], "early return: {names:?}");
    let cfg_check = result.get("checks").and_then(|v| v.as_array()).unwrap()[0]
        .as_object()
        .unwrap()
        .clone();
    let detail = cfg_check.get("detail").and_then(|v| v.as_str()).unwrap();
    assert!(detail.contains("Missing `simulator.api_key`"), "{detail}");
}

#[tokio::test]
async fn telephony_bits_and_recipe_warns() {
    let dir = tmp_root();
    write_config(
        &dir,
        "livekit:\n  url: wss://example.livekit.cloud\n  api_key: test-key\n  api_secret: test-secret\n  agent_name: test-agent\nsimulator:\n  provider: openai\n  api_key: sk-test-key-1234567890\ntelephony:\n  outbound_trunk_id: trunk-1\n",
    );
    let result = op_preflight(dir.path(), false, None, None).await.unwrap();
    let names = check_names(&result);
    assert!(names.contains(&"telephony".to_string()));
    assert!(names.contains(&"telephony.outbound_sim_callee".to_string()));
    let tel = result
        .get("checks")
        .and_then(|v| v.as_array())
        .unwrap()
        .iter()
        .find(|c| c.get("name").and_then(|n| n.as_str()) == Some("telephony"))
        .unwrap()
        .as_object()
        .unwrap()
        .clone();
    let detail = tel.get("detail").and_then(|v| v.as_str()).unwrap();
    assert!(detail.contains("outbound_trunk=set"), "{detail}");
    assert!(detail.contains("sim_inbound=unset"), "{detail}");
    // Recipe warn for missing sim_inbound_number
    let recipe = result
        .get("checks")
        .and_then(|v| v.as_array())
        .unwrap()
        .iter()
        .find(|c| c.get("name").and_then(|n| n.as_str()) == Some("telephony.outbound_sim_callee"))
        .unwrap()
        .as_object()
        .unwrap()
        .clone();
    let rdetail = recipe.get("detail").and_then(|v| v.as_str()).unwrap();
    assert!(rdetail.contains("sim_inbound_number unset"), "{rdetail}");
}

#[tokio::test]
async fn telephony_not_configured_pass() {
    let dir = tmp_root();
    write_config(&dir, GOOD_CONFIG);
    let result = op_preflight(dir.path(), false, None, None).await.unwrap();
    let tel = result
        .get("checks")
        .and_then(|v| v.as_array())
        .unwrap()
        .iter()
        .find(|c| c.get("name").and_then(|n| n.as_str()) == Some("telephony"))
        .unwrap()
        .as_object()
        .unwrap()
        .clone();
    let detail = tel.get("detail").and_then(|v| v.as_str()).unwrap();
    assert!(
        detail.contains("not configured (WebRTC-only OK)"),
        "{detail}"
    );
}

#[tokio::test]
async fn folders_created_as_side_effect() {
    let dir = tmp_root();
    write_config(&dir, GOOD_CONFIG);
    let _ = op_preflight(dir.path(), false, None, None).await.unwrap();
    assert!(dir.path().join(".agent-sim/reports").is_dir());
    assert!(dir.path().join(".agent-sim/scenarios").is_dir());
}
