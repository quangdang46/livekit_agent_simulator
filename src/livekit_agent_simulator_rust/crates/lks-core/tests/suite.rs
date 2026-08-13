//! Tests for suite gate (evaluate_run_result).
use lks_core::suite::evaluate_run_result;
use serde_json::{json, Map, Value as Json};

fn result() -> Map<String, Json> {
    let mut m = Map::new();
    m.insert("executed".into(), json!(true));
    m.insert("status".into(), json!("done"));
    m.insert("summary".into(), Json::Object(Map::new()));
    m
}

fn summary_json(v: Json) -> Json {
    // {"summary": v}
    let mut m = Map::new();
    m.insert("summary".into(), v);
    Json::Object(m)
}

#[test]
fn pass_gate() {
    let r = result();
    let g = evaluate_run_result(&r, false);
    assert_eq!(g["ok"], json!(true));
    assert_eq!(g["gate"], json!("pass"));
}

#[test]
fn not_executed_hard() {
    let mut r = result();
    r.insert("executed".into(), json!(false));
    let g = evaluate_run_result(&r, false);
    assert_eq!(g["ok"], json!(false));
    assert!(g["hard_reasons"]
        .as_array()
        .unwrap()
        .iter()
        .any(|x| x == "not_executed"));
}

#[test]
fn status_not_done_hard() {
    let mut r = result();
    r.insert("status".into(), json!("failed"));
    let g = evaluate_run_result(&r, false);
    assert_eq!(g["gate"], json!("hard"));
    assert!(g["hard_reasons"]
        .as_array()
        .unwrap()
        .iter()
        .any(|x| x == "status:failed"));
}

#[test]
fn assert_verify_fail_hard() {
    let mut r = result();
    r.insert(
        "summary".into(),
        json!({"assert_verify": {"pass": false, "skipped": false}}),
    );
    let g = evaluate_run_result(&r, false);
    assert_eq!(g["gate"], json!("hard"));
    assert!(g["hard_reasons"]
        .as_array()
        .unwrap()
        .iter()
        .any(|x| x == "assert_verify"));
}

#[test]
fn assert_verify_skipped_not_hard() {
    let mut r = result();
    r.insert(
        "summary".into(),
        json!({"assert_verify": {"pass": false, "skipped": true}}),
    );
    let g = evaluate_run_result(&r, false);
    assert_eq!(g["gate"], json!("pass"), "skipped assert not hard");
}

#[test]
fn script_verify_fail_hard() {
    let mut r = result();
    r.insert("summary".into(), json!({"script_verify": {"pass": false}}));
    let g = evaluate_run_result(&r, false);
    assert!(g["hard_reasons"]
        .as_array()
        .unwrap()
        .iter()
        .any(|x| x == "script_verify"));
}

#[test]
fn judge_fail_soft_unless_strict() {
    let mut r = result();
    r.insert("summary".into(), json!({"verdict": {"verdict": "fail"}}));
    let g = evaluate_run_result(&r, false);
    assert_eq!(g["gate"], json!("soft"));
    assert!(g["soft_reasons"]
        .as_array()
        .unwrap()
        .iter()
        .any(|x| x == "judge_fail"));
    let gs = evaluate_run_result(&r, true);
    assert_eq!(
        gs["gate"],
        json!("hard"),
        "strict_judge promotes judge_fail to hard"
    );
    assert!(gs["hard_reasons"]
        .as_array()
        .unwrap()
        .iter()
        .any(|x| x == "judge_fail"));
}

#[test]
fn judge_error_never_hard() {
    let mut r = result();
    r.insert("summary".into(), json!({"verdict": {"verdict": "error"}}));
    let g = evaluate_run_result(&r, true);
    assert_eq!(
        g["gate"],
        json!("soft"),
        "judge_error never hard even strict"
    );
}

#[test]
fn judge_skipped_ignored() {
    let mut r = result();
    r.insert("summary".into(), json!({"verdict": {"verdict": "skipped"}}));
    let g = evaluate_run_result(&r, false);
    assert_eq!(g["gate"], json!("pass"));
}

#[test]
fn validation_failed_hard() {
    let mut r = result();
    r.insert(
        "validation".into(),
        json!({"valid": false, "error": "bad scenario"}),
    );
    let g = evaluate_run_result(&r, false);
    assert!(g["hard_reasons"]
        .as_array()
        .unwrap()
        .iter()
        .any(|x| x == "validation_failed"));
}

#[test]
fn not_executed_with_error() {
    let mut r = result();
    r.insert("executed".into(), json!(false));
    r.insert("error".into(), json!("boom"));
    let g = evaluate_run_result(&r, false);
    assert!(g["hard_reasons"]
        .as_array()
        .unwrap()
        .iter()
        .any(|x| x == "error:boom"));
}

// silence the unused helper
#[allow(dead_code)]
fn _u() {
    let _ = summary_json(Json::Null);
}
