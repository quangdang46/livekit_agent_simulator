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
