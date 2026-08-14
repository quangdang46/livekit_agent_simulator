//! interruption_rate parse tests (port of test_interrupt_rate.py).

use lks_core::interrupt_rate::parse_interrupt_rate;
use serde_json::json;

#[test]
fn none_returns_none() {
    let p = json!({"speech_conditions": {"interruption_rate": "none"}});
    assert!(parse_interrupt_rate(p.as_object().unwrap())
        .unwrap()
        .is_none());
}

#[test]
fn missing_returns_none() {
    let p = json!({"speech_conditions": {}});
    assert!(parse_interrupt_rate(p.as_object().unwrap())
        .unwrap()
        .is_none());
}

#[test]
fn invalid_rate_errors() {
    let p = json!({"speech_conditions": {"interruption_rate": "insane"}});
    let e = parse_interrupt_rate(p.as_object().unwrap()).unwrap_err();
    assert!(e.contains("must be one of none|low|medium|high"), "{e}");
}

#[test]
fn interval_below_min_errors() {
    let p =
        json!({"speech_conditions": {"interruption_rate": "low", "interruption_interval_ms": 500}});
    assert!(parse_interrupt_rate(p.as_object().unwrap()).is_err());
}

#[test]
fn medium_parses_defaults() {
    let p = json!({"speech_conditions": {"interruption_rate": "medium"}});
    let spec = parse_interrupt_rate(p.as_object().unwrap())
        .unwrap()
        .unwrap();
    assert_eq!(spec.rate, "medium");
    assert_eq!(spec.interval_ms, 45_000);
    assert_eq!(spec.say, "Sorry — one second —");
    assert_eq!(spec.delivery, "gemini_text");
    assert_eq!(spec.interrupt_class, "correction");
}

#[test]
fn silent_mode_off() {
    let p = json!({"speech_conditions": {"interruption_rate": "high", "silent_mode": true}});
    assert!(parse_interrupt_rate(p.as_object().unwrap())
        .unwrap()
        .is_none());
}

#[test]
fn gain_out_of_range_errors() {
    let p = json!({"speech_conditions": {"interruption_rate": "low", "interruption_gain": 1.5}});
    assert!(parse_interrupt_rate(p.as_object().unwrap()).is_err());
}
