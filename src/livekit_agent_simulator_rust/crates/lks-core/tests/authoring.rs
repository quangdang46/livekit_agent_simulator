//! Tests for authoring scaffolds — init_project / init_scenario.
use lks_core::authoring::{init_project, init_scenario};
use serde_json::Value;
use std::path::PathBuf;

fn temp_root(name: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("lks_p1_auth_{name}"));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

#[test]
fn init_project_scaffolds_dot_agent_sim() {
    let root = temp_root("project");
    let res = init_project(&root).expect("init_project");
    let created = res["created"].as_array().expect("created list");
    assert!(!created.is_empty(), "something created");
    // .agent-sim/ dirs + files exist
    assert!(root.join(".agent-sim").join("config.yaml").exists());
    assert!(root
        .join(".agent-sim")
        .join("scenarios")
        .join("smoke-hello.yaml")
        .exists());
    assert!(root
        .join(".agent-sim")
        .join("plugins")
        .join("example_verify.py")
        .exists());
    assert!(root
        .join(".agent-sim")
        .join("cues")
        .join("README.md")
        .exists());
    // gitignore has .agent-sim/
    let gi = std::fs::read_to_string(root.join(".gitignore")).unwrap_or_default();
    assert!(
        gi.contains(".agent-sim/"),
        "gitignore has .agent-sim/: {gi}"
    );
    // idempotent: second call creates nothing new
    let res2 = init_project(&root).expect("init_project again");
    assert_eq!(
        res2["created"].as_array().map(|a| a.len()),
        Some(0),
        "idempotent"
    );
}

#[test]
fn init_scenario_scaffolds_yaml() {
    let root = temp_root("scenario");
    // Pre-create .agent-sim so init_project isn't needed.
    std::fs::create_dir_all(root.join(".agent-sim").join("scenarios")).unwrap();
    let res = init_scenario(&root, "my-test", false).expect("init_scenario");
    assert_eq!(res["scenario_id"], Value::String("my-test".into()));
    let path = root
        .join(".agent-sim")
        .join("scenarios")
        .join("my-test.yaml");
    assert!(path.exists(), "scaffold written");
    // id substituted (the scaffold emits `id: 'my-test'` — quoted)
    let text = std::fs::read_to_string(&path).unwrap();
    assert!(!text.contains("{{SCENARIO_ID}}"), "id substituted: {text}");
    assert!(text.contains("my-test"), "id present: {text}");
    // parses as a valid scenario
    let s = lks_core::scenario_yaml::load_scenario_yaml(&path).expect("parses");
    assert_eq!(s.id, "my-test");
}

#[test]
fn init_scenario_existing_requires_force() {
    let root = temp_root("scenario_force");
    std::fs::create_dir_all(root.join(".agent-sim").join("scenarios")).unwrap();
    init_scenario(&root, "dup", false).expect("first");
    let err = init_scenario(&root, "dup", false).expect_err("no force fails");
    assert!(err.to_string().contains("already exists"), "got: {err}");
    init_scenario(&root, "dup", true).expect("force overwrites");
}

#[test]
fn init_scenario_invalid_id() {
    let root = temp_root("scenario_bad");
    let err = init_scenario(&root, "bad id!", false).expect_err("invalid id");
    assert!(
        err.to_string()
            .contains("use letters/digits/[_-], start with alnum, max 64 chars"),
        "got: {err}"
    );
}

#[test]
fn build_authoring_report_structure() {
    use lks_core::authoring_warnings::build_authoring_report;
    use serde_json::{json, Map};
    let mut persona = Map::new();
    persona.insert("brief".into(), json!("Test caller"));
    persona.insert("goals".into(), json!(["Goal one"]));
    let report = build_authoring_report(
        &persona,
        &["smoke".to_string()],
        &[],
        None,
        None,
        None,
        &lks_core::scenario::SimulatorSpec {
            max_turns: 6,
            timeout_s: 120,
            first_speaker: "agent".into(),
        },
        None,
    );
    // scorecard present with 6 dims, max 12
    let scorecard = report.get("scorecard").and_then(|v| v.as_object()).unwrap();
    assert_eq!(scorecard.get("max").and_then(|v| v.as_i64()), Some(12));
    let dims = scorecard
        .get("dimensions")
        .and_then(|v| v.as_object())
        .unwrap();
    assert_eq!(dims.len(), 6);
    // tier + message + soft
    assert!(report.get("tier").and_then(|v| v.as_str()).is_some());
    let msg = report.get("message").and_then(|v| v.as_str()).unwrap();
    assert!(msg.contains("authoring tier="), "{msg}");
    assert_eq!(report.get("soft").and_then(|v| v.as_bool()), Some(true));
    // warning_codes/info_codes arrays
    assert!(report
        .get("warning_codes")
        .and_then(|v| v.as_array())
        .is_some());
    assert!(report
        .get("info_codes")
        .and_then(|v| v.as_array())
        .is_some());
}
