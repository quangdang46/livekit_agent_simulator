//! Tests for scenario discovery ops — find_scenario / list_scenarios / export.
//! Uses a temp scenarios dir (not the repo templates, to control contents).
use lks_core::scenario_ops::{
    export_scenario, find_scenario, is_valid_scenario_id, list_scenarios,
};
use serde_json::Value;
use std::path::PathBuf;

fn temp_scenarios_dir(name: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("lks_p1_ops_{name}"));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

fn write_scenario(dir: &PathBuf, fname: &str, body: &str) {
    std::fs::write(dir.join(fname), body).unwrap();
}

const HEADER: &str = "{\"apiVersion\":\"agent-sim/v1\",\"kind\":\"Scenario\",\"metadata\":{\"id\":\"smoke\",\"locale\":\"en-US\"}}\n";

#[test]
fn find_scenario_direct_yaml() {
    let dir = temp_scenarios_dir("direct_yaml");
    write_scenario(
        &dir,
        "smoke.yaml",
        "apiVersion: agent-sim/v1\nkind: Scenario\nmetadata:\n  id: smoke\npersona:\n  brief: Hi\n",
    );
    let s = find_scenario(&dir, "smoke").expect("find by yaml");
    assert_eq!(s.id, "smoke");
}

#[test]
fn find_scenario_yaml_shadows_jsonl() {
    // Both smoke.jsonl and smoke.yaml exist → YAML wins.
    let dir = temp_scenarios_dir("shadow");
    write_scenario(
        &dir,
        "smoke.jsonl",
        "{\"apiVersion\":\"agent-sim/v1\",\"kind\":\"Scenario\",\"metadata\":{\"id\":\"shadowed\"}}\n{\"kind\":\"Persona\",\"spec\":{\"brief\":\"Hi\"}}\n",
    );
    write_scenario(
        &dir,
        "smoke.yaml",
        "apiVersion: agent-sim/v1\nkind: Scenario\nmetadata:\n  id: smoke\npersona:\n  brief: Hi\n",
    );
    let s = find_scenario(&dir, "smoke").expect("yaml shadows");
    assert_eq!(s.id, "smoke", "yaml id wins over jsonl id");
}

#[test]
fn find_scenario_invalid_id() {
    let dir = temp_scenarios_dir("bad_id");
    let err = find_scenario(&dir, "bad id!").expect_err("invalid id fails");
    assert!(
        err.to_string()
            .contains("use letters/digits/[_-], start with alnum, max 64 chars"),
        "got: {err}"
    );
}

#[test]
fn find_scenario_not_found() {
    let dir = temp_scenarios_dir("not_found");
    let err = find_scenario(&dir, "nope").expect_err("not found");
    assert!(
        err.to_string().contains("Scenario `nope` not found in"),
        "got: {err}"
    );
}

#[test]
fn list_scenarios_yaml_shadows() {
    let dir = temp_scenarios_dir("list_shadow");
    write_scenario(
        &dir,
        "a.jsonl",
        "{\"apiVersion\":\"agent-sim/v1\",\"kind\":\"Scenario\",\"metadata\":{\"id\":\"a\"}}\n{\"kind\":\"Persona\",\"spec\":{\"brief\":\"Hi\"}}\n",
    );
    write_scenario(
        &dir,
        "a.yaml",
        "apiVersion: agent-sim/v1\nkind: Scenario\nmetadata:\n  id: a\npersona:\n  brief: Hi\n",
    );
    write_scenario(
        &dir,
        "b.yaml",
        "apiVersion: agent-sim/v1\nkind: Scenario\nmetadata:\n  id: b\npersona:\n  brief: Hi\n",
    );
    let list = list_scenarios(&dir);
    // a.jsonl is shadowed by a.yaml → only 2 entries (a.yaml, b.yaml)
    assert_eq!(list.len(), 2, "jsonl shadowed: {list:?}");
    let ids: Vec<String> = list
        .iter()
        .filter_map(|m| m.get("id").and_then(|v| v.as_str()).map(String::from))
        .collect();
    assert!(ids.contains(&"a".to_string()));
    assert!(ids.contains(&"b".to_string()));
}

#[test]
fn list_scenarios_invalid_included_with_error() {
    let dir = temp_scenarios_dir("list_invalid");
    write_scenario(
        &dir,
        "bad.yaml",
        "apiVersion: agent-sim/v1\nkind: Scenario\nmetadata:\n  id: bad\npersona:\n  nope\n",
    );
    let list = list_scenarios(&dir);
    assert_eq!(list.len(), 1);
    assert!(
        list[0].get("error").is_some(),
        "invalid file has error: {list:?}"
    );
    assert_eq!(list[0].get("id"), Some(&Value::Null));
}

#[test]
fn export_scenario_found_and_notfound() {
    let dir = temp_scenarios_dir("export");
    write_scenario(
        &dir,
        "smoke.yaml",
        "apiVersion: agent-sim/v1\nkind: Scenario\nmetadata:\n  id: smoke\npersona:\n  brief: Hi\n",
    );
    let found = export_scenario(&dir, "smoke");
    assert_eq!(found.get("found"), Some(&Value::Bool(true)));
    assert_eq!(
        found
            .get("metadata")
            .and_then(|m| m.get("id"))
            .and_then(|v| v.as_str()),
        Some("smoke")
    );
    let notfound = export_scenario(&dir, "ghost");
    assert_eq!(notfound.get("found"), Some(&Value::Bool(false)));
    assert!(notfound.get("error").is_some());
}

#[test]
fn is_valid_scenario_id_cases() {
    assert!(is_valid_scenario_id("smoke-hello"));
    assert!(is_valid_scenario_id("abc_123"));
    assert!(is_valid_scenario_id("a"));
    assert!(!is_valid_scenario_id("bad id!"));
    assert!(!is_valid_scenario_id("_leading"));
    assert!(!is_valid_scenario_id(""));
    assert!(!is_valid_scenario_id(&"a".repeat(65)));
}
