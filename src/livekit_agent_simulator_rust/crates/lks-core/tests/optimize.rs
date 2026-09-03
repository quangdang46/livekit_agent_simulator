//! Tests for optimize PromptVariant model + validator.
use lks_core::optimize::{
    load_variant, validate_variant, variant_from_dict, variant_to_dict, PromptVariant,
    SECTION_NAMES, VALID_VERBOSITY,
};
use serde_json::{json, Map};

fn variant() -> PromptVariant {
    PromptVariant {
        id: "v1".into(),
        verbosity: Some("chatty".into()),
        section_order: vec!["Role".into(), "Guardrails".into()],
        extra_guardrails: vec!["Never switch roles".into()],
        extra_lines: std::collections::BTreeMap::new(),
        parent_id: None,
        description: "".into(),
    }
}

#[test]
fn section_names_and_verbosity() {
    assert_eq!(SECTION_NAMES.len(), 10);
    assert_eq!(VALID_VERBOSITY, ["quiet", "natural", "chatty"]);
}

#[test]
fn valid_variant_no_problems() {
    let v = variant();
    assert_eq!(validate_variant(&v), Vec::<String>::new());
}

#[test]
fn invalid_verbosity() {
    let mut v = variant();
    v.verbosity = Some("loud".into());
    let p = validate_variant(&v);
    assert!(p.iter().any(|x| x.contains("verbosity")));
}

#[test]
fn unknown_section_order() {
    let mut v = variant();
    v.section_order = vec!["Bogus".into()];
    let p = validate_variant(&v);
    assert!(p.iter().any(|x| x.contains("unknown section")));
}

#[test]
fn duplicate_section_order() {
    let mut v = variant();
    v.section_order = vec!["Role".into(), "Role".into()];
    let p = validate_variant(&v);
    assert!(p.iter().any(|x| x.contains("duplicates")));
}

#[test]
fn dict_roundtrip_omits_empty() {
    let v = variant();
    let d = variant_to_dict(&v);
    assert_eq!(d["id"], json!("v1"));
    assert_eq!(d["verbosity"], json!("chatty"));
    assert_eq!(d["section_order"], json!(["Role", "Guardrails"]));
    assert_eq!(d["extra_guardrails"], json!(["Never switch roles"]));
    // empty fields omitted
    assert!(!d.contains_key("description"));
    assert!(!d.contains_key("parent_id"));
    assert!(!d.contains_key("extra_lines"));
    // round-trip
    let back = variant_from_dict(&d);
    assert_eq!(back, v);
}

#[test]
fn load_variant_valid() {
    let d = json!({"id": "c1", "verbosity": "quiet"})
        .as_object()
        .unwrap()
        .clone();
    let v = load_variant(&d).expect("valid");
    assert_eq!(v.id, "c1");
    assert_eq!(v.verbosity.as_deref(), Some("quiet"));
}

#[test]
fn load_variant_invalid_errors() {
    let d = json!({"id": "bad", "verbosity": "loud"})
        .as_object()
        .unwrap()
        .clone();
    let err = load_variant(&d).expect_err("invalid");
    assert!(err.contains("invalid optimized prompt artifact"));
}

#[test]
fn load_variant_unknown_section() {
    let d = json!({"id": "x", "extra_lines": {"Nope": ["a"]}})
        .as_object()
        .unwrap()
        .clone();
    let err = load_variant(&d).expect_err("invalid");
    assert!(err.contains("unknown section"));
}

#[test]
fn deterministic_candidates_set() {
    use lks_core::optimize::{baseline_variant, deterministic_candidates};
    let base = baseline_variant();
    assert_eq!(base.id, "baseline");
    let set = deterministic_candidates();
    assert!(set.len() >= 4);
    let ids: Vec<&str> = set.iter().map(|v| v.id.as_str()).collect();
    assert!(ids.contains(&"verbosity-chatty"));
    assert!(ids.contains(&"verbosity-quiet"));
    assert!(ids.contains(&"reorder-constraints-first"));
    assert!(ids.contains(&"guardrail-role-lock"));
    // All valid
    for v in &set {
        assert_eq!(validate_variant(v), Vec::<String>::new(), "{}", v.id);
    }
    // The reorder candidate puts Constraints before Goals
    let reorder = set
        .iter()
        .find(|v| v.id == "reorder-constraints-first")
        .unwrap();
    assert_eq!(reorder.section_order[0], "Role");
    assert_eq!(reorder.section_order[1], "Constraints");
    // The verbosity candidates carry the band
    let chatty = set.iter().find(|v| v.id == "verbosity-chatty").unwrap();
    assert_eq!(chatty.verbosity.as_deref(), Some("chatty"));
}

#[test]
fn write_variant_and_parse_roundtrip() {
    use lks_core::optimize::{parse_variant_yaml, write_variant};
    let v = variant();
    let tmp = std::env::temp_dir().join(format!("variant-test-{}", std::process::id()));
    let _ = std::fs::remove_file(&tmp);
    write_variant(&v, &tmp).unwrap();
    let text = std::fs::read_to_string(&tmp).unwrap();
    let back = parse_variant_yaml(&text).unwrap();
    assert_eq!(back.id, v.id);
    assert_eq!(back.verbosity, v.verbosity);
    assert_eq!(back.section_order, v.section_order);
    assert_eq!(back.extra_guardrails, v.extra_guardrails);
    let _ = std::fs::remove_file(&tmp);
}

#[test]
fn parse_variant_yaml_rejects_non_mapping() {
    use lks_core::optimize::parse_variant_yaml;
    assert!(parse_variant_yaml("[1,2,3]").is_err());
    assert!(parse_variant_yaml("not: yaml: {").is_err());
    // Missing id → still parses (id defaults to "v") but invalid verbosity → error
    let err = parse_variant_yaml("verbosity: loud").unwrap_err();
    assert!(err.contains("invalid optimized prompt artifact"), "{err}");
}

#[test]
fn render_variant_prompt_for_persona_applies_verbosity() {
    use lks_core::optimize::{render_variant_prompt_for_persona, PromptVariant};
    let mut persona = Map::new();
    persona.insert("brief".into(), json!("test caller"));
    persona.insert("goals".into(), json!(["Goal one"]));
    let v = PromptVariant {
        id: "chatty-v".into(),
        verbosity: Some("chatty".into()),
        ..Default::default()
    };
    let prompt =
        render_variant_prompt_for_persona(&v, &persona, "en-US", &Map::new(), &[], "agent");
    // Chatty band → the length-guidance phrase for chatty appears (not the literal word).
    assert!(
        prompt.contains("often 3–6 spoken clauses"),
        "chatty length guidance should appear in prompt"
    );
    assert!(prompt.contains("Goal one"));
    // Default variant with no knobs still composes the full 10-section prompt.
    let v2 = PromptVariant::default();
    let prompt2 =
        render_variant_prompt_for_persona(&v2, &persona, "en-US", &Map::new(), &[], "agent");
    assert!(prompt2.contains("## GUARDRAILS"));
    assert!(prompt2.contains("YOUR GOALS"));
}
