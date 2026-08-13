//! Tests for persona_traits (trait library + expand_traits).
use lks_core::persona_traits::{expand_traits, list_trait_ids, trait_library};
use serde_json::json;

#[test]
fn trait_library_has_all_traits() {
    let lib = trait_library();
    assert_eq!(lib.len(), 18);
    for t in [
        "polite",
        "impatient",
        "interrupts",
        "quiet",
        "terse",
        "silent",
        "confused",
        "elderly",
        "angry",
        "fast_speaker",
        "soft_spoken",
        "non_native",
        "skeptical",
        "chatty",
        "backchannel",
        "hangup_threat",
        "code_switch",
        "urgent",
    ] {
        assert!(lib.contains_key(t), "missing trait: {t}");
    }
}

#[test]
fn list_trait_ids_sorted() {
    let ids = list_trait_ids();
    let mut sorted = ids.clone();
    sorted.sort();
    assert_eq!(ids, sorted);
    assert_eq!(ids.len(), 18); // 17 traits + urgent = 18 (count actual)
}

#[test]
fn expand_traits_known_and_unknown() {
    let lines = expand_traits(&json!(["polite", "impatient"]));
    assert_eq!(lines.len(), 3); // 1 polite + 2 impatient
    assert!(lines[0].contains("courteous"));
    assert!(lines[1].contains("short on time"));
}

#[test]
fn expand_traits_unknown_passthrough() {
    let lines = expand_traits(&json!(["mystery_behavior"]));
    assert_eq!(lines.len(), 1);
    assert_eq!(
        lines[0],
        "Additional caller behavior (follow naturally): mystery_behavior."
    );
}

#[test]
fn expand_traits_dedupe_first_wins() {
    let lines = expand_traits(&json!(["polite", "POLITE", "polite"]));
    assert_eq!(lines.len(), 1, "dedupe first-occurrence");
}

#[test]
fn expand_traits_single_string() {
    let lines = expand_traits(&json!("quiet"));
    assert_eq!(lines.len(), 1);
    assert!(lines[0].contains("reserved"));
}

#[test]
fn expand_traits_empty() {
    assert_eq!(expand_traits(&json!([])), Vec::<String>::new());
    assert_eq!(expand_traits(&json!("")), Vec::<String>::new());
}

#[test]
fn expand_traits_key_normalization() {
    // hyphen/space → underscore
    let lines = expand_traits(&json!(["fast speaker"]));
    assert_eq!(lines.len(), 1, "fast_speaker matched via normalization");
}
