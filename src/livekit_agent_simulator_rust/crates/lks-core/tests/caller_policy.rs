//! Tests for caller_policy (CallerPolicyContext + verbosity + length guidance).
use lks_core::caller_policy::{
    between_cues_answer_guidance, length_guidance, neutralize_style_length_hints,
    CallerPolicyContext, Verbosity,
};
use serde_json::json;

fn ctx(persona: serde_json::Value) -> CallerPolicyContext {
    CallerPolicyContext {
        persona: persona.as_object().unwrap().clone(),
        locale: "en-US".into(),
        context: serde_json::Map::new(),
        script_steps: vec![],
        first_speaker: "agent".into(),
    }
}

#[test]
fn verbosity_explicit_wins() {
    let c = ctx(json!({"speech_conditions": {"verbosity": "chatty"}}));
    assert_eq!(c.resolved_verbosity(), Verbosity::Chatty);
    let c = ctx(json!({"speech_conditions": {"verbosity": "quiet"}}));
    assert_eq!(c.resolved_verbosity(), Verbosity::Quiet);
}

#[test]
fn verbosity_case_insensitive() {
    let c = ctx(json!({"speech_conditions": {"verbosity": "CHATTY"}}));
    assert_eq!(c.resolved_verbosity(), Verbosity::Chatty);
}

#[test]
fn verbosity_traits_chatty_beats_quiet() {
    let c = ctx(json!({"traits": ["quiet", "chatty"]}));
    assert_eq!(
        c.resolved_verbosity(),
        Verbosity::Chatty,
        "chatty checked first"
    );
}

#[test]
fn verbosity_quiet_trait() {
    let c = ctx(json!({"traits": ["terse"]}));
    assert_eq!(c.resolved_verbosity(), Verbosity::Quiet);
}

#[test]
fn verbosity_default_natural() {
    let c = ctx(json!({"goals": ["a"]}));
    assert_eq!(c.resolved_verbosity(), Verbosity::Natural);
}

#[test]
fn verbosity_unknown_falls_back_natural() {
    let c = ctx(json!({"speech_conditions": {"verbosity": "loud"}}));
    assert_eq!(c.resolved_verbosity(), Verbosity::Natural);
}

#[test]
fn goals_and_constraints() {
    let c = ctx(json!({
        "goals": ["G1", "G2"],
        "constraints": ["C1"]
    }));
    assert_eq!(c.goals(), vec!["G1", "G2"]);
    assert_eq!(c.constraints(), vec!["C1"]);
}

#[test]
fn length_guidance_bands() {
    assert!(length_guidance(Verbosity::Quiet).contains("one short spoken clause"));
    assert!(length_guidance(Verbosity::Chatty).contains("3–6 spoken clauses"));
    assert!(length_guidance(Verbosity::Natural).contains("Never a"));
}

#[test]
fn between_cues_guidance_bands() {
    assert!(between_cues_answer_guidance(Verbosity::Quiet).contains("one short spoken clause"));
    assert!(between_cues_answer_guidance(Verbosity::Chatty).contains("talkative"));
    assert!(between_cues_answer_guidance(Verbosity::Natural).contains("2–5 natural"));
}

#[test]
fn neutralize_style_length_hints_strips() {
    let (cleaned, stripped) =
        neutralize_style_length_hints("polite, brief answers, warm", Verbosity::Natural);
    assert!(stripped);
    assert!(!cleaned.contains("brief answers"), "got: {cleaned}");
    assert!(cleaned.contains("polite"), "got: {cleaned}");
    assert!(cleaned.contains("warm"), "got: {cleaned}");
}

#[test]
fn neutralize_quiet_keeps_verbatim() {
    let (cleaned, stripped) = neutralize_style_length_hints("brief answers", Verbosity::Quiet);
    assert!(!stripped);
    assert_eq!(cleaned, "brief answers");
}

#[test]
fn neutralize_tidies_separators() {
    let (cleaned, _) = neutralize_style_length_hints("polite; ; everyday", Verbosity::Natural);
    assert_eq!(cleaned, "polite, everyday");
}

#[test]
fn default_policy_builds_system_instruction() {
    use lks_core::caller_policy::DefaultCallerPolicy;
    let c = ctx(json!({
        "name": "Alex",
        "brief": "Called about a bill",
        "goals": ["Pay the bill"]
    }));
    let p = DefaultCallerPolicy::new();
    let si = p.build_system_instruction(&c);
    assert!(si.contains("## PERSONA"));
    assert!(si.contains("## GUARDRAILS"));
    assert!(si.contains("GOAL 1: Pay the bill"));
    // sections joined with \n, no trailing newline
    assert!(!si.ends_with('\n'));
}

#[test]
fn default_policy_midcall_bootstrap_user_first() {
    use lks_core::caller_policy::DefaultCallerPolicy;
    let c = CallerPolicyContext {
        persona: serde_json::Map::new(),
        locale: "en-US".into(),
        context: serde_json::Map::new(),
        script_steps: vec![],
        first_speaker: "user".into(),
    };
    let p = DefaultCallerPolicy::new();
    let cues = p.midcall_cues(&c);
    assert!(
        cues.iter().any(|cue| cue.kind == "bootstrap"),
        "got: {cues:?}"
    );
}

#[test]
fn default_policy_no_bootstrap_when_script_owns_opening() {
    use lks_core::caller_policy::DefaultCallerPolicy;
    // script with a time+speak step owns the opening → no bootstrap
    let c = CallerPolicyContext {
        persona: serde_json::Map::new(),
        locale: "en-US".into(),
        context: serde_json::Map::new(),
        script_steps: vec![serde_json::json!({"trigger": "time", "action": "speak", "say": "Hi"})],
        first_speaker: "user".into(),
    };
    let p = DefaultCallerPolicy::new();
    let cues = p.midcall_cues(&c);
    assert!(
        !cues.iter().any(|cue| cue.kind == "bootstrap"),
        "got: {cues:?}"
    );
}

#[test]
fn default_policy_midcall_reground_goals() {
    use lks_core::caller_policy::DefaultCallerPolicy;
    let c = ctx(json!({"goals": ["Pay the bill", "Get receipt"]}));
    let p = DefaultCallerPolicy::new();
    let cues = p.midcall_cues(&c);
    let reground = cues.iter().find(|cue| cue.label == "goal_reground");
    assert!(reground.is_some(), "got: {cues:?}");
    assert!(reground.unwrap().text.contains("GOAL 1 — Pay the bill"));
}

#[test]
fn default_policy_midcall_script_no_early_bye() {
    use lks_core::caller_policy::DefaultCallerPolicy;
    let mut c = ctx(json!({"goals": ["G1"]}));
    c.script_steps = vec![serde_json::json!({"id": "a", "say": "Hi"})];
    let p = DefaultCallerPolicy::new();
    let cues = p.midcall_cues(&c);
    assert!(
        cues.iter().any(|cue| cue.label == "script_no_early_bye"),
        "got: {cues:?}"
    );
}
