//! Golden tests for JSONL scenario parsing (P1) — byte-exact contracts vs
//! scenario.py::parse_scenario (JSONL branch). Uses real templates/*.jsonl.
use lks_core::scenario_jsonl::parse_scenario_jsonl;
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
fn golden_jsonl_scaffold_parses() {
    let s =
        parse_scenario_jsonl(&template("scenario-scaffold.jsonl")).expect("parse scaffold jsonl");
    assert_eq!(s.id, "{{SCENARIO_ID}}");
    assert_eq!(s.locale, "en-US");
    // persona brief present (JSONL message differs from YAML)
    let brief = s
        .persona
        .get("brief")
        .and_then(|v| v.as_str())
        .unwrap_or("");
    assert!(!brief.is_empty(), "persona brief present");
    // simulator defaults
    assert_eq!(s.simulator.max_turns, 6);
    assert_eq!(s.simulator.first_speaker, "agent");
}

#[test]
fn golden_jsonl_smoke_hello_parses() {
    let s = parse_scenario_jsonl(&template("smoke-hello.jsonl")).expect("parse smoke-hello jsonl");
    assert_eq!(s.id, "smoke-hello");
    // execute override
    let ex = s.execute.as_ref().expect("execute");
    assert_eq!(ex.max_turns, Some(2));
    assert_eq!(ex.first_speaker.as_deref(), Some("user"));
}

#[test]
fn jsonl_invalid_json_line() {
    let dir = std::env::temp_dir().join("lks_p1_jsonl_invalid");
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let p = dir.join("bad.jsonl");
    std::fs::write(
        &p,
        "{\"kind\":\"Scenario\",\"apiVersion\":\"agent-sim/v1\",\"spec\":{\"metadata\":{\"id\":\"x\"}}}\nthis is not json\n",
    )
    .unwrap();
    let err = parse_scenario_jsonl(&p).expect_err("bad json line must fail");
    assert!(err.to_string().contains("invalid JSON"), "got: {err}");
}

#[test]
fn jsonl_header_must_be_scenario() {
    let dir = std::env::temp_dir().join("lks_p1_jsonl_header");
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let p = dir.join("bad-header.jsonl");
    std::fs::write(&p, "{\"kind\":\"Persona\",\"spec\":{\"brief\":\"Hi\"}}\n").unwrap();
    let err = parse_scenario_jsonl(&p).expect_err("header must be Scenario");
    assert!(
        err.to_string()
            .contains("first line must have kind=Scenario"),
        "got: {err}"
    );
}

#[test]
fn jsonl_unknown_kind() {
    let dir = std::env::temp_dir().join("lks_p1_jsonl_kind");
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let p = dir.join("bad-kind.jsonl");
    std::fs::write(
        &p,
        "{\"kind\":\"Scenario\",\"apiVersion\":\"agent-sim/v1\",\"metadata\":{\"id\":\"x\"}}\n{\"kind\":\"Bogus\",\"spec\":{}}\n",
    )
    .unwrap();
    let err = parse_scenario_jsonl(&p).expect_err("unknown kind must fail");
    assert!(err.to_string().contains("unknown kind"), "got: {err}");
}

#[test]
fn jsonl_persona_brief_required_message() {
    let dir = std::env::temp_dir().join("lks_p1_jsonl_brief");
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let p = dir.join("no-brief.jsonl");
    std::fs::write(
        &p,
        "{\"kind\":\"Scenario\",\"apiVersion\":\"agent-sim/v1\",\"metadata\":{\"id\":\"x\"}}\n{\"kind\":\"Persona\",\"spec\":{\"name\":\"A\"}}\n",
    )
    .unwrap();
    let err = parse_scenario_jsonl(&p).expect_err("no brief must fail");
    // JSONL-specific message
    assert!(
        err.to_string()
            .contains("Persona.spec.brief is required — the simulator needs a caller brief"),
        "got: {err}"
    );
}

#[test]
fn jsonl_extension_keys_stripped() {
    // strip_extension_keys strips TOP-LEVEL `_`-prefixed keys of each record
    // ({kind, spec} wrapper), NOT keys inside spec (verified against Python:
    // spec._doc remains in persona). A top-level `_doc` on the record is dropped.
    let dir = std::env::temp_dir().join("lks_p1_jsonl_ext");
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let p = dir.join("ext.jsonl");
    std::fs::write(
        &p,
        "{\"kind\":\"Scenario\",\"apiVersion\":\"agent-sim/v1\",\"metadata\":{\"id\":\"x\"},\"_doc\":\"note\"}\n{\"kind\":\"Persona\",\"spec\":{\"brief\":\"Hi\",\"_doc\":\"note\"}}\n",
    )
    .unwrap();
    let s = parse_scenario_jsonl(&p).expect("parses with extension keys");
    // Top-level _doc on the header record is stripped (doesn't break parsing).
    assert_eq!(s.id, "x");
    // spec._doc is NOT stripped (matches Python) — persona keeps it.
    assert_eq!(
        s.persona.get("_doc").and_then(|v| v.as_str()),
        Some("note"),
        "spec._doc preserved (matches Python strip_extension_keys semantics)"
    );
}

#[test]
fn jsonl_plugins_extend() {
    let dir = std::env::temp_dir().join("lks_p1_jsonl_plugins");
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let p = dir.join("plugins.jsonl");
    std::fs::write(
        &p,
        "{\"kind\":\"Scenario\",\"apiVersion\":\"agent-sim/v1\",\"metadata\":{\"id\":\"x\"}}\n{\"kind\":\"Persona\",\"spec\":{\"brief\":\"Hi\"}}\n{\"kind\":\"Plugins\",\"spec\":{\"modules\":[\"a\"]}}\n{\"kind\":\"Plugins\",\"spec\":{\"modules\":[\"b\"]}}\n",
    )
    .unwrap();
    let s = parse_scenario_jsonl(&p).expect("parses");
    // Plugins EXTENDS (appends) → both a and b
    assert_eq!(s.plugin_modules, vec!["a", "b"]);
}
