//! Tests for behavior_compile (Hamming-style speech_conditions / Behavior → ScriptStep).
use lks_core::behavior_compile::{
    apply_caller_behavior, compile_from_behavior_spec, compile_from_speech_conditions,
    is_voice_asset, silent_mode_enabled,
};
use serde_json::json;

#[test]
fn is_voice_asset_cases() {
    assert!(is_voice_asset(Some("builtin:voice.backchannel")));
    assert!(is_voice_asset(Some("@voice.persona")));
    assert!(!is_voice_asset(Some("builtin:noise.loud")));
    assert!(!is_voice_asset(Some("my_noise.wav")));
    assert!(!is_voice_asset(None));
}

#[test]
fn silent_mode_enabled_cases() {
    assert!(silent_mode_enabled(
        &json!({"speech_conditions": {"silent_mode": true}})
            .as_object()
            .unwrap()
    ));
    assert!(silent_mode_enabled(
        &json!({"speech_conditions": {"silentMode": "on"}})
            .as_object()
            .unwrap()
    ));
    assert!(!silent_mode_enabled(
        &json!({"speech_conditions": {"silent_mode": false}})
            .as_object()
            .unwrap()
    ));
    assert!(!silent_mode_enabled(&json!({}).as_object().unwrap()));
}

#[test]
fn compile_from_speech_conditions_ambient() {
    let persona = json!({
        "speech_conditions": {
            "noise": "builtin:noise.ambient",
            "noise_gain": 0.5,
            "noise_when": "loop"
        }
    });
    let steps = compile_from_speech_conditions(persona.as_object().unwrap()).expect("compile");
    assert_eq!(steps.len(), 1);
    let s = &steps[0];
    assert_eq!(s.id, "auto-ambient");
    assert_eq!(s.trigger, "time");
    assert_eq!(s.delay_ms, 5000);
    assert_eq!(s.say, "[ambient]");
    assert_eq!(s.delivery, "room_pcm");
    assert_eq!(s.asset.as_deref(), Some("builtin:noise.ambient"));
    assert!(s.r#loop, "noise_when: loop → loop");
    assert_eq!(s.interrupt_class.as_deref(), Some("noise"));
    assert_eq!(s.gain, 0.5);
}

#[test]
fn compile_from_speech_conditions_barge() {
    let persona = json!({
        "speech_conditions": {
            "barge_policy": "mid_agent_turn",
            "barge_after_agent_ms": 600
        }
    });
    let steps = compile_from_speech_conditions(persona.as_object().unwrap()).expect("compile");
    assert_eq!(steps.len(), 1);
    let s = &steps[0];
    assert_eq!(s.id, "auto-barge-1");
    assert_eq!(s.trigger, "agent_speaking");
    assert_eq!(s.delay_ms, 300, "600//2");
    assert_eq!(s.min_agent_active_ms, 300);
    assert!(s.barge_in);
    assert!(s.with_blip, "gemini_text barge → blip");
    assert_eq!(s.delivery, "gemini_text");
    assert_eq!(s.say, "Sorry — one second —");
}

#[test]
fn compile_from_speech_conditions_silence() {
    let persona = json!({
        "speech_conditions": {"silence_ms": 2000}
    });
    let steps = compile_from_speech_conditions(persona.as_object().unwrap()).expect("compile");
    assert_eq!(steps.len(), 1);
    let s = &steps[0];
    assert_eq!(s.id, "auto-user-silence");
    assert_eq!(s.action, "wait");
    assert_eq!(s.silence_after_cue_ms, 2000);
    assert_eq!(s.trigger, "time");
}

#[test]
fn compile_from_speech_conditions_gain_out_of_range() {
    let persona = json!({
        "speech_conditions": {"noise": "x", "noise_gain": 1.5}
    });
    let err = compile_from_speech_conditions(persona.as_object().unwrap()).expect_err("bad gain");
    assert!(
        err.contains("noise_gain must be between 0.0 and 1.0"),
        "got: {err}"
    );
}

#[test]
fn compile_from_behavior_spec_barges() {
    let spec = json!({
        "barge_ins": [
            {"id": "b1", "say": "Wait", "after_agent_ms": 800}
        ]
    });
    let steps = compile_from_behavior_spec(spec.as_object().unwrap(), "Behavior").expect("compile");
    assert_eq!(steps.len(), 1);
    let s = &steps[0];
    assert_eq!(s.id, "b1");
    assert_eq!(s.delay_ms, 400, "after//2");
    assert_eq!(s.say, "Wait");
    assert!(s.barge_in);
    assert_eq!(s.interrupt_class.as_deref(), Some("correction"));
}

#[test]
fn apply_caller_behavior_merges_and_verify() {
    let persona = json!({
        "speech_conditions": {"barge_policy": "mid_agent_turn", "barge_after_agent_ms": 600}
    });
    let explicit: Vec<lks_core::script::ScriptStep> = Vec::new();
    let (steps, verify) =
        apply_caller_behavior(persona.as_object().unwrap(), None, &explicit, None, "test")
            .expect("apply");
    // barge compiled → auto-barge-1
    assert_eq!(steps.len(), 1);
    assert_eq!(steps[0].id, "auto-barge-1");
    // default_verify_for_compiled → has_barge → min_agent_finals_after_barge_in=1
    let v = verify.expect("verify auto-added");
    assert_eq!(v.min_agent_finals_after_barge_in, 1);
    assert!(!v.require_during_agent_speech);
}

#[test]
fn apply_caller_behavior_silent_drops_speak() {
    let persona = json!({
        "speech_conditions": {"silent_mode": true, "silence_ms": 2000}
    });
    // explicit speak step should be dropped in silent mode
    let explicit: Vec<lks_core::script::ScriptStep> = lks_core::script::parse::parse_script_steps(
        &json!({"steps": [{"id": "say-hi", "say": "Hi"}]})
            .as_object()
            .unwrap(),
        "test",
    )
    .expect("parse");
    let (steps, _) =
        apply_caller_behavior(persona.as_object().unwrap(), None, &explicit, None, "test")
            .expect("apply");
    // speak step dropped; compile_from_speech_conditions returns [] in silent mode,
    // so no auto steps either (matches Python: silent_mode → early return []).
    assert_eq!(steps.len(), 0, "silent mode drops speak + no auto compile");
}
