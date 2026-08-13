//! Golden tests for script step/verify parsing (P1) — vs script_parse.py.
use lks_core::script::parse::{parse_script_steps, parse_script_verify};
use lks_core::script::{counts_for_recovery_barge, effective_overlay, normalize_interrupt_class};
use serde_json::json;

#[test]
fn parse_simple_speak_step() {
    let spec = json!({
        "steps": [{
            "id": "greet",
            "trigger": "agent_speaking",
            "delay_ms": 500,
            "say": "Hello!",
            "action": "speak"
        }]
    });
    let steps = parse_script_steps(spec.as_object().unwrap(), "test").expect("parse");
    assert_eq!(steps.len(), 1);
    let s = &steps[0];
    assert_eq!(s.id, "greet");
    assert_eq!(s.trigger, "agent_speaking");
    assert_eq!(s.delay_ms, 500);
    assert_eq!(s.say, "Hello!");
    assert_eq!(s.action, "speak");
    assert_eq!(s.delivery, "gemini_text");
    assert!(s.once);
    assert_eq!(s.min_agent_active_ms, 400);
    assert_eq!(s.gain, 1.0);
    assert_eq!(s.interrupt_class, None);
}

#[test]
fn parse_barge_in_forces_defaults() {
    let spec = json!({
        "steps": [{
            "id": "barge",
            "say": "Actually...",
            "barge_in": true,
            "interrupt_class": "correction"
        }]
    });
    let steps = parse_script_steps(spec.as_object().unwrap(), "test").expect("parse");
    let s = &steps[0];
    // barge_in forces trigger=agent_speaking, action=speak, shorter defaults
    assert_eq!(s.trigger, "agent_speaking");
    assert_eq!(s.action, "speak");
    assert_eq!(s.delay_ms, 250);
    assert_eq!(s.min_agent_active_ms, 200);
    assert!(s.barge_in);
    assert_eq!(s.interrupt_class.as_deref(), Some("correction"));
    assert!(s.with_blip, "barge + gemini_text → blip on");
}

#[test]
fn parse_dtmf_step() {
    let spec = json!({
        "steps": [{
            "id": "dial",
            "action": "dtmf",
            "digits": "123#"
        }]
    });
    let steps = parse_script_steps(spec.as_object().unwrap(), "test").expect("parse");
    let s = &steps[0];
    assert_eq!(s.action, "dtmf");
    assert_eq!(s.digits, "123#");
    assert_eq!(s.say, "[DTMF: 123#]");
    assert_eq!(s.trigger, "time", "dtmf without silence/time → time");
}

#[test]
fn parse_dtmf_invalid_digit() {
    let spec = json!({
        "steps": [{
            "id": "bad",
            "action": "dtmf",
            "digits": "12x"
        }]
    });
    let err =
        parse_script_steps(spec.as_object().unwrap(), "test").expect_err("invalid digit fails");
    assert!(err.contains("digits can only contain 0-9*#w"), "got: {err}");
}

#[test]
fn parse_room_pcm_requires_asset() {
    let spec = json!({
        "steps": [{
            "id": "noise",
            "delivery": "room_pcm",
            "say": "x"
        }]
    });
    let err = parse_script_steps(spec.as_object().unwrap(), "test").expect_err("no asset fails");
    assert!(
        err.contains("room_pcm delivery requires asset"),
        "got: {err}"
    );
}

#[test]
fn parse_loop_requires_room_pcm() {
    let spec = json!({
        "steps": [{
            "id": "amb",
            "loop": true,
            "say": "x"
        }]
    });
    let err =
        parse_script_steps(spec.as_object().unwrap(), "test").expect_err("loop needs room_pcm");
    assert!(
        err.contains("loop requires delivery=room_pcm"),
        "got: {err}"
    );
}

#[test]
fn parse_say_required_for_speak() {
    let spec = json!({
        "steps": [{"id": "no-say", "action": "speak"}]
    });
    let err = parse_script_steps(spec.as_object().unwrap(), "test").expect_err("no say fails");
    assert!(
        err.contains("say/text required when action=speak"),
        "got: {err}"
    );
}

#[test]
fn parse_unsupported_trigger() {
    let spec = json!({
        "steps": [{"id": "bad", "trigger": "never", "say": "x"}]
    });
    let err = parse_script_steps(spec.as_object().unwrap(), "test").expect_err("bad trigger fails");
    assert!(err.contains("unsupported trigger"), "got: {err}");
}

#[test]
fn normalize_interrupt_class_aliases() {
    // barge/correct → correction; ack → backchannel; noise; dtmf; escalate
    assert_eq!(
        normalize_interrupt_class(Some(&json!("barge")), false, "correction").unwrap(),
        Some("correction".into())
    );
    assert_eq!(
        normalize_interrupt_class(Some(&json!("ack")), false, "correction").unwrap(),
        Some("backchannel".into())
    );
    assert_eq!(
        normalize_interrupt_class(Some(&json!("false_positive")), false, "correction").unwrap(),
        Some("noise".into())
    );
    assert_eq!(
        normalize_interrupt_class(Some(&json!("handoff")), false, "correction").unwrap(),
        Some("escalate".into())
    );
    // barge_in with no class → correction
    assert_eq!(
        normalize_interrupt_class(None, true, "correction").unwrap(),
        Some("correction".into())
    );
    // no barge, no class → None
    assert_eq!(
        normalize_interrupt_class(None, false, "correction").unwrap(),
        None
    );
}

#[test]
fn counts_for_recovery_barge_cases() {
    assert!(counts_for_recovery_barge(true, Some("correction")));
    assert!(counts_for_recovery_barge(true, Some("escalate")));
    assert!(!counts_for_recovery_barge(true, Some("noise")));
    assert!(!counts_for_recovery_barge(false, Some("correction")));
    // barge with None class → Python defaults to "correction" → counts
    assert!(
        counts_for_recovery_barge(true, None),
        "None class → correction → counts"
    );
}

#[test]
fn effective_overlay_cases() {
    let base = |id: &str| lks_core::script::ScriptStep {
        id: id.into(),
        trigger: "agent_speaking".into(),
        delay_ms: 400,
        say: "hi".into(),
        label: id.into(),
        once: true,
        min_agent_active_ms: 400,
        delivery: "gemini_text".into(),
        asset: None,
        silence_after_cue_ms: 0,
        action: "speak".into(),
        mute_persona: None,
        digits: "".into(),
        r#loop: false,
        require_agent_spoke_first: true,
        require_agent_reply_this_turn: true,
        defer_on_open_question: true,
        open_question_idle_ms: 20_000,
        barge_in: false,
        with_blip: false,
        gain: 1.0,
        interrupt_class: None,
        overlay: None,
    };
    // explicit overlay wins
    let mut s = base("a");
    s.overlay = Some("line".into());
    assert_eq!(effective_overlay(&s), "line");
    // barge_in → fixture
    let mut s = base("b");
    s.barge_in = true;
    assert_eq!(effective_overlay(&s), "fixture");
    // room_pcm → fixture
    let mut s = base("c");
    s.delivery = "room_pcm".into();
    assert_eq!(effective_overlay(&s), "fixture");
    // speak with say → line
    assert_eq!(effective_overlay(&base("d")), "line");
}

#[test]
fn parse_script_verify_spec() {
    let v = json!({
        "require_during_agent_speech": false,
        "min_agent_finals_after_first_cue": 1,
        "min_agent_finals_after_barge_in": 2,
        "plugins": ["a", "b"]
    });
    let vs = parse_script_verify(&v).expect("verify").expect("some");
    assert!(!vs.require_during_agent_speech);
    assert_eq!(vs.min_agent_finals_after_first_cue, 1);
    assert_eq!(vs.min_agent_finals_after_barge_in, 2);
    assert_eq!(vs.plugins, vec!["a", "b"]);
    // None when absent
    assert!(parse_script_verify(&json!(null)).unwrap().is_none());
}
