//! Tests for the PyYAML-compatible YAML writer (scenario export / convert).
//! Round-trips a parsed scenario through to_yaml_string and re-parses.
use lks_core::scenario_jsonl::parse_scenario_jsonl;
use lks_core::scenario_yaml::{load_scenario_yaml, scenario_to_yaml_text};
use lks_core::yaml_writer::{clean, to_yaml_string};
use serde_json::json;
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
fn yaml_writer_roundtrip_jsonl_to_yaml() {
    // Parse the JSONL, serialize to YAML, parse the YAML back → same Scenario id.
    let jsonl = parse_scenario_jsonl(&template("smoke-hello.jsonl")).expect("parse jsonl");
    let yaml_text = scenario_to_yaml_text(&jsonl);
    assert!(
        yaml_text.contains("apiVersion: agent-sim/v1"),
        "has header: {yaml_text}"
    );
    assert!(
        yaml_text.contains("kind: Scenario"),
        "has kind: {yaml_text}"
    );
    // Parse the emitted YAML back.
    let dir = std::env::temp_dir().join("lks_p1_yaml_writer_rt");
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let p = dir.join("smoke-hello.yaml");
    std::fs::write(&p, &yaml_text).unwrap();
    let back = load_scenario_yaml(&p).expect("reparse yaml");
    assert_eq!(back.id, jsonl.id);
    assert_eq!(back.effective_caller_mode(), jsonl.effective_caller_mode());
}

#[test]
fn yaml_writer_digit_quoting() {
    // Quote behavior verified against real PyYAML on 2026-08-13.
    let obj = json!({
        "a": "123",
        "b": "0",
        "c": "00",
        "d": "09",
        "e": "0x1F",
        "f": "1e5",
        "g": "-1",
        "h": "1.5",
        "i": "1_000",
        "j": "hello world",
    });
    let cleaned = clean(obj).expect("clean");
    let out = to_yaml_string(&cleaned);
    // Quoted (matches PyYAML dump): 123, 0, 00, 0x1F, 1e5, -1, 1.5, 1_000
    assert!(out.contains("a: '123'"), "a quoted: {out}");
    assert!(out.contains("b: '0'"), "b quoted: {out}");
    assert!(out.contains("c: '00'"), "c quoted: {out}");
    assert!(out.contains("e: '0x1F'"), "e quoted: {out}");
    assert!(out.contains("f: '1e5'"), "f quoted: {out}");
    assert!(out.contains("g: '-1'"), "g quoted: {out}");
    assert!(out.contains("h: '1.5'"), "h quoted: {out}");
    assert!(out.contains("i: '1_000'"), "i quoted: {out}");
    // Bare words pass through.
    assert!(out.contains("j: hello world"), "j plain: {out}");
}

#[test]
fn yaml_writer_clean_drops_none_and_empty() {
    let obj = json!({
        "keep": "value",
        "drop_none": null,
        "drop_empty_obj": {},
        "drop_empty_list": [],
        "keep_nested": {"x": 1},
    });
    let cleaned = clean(obj).expect("clean");
    let out = to_yaml_string(&cleaned);
    assert!(out.contains("keep: value"));
    assert!(!out.contains("drop_none"), "null dropped: {out}");
    assert!(!out.contains("drop_empty_obj"), "empty obj dropped: {out}");
    assert!(
        !out.contains("drop_empty_list"),
        "empty list dropped: {out}"
    );
    assert!(out.contains("keep_nested:"), "nested kept: {out}");
}

#[test]
fn yaml_writer_nested_structures() {
    let obj = json!({
        "persona": {"name": "Alex", "goals": ["a", "b"]},
        "execute": {"max_turns": 2, "first_speaker": "user"},
        "pass_criteria": {"criteria": ["c1", "c2"], "mode": "all"},
    });
    let out = to_yaml_string(&obj);
    assert!(out.contains("persona:"), "persona key: {out}");
    assert!(out.contains("name: Alex"), "nested scalar: {out}");
    assert!(out.contains("- a"), "sequence item: {out}");
    assert!(out.contains("max_turns: 2"), "int: {out}");
}

#[test]
fn yaml_writer_roundtrip_scaffold() {
    // The scaffold template round-trips (id/locale preserved).
    let jsonl = parse_scenario_jsonl(&template("scenario-scaffold.jsonl")).expect("parse");
    let yaml_text = scenario_to_yaml_text(&jsonl);
    let dir = std::env::temp_dir().join("lks_p1_yaml_writer_scaffold");
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let p = dir.join("scaffold.yaml");
    std::fs::write(&p, &yaml_text).unwrap();
    let back = load_scenario_yaml(&p).expect("reparse scaffold");
    assert_eq!(back.id, jsonl.id);
    assert_eq!(back.locale, jsonl.locale);
}
