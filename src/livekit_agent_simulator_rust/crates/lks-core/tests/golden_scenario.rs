//! Golden tests for scenario parsing (P1) — byte-exact contracts vs scenario.py /
//! scenario_yaml.py. Uses the real templates/*.yaml as input.
use lks_core::errors::ScenarioError;
use lks_core::scenario_yaml::load_scenario_yaml;
use std::path::PathBuf;

fn repo_root() -> PathBuf {
    let mut p = std::env::current_dir().unwrap();
    for _ in 0..4 {
        p.pop();
    }
    p
}

fn template(name: &str) -> PathBuf {
    repo_root().join("templates").join(name)
}

#[test]
fn golden_scenario_smoke_hello_parses() {
    let s = load_scenario_yaml(&template("smoke-hello.yaml")).expect("parse smoke-hello");
    assert_eq!(s.id, "smoke-hello");
    assert_eq!(s.locale, "en-US");
    assert_eq!(s.tags, vec!["smoke"]);
    // persona
    assert_eq!(s.persona.get("name").and_then(|v| v.as_str()), Some("Alex"));
    let brief = s
        .persona
        .get("brief")
        .and_then(|v| v.as_str())
        .unwrap_or("");
    assert!(brief.contains("calling a business"), "brief: {brief}");
    let goals = s.persona.get("goals").and_then(|v| v.as_array());
    assert_eq!(goals.map(|a| a.len()), Some(2));
    // execute
    let ex = s.execute.as_ref().expect("execute present");
    assert_eq!(ex.max_turns, Some(2));
    assert_eq!(ex.timeout_s, Some(90));
    assert_eq!(ex.first_speaker.as_deref(), Some("user"));
    // simulator default
    assert_eq!(s.simulator.max_turns, 6);
    // run_spec: execute overrides simulator
    let rs = s.run_spec();
    assert_eq!(rs.max_turns, 2);
    assert_eq!(rs.timeout_s, 90);
    assert_eq!(rs.first_speaker, "user");
    // pass_criteria
    assert_eq!(s.pass_criteria.len(), 2);
    assert_eq!(s.pass_criteria[0], "The agent responded to the caller");
    // caller default
    assert_eq!(s.effective_caller_mode(), "webrtc_sim");
}

#[test]
fn golden_scenario_scaffold_parses() {
    // scaffold has placeholders; must parse (persona.brief required and present).
    let s = load_scenario_yaml(&template("scenario-scaffold.yaml")).expect("parse scaffold");
    assert_eq!(s.id, "{{SCENARIO_ID}}");
    assert_eq!(s.locale, "en-US");
    assert!(s.persona.get("brief").is_some());
    // dispatch metadata present (must be valid JSON)
    let d = s.dispatch.as_ref().expect("dispatch present");
    let meta = d.metadata.as_deref().expect("metadata");
    assert!(
        serde_json::from_str::<serde_json::Value>(meta).is_ok(),
        "dispatch.metadata valid JSON: {meta}"
    );
    // execute overrides
    let ex = s.execute.as_ref().expect("execute");
    assert_eq!(ex.max_turns, Some(4));
    assert_eq!(ex.timeout_s, Some(120));
}

#[test]
fn golden_scenario_jsonl_twin_parses_as_yaml() {
    // Legacy .jsonl scenarios also have .yaml twins in templates.
    let s =
        load_scenario_yaml(&template("inbound-caller-sim.yaml")).expect("parse inbound-caller-sim");
    assert!(!s.id.is_empty());
}

#[test]
fn scenario_persona_brief_required() {
    // persona without brief → error "{path_label}: persona.brief is required"
    let dir = std::env::temp_dir().join("lks_p1_scen_brief");
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let p = dir.join("no-brief.yaml");
    std::fs::write(
        &p,
        "apiVersion: agent-sim/v1\nkind: Scenario\nmetadata:\n  id: no-brief\npersona:\n  name: Alex\n",
    )
    .unwrap();
    let err = load_scenario_yaml(&p).expect_err("persona without brief must fail");
    assert!(
        err.to_string().contains("persona.brief is required"),
        "got: {err}"
    );
}

#[test]
fn scenario_missing_id_required() {
    let dir = std::env::temp_dir().join("lks_p1_scen_id");
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let p = dir.join("no-id.yaml");
    std::fs::write(
        &p,
        "apiVersion: agent-sim/v1\nkind: Scenario\npersona:\n  brief: Hi\n",
    )
    .unwrap();
    let err = load_scenario_yaml(&p).expect_err("missing id must fail");
    assert!(
        err.to_string().contains("id or metadata.id is required"),
        "got: {err}"
    );
}

#[test]
fn scenario_caller_mode_invalid() {
    let dir = std::env::temp_dir().join("lks_p1_scen_caller");
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let p = dir.join("bad-caller.yaml");
    std::fs::write(
        &p,
        "apiVersion: agent-sim/v1\nkind: Scenario\nmetadata:\n  id: bad-caller\npersona:\n  brief: Hi\ncaller:\n  mode: bad_mode\n",
    )
    .unwrap();
    let err = load_scenario_yaml(&p).expect_err("bad caller.mode must fail");
    assert!(
        err.to_string().contains("caller.mode must be one of"),
        "got: {err}"
    );
}

#[test]
fn scenario_empty_file() {
    let dir = std::env::temp_dir().join("lks_p1_scen_empty");
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let p = dir.join("empty.yaml");
    std::fs::write(&p, "").unwrap();
    let err = load_scenario_yaml(&p).expect_err("empty file must fail");
    assert!(
        err.to_string().contains("empty scenario file"),
        "got: {err}"
    );
}

#[test]
fn scenario_dispatch_bad_json() {
    let dir = std::env::temp_dir().join("lks_p1_scen_dispatch");
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let p = dir.join("bad-dispatch.yaml");
    std::fs::write(
        &p,
        "apiVersion: agent-sim/v1\nkind: Scenario\nmetadata:\n  id: bad-dispatch\npersona:\n  brief: Hi\ndispatch:\n  metadata: \"{not json}\"\n",
    )
    .unwrap();
    let err = load_scenario_yaml(&p).expect_err("bad dispatch.metadata must fail");
    assert!(
        err.to_string()
            .contains("dispatch.metadata must be valid JSON"),
        "got: {err}"
    );
}

#[test]
fn scenario_first_speaker_invalid() {
    let dir = std::env::temp_dir().join("lks_p1_scen_fs");
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let p = dir.join("bad-fs.yaml");
    std::fs::write(
        &p,
        "apiVersion: agent-sim/v1\nkind: Scenario\nmetadata:\n  id: bad-fs\npersona:\n  brief: Hi\nsimulator:\n  first_speaker: robot\n",
    )
    .unwrap();
    let err = load_scenario_yaml(&p).expect_err("bad first_speaker must fail");
    assert!(
        err.to_string()
            .contains("first_speaker must be agent or user"),
        "got: {err}"
    );
}

#[test]
fn scenario_error_type_is_scenarioerror() {
    fn takes(_: &ScenarioError) {}
    let dir = std::env::temp_dir().join("lks_p1_scen_type");
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let p = dir.join("missing.yaml");
    std::fs::write(&p, "").unwrap();
    match load_scenario_yaml(&p) {
        Err(e) => takes(&e),
        Ok(_) => panic!("must fail"),
    }
}
