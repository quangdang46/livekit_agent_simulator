//! Tests for log-based script verify (script/verify.rs).
use lks_core::script::parse::parse_script_steps;
use lks_core::script::verify::evaluate_script_log;
use serde_json::{json, Map, Value as Json};

fn ev(kind: &str, ts: i64, spec: serde_json::Value) -> Map<String, Json> {
    let mut m = Map::new();
    m.insert("kind".into(), json!(kind));
    m.insert("ts_mono_ms".into(), json!(ts));
    m.insert("spec".into(), spec);
    m
}

fn cue(step_id: &str, ts: i64, during: bool, waited: i64) -> Map<String, Json> {
    ev(
        "sim.script.cue",
        ts,
        json!({
            "step_id": step_id,
            "during_agent_speech": during,
            "trigger": "agent_speaking",
            "action": "speak",
            "waited_ms": waited,
            "barge_in": false,
        }),
    )
}

#[test]
fn verify_empty_script_fails() {
    // Empty checks → pass:false (Python: `all(...) if checks else False`).
    let events = vec![ev("run.started", 0, json!({}))];
    let steps = vec![];
    let res = evaluate_script_log(&events, &steps, None);
    assert_eq!(res["pass"], json!(false), "empty checks → fail");
    assert_eq!(res["script_steps"], json!(0));
}

#[test]
fn verify_step_not_fired() {
    let spec = json!({"steps": [{"id": "greet", "say": "Hi"}]});
    let steps = parse_script_steps(spec.as_object().unwrap(), "test").unwrap();
    let events = vec![ev("run.started", 0, json!({}))];
    let res = evaluate_script_log(&events, &steps, None);
    assert_eq!(res["pass"], json!(false));
    assert_eq!(res["cues_fired"], json!(0));
    let checks = res["checks"].as_array().unwrap();
    assert_eq!(checks[0]["reason"], json!("script step not fired"));
}

#[test]
fn verify_step_passes_when_cue_fires_during_agent() {
    let spec = json!({"steps": [{"id": "greet", "say": "Hi"}]});
    let steps = parse_script_steps(spec.as_object().unwrap(), "test").unwrap();
    let events = vec![
        ev("run.started", 0, json!({})),
        cue("greet", 100, true, 400),
        ev("transcript.agent.final", 500, json!({"text": "hi"})),
    ];
    let res = evaluate_script_log(&events, &steps, None);
    assert_eq!(res["pass"], json!(true));
    assert_eq!(res["cues_fired"], json!(1));
    assert_eq!(res["agent_finals_after_first_cue"], json!(1));
}

#[test]
fn verify_agent_not_speaking_fails() {
    // trigger=agent_speaking + action=speak + not during_agent_speech → fail.
    let spec = json!({"steps": [{"id": "greet", "say": "Hi"}]});
    let steps = parse_script_steps(spec.as_object().unwrap(), "test").unwrap();
    let events = vec![
        ev("run.started", 0, json!({})),
        cue("greet", 100, false, 400),
    ];
    let res = evaluate_script_log(&events, &steps, None);
    assert_eq!(res["pass"], json!(false));
    let checks = res["checks"].as_array().unwrap();
    assert_eq!(
        checks[0]["reason"],
        json!("cue fired but agent was not active speaker")
    );
}

#[test]
fn verify_min_finals_checks() {
    let spec = json!({
        "steps": [{"id": "greet", "say": "Hi"}],
        "verify": {"min_agent_finals_after_first_cue": 1}
    });
    let steps = parse_script_steps(spec.as_object().unwrap(), "test").unwrap();
    let verify = lks_core::script::parse::parse_script_verify(&spec["verify"])
        .unwrap()
        .unwrap();
    // cue fires, but no agent final after it → min check fails
    let events = vec![
        ev("run.started", 0, json!({})),
        cue("greet", 100, true, 400),
    ];
    let res = evaluate_script_log(&events, &steps, Some(&verify));
    assert_eq!(res["pass"], json!(false));
    let checks = res["checks"].as_array().unwrap();
    assert!(
        checks
            .iter()
            .any(|c| c["check"] == json!("min_agent_finals_after_first_cue")
                && c["pass"] == json!(false)),
        "min check present + failing"
    );
}

#[test]
fn verify_min_interruptions() {
    let spec = json!({"steps": [{"id": "g", "say": "Hi"}]});
    let steps = parse_script_steps(spec.as_object().unwrap(), "test").unwrap();
    let verify = lks_core::script::parse::parse_script_verify(&json!({"min_interruptions": 2}))
        .unwrap()
        .unwrap();
    let events = vec![
        ev("run.started", 0, json!({})),
        cue("g", 100, true, 400),
        ev("interruption", 200, json!({"by": "sim"})),
    ];
    let res = evaluate_script_log(&events, &steps, Some(&verify));
    // only 1 interruption < 2 → min_interruptions check fails
    assert_eq!(res["pass"], json!(false));
    let checks = res["checks"].as_array().unwrap();
    assert!(
        checks
            .iter()
            .any(|c| c["check"] == json!("min_interruptions") && c["pass"] == json!(false)),
        "min_interruptions failing check"
    );
    assert_eq!(res["interruptions"], json!(1));
}
