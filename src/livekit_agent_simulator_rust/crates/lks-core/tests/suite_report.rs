//! Suite report parity — port of `tests/test_suite.py` vectors against the
//! Rust `lks_core::suite` module (build_suite_report / write_suite_report /
//! suite_report_markdown / _scenario_id_of).

use lks_core::suite::{
    build_suite_report, evaluate_run_result, suite_report_markdown, write_suite_report,
};
use serde_json::{json, Map, Value as Json};

fn ok_result(overrides: &[(&str, Json)]) -> Map<String, Json> {
    let mut m = Map::new();
    m.insert("executed".into(), json!(true));
    m.insert("status".into(), json!("done"));
    m.insert("run_id".into(), json!("smoke-hello-20260101-000000-aaaa"));
    let mut validation = Map::new();
    validation.insert("valid".into(), json!(true));
    validation.insert("id".into(), json!("smoke-hello"));
    m.insert("validation".into(), Json::Object(validation));
    let mut summary = Map::new();
    summary.insert("run_id".into(), json!("smoke-hello-20260101-000000-aaaa"));
    summary.insert("status".into(), json!("done"));
    summary.insert("duration_ms".into(), json!(1000));
    summary.insert("turn_count".into(), json!(2));
    let mut av = Map::new();
    av.insert("pass".into(), json!(true));
    av.insert("skipped".into(), json!(false));
    summary.insert("assert_verify".into(), Json::Object(av));
    let mut sv = Map::new();
    sv.insert("pass".into(), json!(true));
    summary.insert("script_verify".into(), Json::Object(sv));
    let mut verdict = Map::new();
    verdict.insert("verdict".into(), json!("pass"));
    verdict.insert("score".into(), json!(100));
    summary.insert("verdict".into(), Json::Object(verdict));
    let mut metrics = Map::new();
    metrics.insert("ttfw_ms".into(), json!(400));
    let mut tt = Map::new();
    tt.insert("p50".into(), json!(800));
    tt.insert("p95".into(), json!(1200));
    tt.insert("count".into(), json!(2));
    metrics.insert("turn_taking_ms".into(), Json::Object(tt));
    let mut rec = Map::new();
    rec.insert("p50".into(), json!(500));
    rec.insert("count".into(), json!(1));
    metrics.insert("recovery_ms".into(), Json::Object(rec));
    metrics.insert("barge_count".into(), json!(1));
    metrics.insert("barge_recovery_rate".into(), json!(1.0));
    summary.insert("metrics".into(), Json::Object(metrics));
    m.insert("summary".into(), Json::Object(summary));

    for (k, v) in overrides {
        m.insert(k.to_string(), v.clone());
    }
    m
}

/// Override a nested path in the summary (dot-separated, relative to summary).
fn summary_override(base: &mut Map<String, Json>, path: &str, value: Json) {
    let mut parts: Vec<&str> = path.split('.').collect();
    let leaf = parts.pop().unwrap();
    let mut cur = base
        .get_mut("summary")
        .and_then(|v| v.as_object_mut())
        .expect("summary object");
    for part in parts {
        cur = cur
            .get_mut(part)
            .and_then(|v| v.as_object_mut())
            .expect("nested object");
    }
    cur.insert(leaf.to_string(), value);
}

#[test]
fn gate_pass() {
    let g = evaluate_run_result(&ok_result(&[]), false);
    assert_eq!(g.get("ok").and_then(|v| v.as_bool()), Some(true));
    assert_eq!(g.get("gate").and_then(|v| v.as_str()), Some("pass"));
}

#[test]
fn gate_assert_hard_fail() {
    let mut r = ok_result(&[]);
    summary_override(
        &mut r,
        "assert_verify",
        json!({"pass": false, "skipped": false}),
    );
    let g = evaluate_run_result(&r, false);
    assert_eq!(g.get("ok").and_then(|v| v.as_bool()), Some(false));
    let hard = g
        .get("hard_reasons")
        .and_then(|v| v.as_array())
        .cloned()
        .unwrap_or_default();
    assert!(hard.iter().any(|x| x.as_str() == Some("assert_verify")));
}

#[test]
fn gate_script_hard_fail() {
    let mut r = ok_result(&[]);
    summary_override(&mut r, "script_verify", json!({"pass": false}));
    let g = evaluate_run_result(&r, false);
    assert_eq!(g.get("ok").and_then(|v| v.as_bool()), Some(false));
    let hard = g
        .get("hard_reasons")
        .and_then(|v| v.as_array())
        .cloned()
        .unwrap_or_default();
    assert!(hard.iter().any(|x| x.as_str() == Some("script_verify")));
}

#[test]
fn gate_judge_soft_by_default() {
    let mut r = ok_result(&[]);
    summary_override(&mut r, "verdict", json!({"verdict": "fail", "score": 0}));
    let g = evaluate_run_result(&r, false);
    assert_eq!(g.get("ok").and_then(|v| v.as_bool()), Some(true));
    assert_eq!(g.get("soft_fail").and_then(|v| v.as_bool()), Some(true));
    let soft = g
        .get("soft_reasons")
        .and_then(|v| v.as_array())
        .cloned()
        .unwrap_or_default();
    assert!(soft.iter().any(|x| x.as_str() == Some("judge_fail")));
}

#[test]
fn gate_judge_error_never_hard() {
    let mut r = ok_result(&[]);
    summary_override(
        &mut r,
        "verdict",
        json!({"verdict": "error", "notes": "HTTP judge 401: unauthorized"}),
    );
    let g = evaluate_run_result(&r, true);
    assert_eq!(g.get("ok").and_then(|v| v.as_bool()), Some(true));
    let soft = g
        .get("soft_reasons")
        .and_then(|v| v.as_array())
        .cloned()
        .unwrap_or_default();
    assert!(soft.iter().any(|x| x.as_str() == Some("judge_error")));
    let hard = g
        .get("hard_reasons")
        .and_then(|v| v.as_array())
        .cloned()
        .unwrap_or_default();
    assert!(!hard.iter().any(|x| x.as_str() == Some("judge_error")));
}

#[test]
fn gate_judge_skipped_ignored() {
    let mut r = ok_result(&[]);
    summary_override(
        &mut r,
        "verdict",
        json!({"verdict": "skipped", "notes": "HTTP judge needs judge.api_key or JUDGE_API_KEY."}),
    );
    let g = evaluate_run_result(&r, true);
    assert_eq!(g.get("ok").and_then(|v| v.as_bool()), Some(true));
    assert_eq!(g.get("soft_fail").and_then(|v| v.as_bool()), Some(false));
}

#[test]
fn gate_status_failed() {
    let mut r = ok_result(&[("status", json!("failed"))]);
    let g = evaluate_run_result(&r, false);
    assert_eq!(g.get("ok").and_then(|v| v.as_bool()), Some(false));
    let hard = g
        .get("hard_reasons")
        .and_then(|v| v.as_array())
        .cloned()
        .unwrap_or_default();
    assert!(hard.iter().any(|x| x
        .as_str()
        .map(|s| s.starts_with("status:"))
        .unwrap_or(false)));
}

#[test]
fn suite_matrix_and_write() {
    let good = ok_result(&[]);
    let mut bad = ok_result(&[]);
    bad.get_mut("validation")
        .and_then(|v| v.as_object_mut())
        .unwrap()
        .insert("id".into(), json!("bad-case"));
    summary_override(
        &mut bad,
        "assert_verify",
        json!({"pass": false, "skipped": false}),
    );
    let mut soft = ok_result(&[]);
    soft.get_mut("validation")
        .and_then(|v| v.as_object_mut())
        .unwrap()
        .insert("id".into(), json!("soft-judge"));
    summary_override(
        &mut soft,
        "verdict",
        json!({"verdict": "fail", "score": 40}),
    );

    let report = build_suite_report(&[good.clone(), bad, soft.clone()], false, None);
    let totals = report.get("totals").and_then(|v| v.as_object()).unwrap();
    assert_eq!(totals.get("total").and_then(|v| v.as_i64()), Some(3));
    assert_eq!(totals.get("failed_hard").and_then(|v| v.as_i64()), Some(1));
    assert_eq!(
        totals.get("failed_soft_judge").and_then(|v| v.as_i64()),
        Some(1)
    );
    assert_eq!(report.get("ok").and_then(|v| v.as_bool()), Some(false));
    assert_eq!(report.get("exit_code").and_then(|v| v.as_i64()), Some(1));

    let report2 = build_suite_report(&[good.clone(), soft], false, None);
    assert_eq!(report2.get("ok").and_then(|v| v.as_bool()), Some(true));
    assert_eq!(report2.get("exit_code").and_then(|v| v.as_i64()), Some(0));

    let tmp = std::env::temp_dir().join(format!("suite-test-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&tmp);
    let paths = write_suite_report(&report, &tmp, Some("suite-test")).unwrap();
    assert!(std::path::Path::new(paths.get("json").and_then(|v| v.as_str()).unwrap()).exists());
    let data: Json = serde_json::from_str(
        &std::fs::read_to_string(paths.get("json").and_then(|v| v.as_str()).unwrap()).unwrap(),
    )
    .unwrap();
    assert_eq!(data.get("suite").and_then(|v| v.as_bool()), Some(true));
    let md = suite_report_markdown(&report);
    assert!(md.contains("Suite report"));
    assert!(md.contains("bad-case") || md.contains("smoke-hello"));
    assert!(md.to_lowercase().contains("ttfw") || md.contains("p50"));
    // matrix carries metric columns
    let row0 = report.get("matrix").and_then(|v| v.as_array()).unwrap()[0]
        .as_object()
        .unwrap();
    assert_eq!(row0.get("turn_p50_ms").and_then(|v| v.as_i64()), Some(800));
    assert_eq!(row0.get("ttfw_ms").and_then(|v| v.as_i64()), Some(400));
    let _ = std::fs::remove_dir_all(&tmp);
}

#[test]
fn scenario_id_of_run_id() {
    // _scenario_id_of: validation.id wins; else run_id rsplit("-",3)[0]
    let r = ok_result(&[]);
    let report = build_suite_report(&[r], false, None);
    let row = report.get("matrix").and_then(|v| v.as_array()).unwrap()[0]
        .as_object()
        .unwrap();
    assert_eq!(
        row.get("scenario_id").and_then(|v| v.as_str()),
        Some("smoke-hello")
    );

    // No validation id → run_id before last 3 dashes
    let mut r2 = ok_result(&[]);
    r2.remove("validation");
    let report2 = build_suite_report(&[r2], false, None);
    let row2 = report2.get("matrix").and_then(|v| v.as_array()).unwrap()[0]
        .as_object()
        .unwrap();
    assert_eq!(
        row2.get("scenario_id").and_then(|v| v.as_str()),
        Some("smoke-hello")
    );
}

#[test]
fn scenario_id_of_less_than_3_dashes() {
    let mut r = Map::new();
    r.insert("run_id".into(), json!("abc-1-2"));
    r.insert("executed".into(), json!(true));
    r.insert("status".into(), json!("done"));
    let report = build_suite_report(&[r], false, None);
    let row = report.get("matrix").and_then(|v| v.as_array()).unwrap()[0]
        .as_object()
        .unwrap();
    assert_eq!(
        row.get("scenario_id").and_then(|v| v.as_str()),
        Some("abc-1-2")
    );
}
