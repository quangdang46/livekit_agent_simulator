//! Tests for prompt sections (Role/Goals/StyleTraits/Constraints/SpeechConditions/
//! FirstSpeaker/Context/ScriptTiming).
use lks_core::caller_policy::CallerPolicyContext;
use lks_core::prompt_sections::{
    constraints_section, context_section, first_speaker_section, goals_section, role_section,
    script_timing_section, speech_conditions_section, style_traits_section,
};
use serde_json::json;

fn ctx(persona: serde_json::Value, script_steps: Vec<serde_json::Value>) -> CallerPolicyContext {
    CallerPolicyContext {
        persona: persona.as_object().unwrap().clone(),
        locale: "en-US".into(),
        context: serde_json::Map::new(),
        script_steps,
        first_speaker: "agent".into(),
    }
}

#[test]
fn role_section_includes_persona_and_language() {
    let c = ctx(
        json!({
            "name": "Alex",
            "brief": "You are calling about a bill.",
            "goals": ["Pay the bill"]
        }),
        vec![],
    );
    let lines = role_section(&c);
    assert!(lines.iter().any(|l| l.contains("## PERSONA")));
    assert!(lines.iter().any(|l| l.contains("RESPOND IN en-US")));
    assert!(lines.iter().any(|l| l.contains("Your name: Alex")));
    assert!(lines
        .iter()
        .any(|l| l.contains("Who you are and why you are calling")));
}

#[test]
fn role_section_situation_label() {
    let c = ctx(
        json!({"situation": "Called about the car", "brief": "different brief"}),
        vec![],
    );
    let lines = role_section(&c);
    assert!(lines
        .iter()
        .any(|l| l.contains("Your situation: Called about the car")));
    assert!(lines
        .iter()
        .any(|l| l.contains("Additional brief: different brief")));
}

#[test]
fn goals_section_empty_returns_empty() {
    let c = ctx(json!({}), vec![]);
    assert_eq!(goals_section(&c), Vec::<String>::new());
}

#[test]
fn goals_section_lists_goals() {
    let c = ctx(json!({"goals": ["G1", "G2"]}), vec![]);
    let lines = goals_section(&c);
    assert!(lines.iter().any(|l| l.contains("GOAL 1: G1")));
    assert!(lines.iter().any(|l| l.contains("GOAL 2: G2")));
}

#[test]
fn goals_section_script_overlay_rules() {
    let c = ctx(
        json!({"goals": ["G1"]}),
        vec![json!({"id": "a", "say": "Hi"})],
    );
    let lines = goals_section(&c);
    assert!(lines.iter().any(|l| l.contains("hybrid / interaction")));
}

#[test]
fn style_traits_section_traits_and_style() {
    let c = ctx(
        json!({"style": "polite, brief", "traits": ["polite"]}),
        vec![],
    );
    let lines = style_traits_section(&c);
    assert!(lines.iter().any(|l| l.contains("Speaking style:")));
    assert!(lines.iter().any(|l| l.contains("Caller behavior traits")));
}

#[test]
fn constraints_section_empty_returns_empty() {
    let c = ctx(json!({}), vec![]);
    assert_eq!(constraints_section(&c), Vec::<String>::new());
}

#[test]
fn constraints_section_lists() {
    let c = ctx(json!({"constraints": ["No card numbers"]}), vec![]);
    let lines = constraints_section(&c);
    assert!(lines.iter().any(|l| l.contains("## HARD CONSTRAINTS")));
    assert!(lines.iter().any(|l| l.contains("- No card numbers")));
}

#[test]
fn speech_conditions_silent_mode() {
    let c = ctx(json!({"speech_conditions": {"silent_mode": true}}), vec![]);
    let lines = speech_conditions_section(&c);
    assert_eq!(lines.len(), 1);
    assert!(lines[0].contains("SILENT MODE"), "got: {lines:?}");
}

#[test]
fn speech_conditions_barge_policy() {
    let c = ctx(
        json!({"speech_conditions": {"barge_policy": "mid_agent_turn"}}),
        vec![],
    );
    let lines = speech_conditions_section(&c);
    assert!(lines[0].contains("barge_policy=mid_agent_turn"));
}

#[test]
fn speech_conditions_empty_returns_empty() {
    let c = ctx(json!({}), vec![]);
    assert_eq!(speech_conditions_section(&c), Vec::<String>::new());
}

#[test]
fn first_speaker_agent_wait() {
    let c = ctx(json!({}), vec![]);
    let lines = first_speaker_section(&c);
    assert!(lines[0].contains("Wait for the assistant to greet you first"));
}

#[test]
fn first_speaker_user() {
    let c = CallerPolicyContext {
        persona: serde_json::Map::new(),
        locale: "en-US".into(),
        context: serde_json::Map::new(),
        script_steps: vec![],
        first_speaker: "user".into(),
    };
    let lines = first_speaker_section(&c);
    assert!(lines[0].contains("You speak first"), "got: {lines:?}");
}

#[test]
fn first_speaker_silent() {
    let c = ctx(json!({"speech_conditions": {"silent_mode": true}}), vec![]);
    let lines = first_speaker_section(&c);
    assert!(lines[0].contains("stay mute"));
}

#[test]
fn context_section_ignores_notes() {
    let c = CallerPolicyContext {
        persona: serde_json::Map::new(),
        locale: "en-US".into(),
        context: json!({"notes": "author note"}).as_object().unwrap().clone(),
        script_steps: vec![],
        first_speaker: "agent".into(),
    };
    // author notes are never injected
    assert_eq!(context_section(&c), Vec::<String>::new());
}

#[test]
fn context_section_caller_knows() {
    let c = CallerPolicyContext {
        persona: serde_json::Map::new(),
        locale: "en-US".into(),
        context: json!({"caller_knows": "You know the order number 123"})
            .as_object()
            .unwrap()
            .clone(),
        script_steps: vec![],
        first_speaker: "agent".into(),
    };
    let lines = context_section(&c);
    assert_eq!(lines.len(), 1);
    assert!(lines[0].contains("order number 123"));
}

#[test]
fn script_timing_section_empty() {
    let c = ctx(json!({"goals": ["G1"]}), vec![]);
    assert_eq!(script_timing_section(&c), Vec::<String>::new());
}

#[test]
fn script_timing_section_counts() {
    let c = ctx(
        json!({"goals": ["G1"]}),
        vec![
            json!({"id": "a", "say": "Hi"}),
            json!({"id": "b", "delivery": "room_pcm", "asset": "x.wav"}),
        ],
    );
    let lines = script_timing_section(&c);
    assert!(
        lines.iter().any(|l| l.contains("2 timed Script step(s)")),
        "got: {lines:?}"
    );
    assert!(
        lines
            .iter()
            .any(|l| l.contains("1 forced line(s), 1 audio fixture(s)")),
        "got: {lines:?}"
    );
}
