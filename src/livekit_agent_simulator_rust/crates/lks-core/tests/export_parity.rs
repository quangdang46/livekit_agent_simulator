//! Export/validate parity regression tests (P5 data-plane): the fixed
//! divergences found against the Python reference on the dtmf-feature suite.

use lks_core::scenario_yaml::scenario_to_dict;
use serde_json::json;

fn scenario_from_yaml(text: &str) -> lks_core::scenario::Scenario {
    use std::sync::atomic::{AtomicUsize, Ordering};
    static N: AtomicUsize = AtomicUsize::new(0);
    let dir = std::env::temp_dir().join("lks_export_parity");
    std::fs::create_dir_all(&dir).unwrap();
    let path = dir.join(format!(
        "s{}_{}.yaml",
        std::process::id(),
        N.fetch_add(1, Ordering::SeqCst)
    ));
    std::fs::write(&path, text).unwrap();
    lks_core::scenario_yaml::load_scenario_yaml(&path).expect("parse")
}

#[test]
fn script_steps_normalize_full_field_set() {
    // dtmf-menu regression: typed steps carry the full Python field set.
    let s = scenario_from_yaml(
        "apiVersion: agent-sim/v1\nkind: Scenario\nmetadata:\n  id: t\npersona:\n  brief: b\nscript:\n  steps:\n    - id: press-1\n      trigger: time\n      delay_ms: 700\n      say: '[DTMF: 1]'\n      action: dtmf\n      digits: '1'\n",
    );
    let d = scenario_to_dict(&s);
    let steps = d["script"]["steps"].as_array().unwrap();
    let step = &steps[0];
    // Full dataclass-asdict field set with defaults (Python parity).
    assert_eq!(step["trigger"], json!("time"));
    assert_eq!(step["say"], json!("[DTMF: 1]"));
    assert_eq!(step["once"], json!(true));
    assert_eq!(step["min_agent_active_ms"], json!(400));
    assert_eq!(step["delivery"], json!("gemini_text"));
    assert_eq!(step["require_agent_spoke_first"], json!(true));
    assert_eq!(step["defer_on_open_question"], json!(true));
    assert_eq!(step["open_question_idle_ms"], json!(20000));
    assert_eq!(step["barge_in"], json!(false));
    assert_eq!(step["with_blip"], json!(false));
    assert_eq!(step["gain"], json!(1.0));
    assert_eq!(step["overlay"], json!(null));
    assert_eq!(step["interrupt_class"], json!(null));
}

#[test]
fn script_verify_normalize_full_field_set() {
    // character-impatient regression: verify carries min/max_interruptions,
    // plugins, plugin_options even when absent in source.
    let s = scenario_from_yaml(
        "apiVersion: agent-sim/v1\nkind: Scenario\nmetadata:\n  id: t\npersona:\n  brief: b\nscript:\n  steps:\n    - id: a\n      trigger: time\n      say: hi\n  verify:\n    require_during_agent_speech: false\n    min_agent_finals_after_barge_in: 1\n",
    );
    let d = scenario_to_dict(&s);
    let v = &d["script"]["verify"];
    assert_eq!(v["require_during_agent_speech"], json!(false));
    assert_eq!(v["min_agent_finals_after_barge_in"], json!(1));
    assert_eq!(v["min_interruptions"], json!(null));
    assert_eq!(v["max_interruptions"], json!(null));
    assert_eq!(v["plugins"], json!([]));
    assert_eq!(v["plugin_options"], json!({}));
}

#[test]
fn assert_section_normalized_with_prompt() {
    // dialogue-signup-basic regression: llm_bool outcome keeps `prompt`,
    // full outcome field set with defaults.
    let s = scenario_from_yaml(
        "apiVersion: agent-sim/v1\nkind: Scenario\nmetadata:\n  id: t\npersona:\n  brief: b\nassert:\n  outcomes:\n    - id: judged\n      type: llm_bool\n      prompt: Did the caller pursue signup?\n",
    );
    let d = scenario_to_dict(&s);
    let oc = &d["assert"]["outcomes"][0];
    assert_eq!(oc["prompt"], json!("Did the caller pursue signup?"));
    assert_eq!(oc["type"], json!("llm_bool"));
    assert_eq!(oc["role"], json!("any"));
    assert_eq!(oc["min_agent_finals_after_barge_in"], json!(1));
    assert_eq!(oc["require_turn_samples"], json!(0));
    assert_eq!(oc["check_agent_transcript"], json!(false));
}

#[test]
fn caller_steps_roundtrip_preserves_trigger_and_barge() {
    // Slice parity: caller_steps parse -> export -> re-parse keeps every
    // field (the pre-parity gap silently dropped trigger:/barge_in:).
    let s = scenario_from_yaml(
        "apiVersion: agent-sim/v1\nkind: Scenario\nmetadata:\n  id: t\npersona:\n  brief: b\ncaller_steps:\n  - say: \"Wait a second...\"\n    trigger:\n      kind: agent_speaking\n      min_agent_active_ms: 350\n      delay_ms: 350\n    barge_in: true\n  - play_audio:\n      asset: builtin:noise.ambient\n      gain: 0.3\n      loop: true\n    trigger:\n      kind: time\n      delay_ms: 1500\n  - interrupt: true\n  - wait: 100\n  - end: true\n",
    );
    assert_eq!(s.caller_actions.len(), 5);
    assert_eq!(s.caller_actions[0].kind, "say");
    assert_eq!(s.caller_actions[1].kind, "play_audio");
    let d = scenario_to_dict(&s);
    let steps = d["caller_steps"].as_array().expect("caller_steps exported");
    assert_eq!(steps.len(), 5);
    assert_eq!(steps[0]["trigger"]["kind"], json!("agent_speaking"));
    assert_eq!(steps[0]["trigger"]["min_agent_active_ms"], json!(350));
    assert_eq!(steps[0]["barge_in"], json!(true));
    assert_eq!(steps[1]["play_audio"]["asset"], json!("builtin:noise.ambient"));
    assert_eq!(steps[1]["trigger"]["kind"], json!("time"));
    assert_eq!(steps[4].get("end"), Some(&json!(true)));
}

#[test]
fn caller_steps_invalid_specs_fail_loudly() {
    // Strictness parity: unknown trigger kind / sibling typo / bad gain
    // are hard errors, never silent drops.
    for bad in [
        "apiVersion: agent-sim/v1\nkind: Scenario\nmetadata:\n  id: t\npersona:\n  brief: b\ncaller_steps:\n  - say: Hi\n    trigger:\n      kind: banana\n",
        "apiVersion: agent-sim/v1\nkind: Scenario\nmetadata:\n  id: t\npersona:\n  brief: b\ncaller_steps:\n  - say: Hi\n    frobnicate: 1\n",
        "apiVersion: agent-sim/v1\nkind: Scenario\nmetadata:\n  id: t\npersona:\n  brief: b\ncaller_steps:\n  - play_audio:\n      asset: x\n      gain: 2.0\n",
    ] {
        let dir = std::env::temp_dir().join("lks_export_parity");
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join(format!("bad_{}.yaml", std::process::id()));
        std::fs::write(&path, bad).unwrap();
        assert!(
            lks_core::scenario_yaml::load_scenario_yaml(&path).is_err(),
            "expected hard error for: {bad:?}"
        );
    }
}
