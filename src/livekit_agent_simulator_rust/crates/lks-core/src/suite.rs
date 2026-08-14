//! Suite gate — score one execute result for CI (port of `suite.py`).
//! Never hard-fails CI for skipped/error judge (misconfig or transport) even with strict_judge.

use serde_json::{json, Map, Value as Json};

/// The 12-key flat digest used by compare_runs / suite rows (port of
/// `metrics.py:metrics_digest`). Returns a map with the standard key set.
fn metrics_digest(metrics: Option<&Map<String, Json>>) -> Map<String, Json> {
    crate::ops::metrics_digest(metrics)
}

/// Score one execute_scenario result for CI. Returns {ok, hard_fail, soft_fail,
/// hard_reasons, soft_reasons, gate}.
pub fn evaluate_run_result(result: &Map<String, Json>, strict_judge: bool) -> Map<String, Json> {
    let mut hard: Vec<String> = Vec::new();
    let mut soft: Vec<String> = Vec::new();

    let executed = result
        .get("executed")
        .and_then(|v| v.as_bool())
        .unwrap_or(false);
    if !executed {
        hard.push("not_executed".to_string());
        let err = result
            .get("error")
            .and_then(|v| v.as_str())
            .map(String::from)
            .or_else(|| {
                result
                    .get("validation")
                    .and_then(|v| v.as_object())
                    .and_then(|m| m.get("error"))
                    .and_then(|v| v.as_str())
                    .map(String::from)
            });
        if let Some(e) = err {
            hard.push(format!("error:{e}"));
        }
    } else {
        let val = result.get("validation").and_then(|v| v.as_object());
        if let Some(v) = val {
            if v.get("valid").and_then(|x| x.as_bool()) == Some(false) {
                hard.push("validation_failed".to_string());
            }
        }
    }

    let status = result.get("status").and_then(|v| v.as_str());
    if executed {
        if let Some(st) = status {
            if st != "done" {
                hard.push(format!("status:{st}"));
            }
        }
    }

    let summary = result.get("summary").and_then(|v| v.as_object());
    let av = summary
        .and_then(|s| s.get("assert_verify"))
        .and_then(|v| v.as_object());
    if let Some(av) = av {
        let skipped = av.get("skipped").and_then(|v| v.as_bool()).unwrap_or(false);
        let pass = av.get("pass").and_then(|v| v.as_bool()).unwrap_or(false);
        if !skipped && !pass {
            hard.push("assert_verify".to_string());
        }
    }
    let sv = summary
        .and_then(|s| s.get("script_verify"))
        .and_then(|v| v.as_object());
    if let Some(sv) = sv {
        if sv.get("pass").and_then(|v| v.as_bool()) == Some(false) {
            hard.push("script_verify".to_string());
        }
    }

    let verdict = summary
        .and_then(|s| s.get("verdict"))
        .and_then(|v| v.as_object());
    if let Some(v) = verdict {
        let jv = v
            .get("verdict")
            .and_then(|x| x.as_str())
            .unwrap_or("")
            .to_lowercase();
        match jv.as_str() {
            "fail" => {
                soft.push("judge_fail".to_string());
                if strict_judge {
                    hard.push("judge_fail".to_string());
                }
            }
            "maybe" => {
                soft.push("judge_maybe".to_string());
                if strict_judge {
                    hard.push("judge_maybe".to_string());
                }
            }
            "error" => {
                // Misconfig / HTTP / parse — visible soft note only; never CI hard gate.
                soft.push("judge_error".to_string());
            }
            _ => {} // skipped → ignore (same UX as no PassCriteria)
        }
    }

    let hard_fail = !hard.is_empty();
    let soft_fail = !soft.is_empty();
    let gate = if hard_fail {
        "hard"
    } else if soft_fail {
        "soft"
    } else {
        "pass"
    };
    let mut m = Map::new();
    m.insert("ok".into(), json!(!hard_fail));
    m.insert("hard_fail".into(), json!(hard_fail));
    m.insert("soft_fail".into(), json!(soft_fail));
    m.insert(
        "hard_reasons".into(),
        Json::Array(hard.into_iter().map(Json::String).collect()),
    );
    m.insert(
        "soft_reasons".into(),
        Json::Array(soft.into_iter().map(Json::String).collect()),
    );
    m.insert("gate".into(), json!(gate));
    m
}

fn _summary(result: &Map<String, Json>) -> Map<String, Json> {
    result
        .get("summary")
        .and_then(|v| v.as_object())
        .cloned()
        .unwrap_or_default()
}

/// Scenario id of a result row (port of `suite.py:_scenario_id_of`).
fn _scenario_id_of(result: &Map<String, Json>) -> String {
    if let Some(sid) = result.get("scenario_id").and_then(|v| v.as_str()) {
        return sid.to_string();
    }
    if let Some(valid) = result.get("validation").and_then(|v| v.as_object()) {
        if let Some(id) = valid.get("id").and_then(|v| v.as_str()) {
            return id.to_string();
        }
    }
    let summary = _summary(result);
    let rid = summary
        .get("run_id")
        .and_then(|v| v.as_str())
        .or_else(|| result.get("run_id").and_then(|v| v.as_str()))
        .unwrap_or("?");
    // run_id is often scenario-timestamp; rsplit("-", 3)[0] = before the last 3 dashes
    if rid.contains('-') && rid.chars().filter(|c| *c == '-').count() >= 3 {
        // rsplitn(4) yields [last, 2nd-last, 3rd-last, everything-before] — take the tail.
        if let Some(head) = rid.rsplitn(4, '-').last() {
            return head.to_string();
        }
    }
    rid.to_string()
}

/// Build matrix + totals for execute_scenarios results (port of
/// `suite.py:build_suite_report`). Returns the `suite` dict.
pub fn build_suite_report(
    results: &[Map<String, Json>],
    strict_judge: bool,
    tag: Option<&str>,
) -> Map<String, Json> {
    let mut rows: Vec<Json> = Vec::new();
    let mut passed = 0usize;
    let mut failed_hard = 0usize;
    let mut failed_soft = 0usize;

    for r in results {
        let gate = evaluate_run_result(r, strict_judge);
        let summary = _summary(r);
        let verdict = summary
            .get("verdict")
            .and_then(|v| v.as_object())
            .cloned()
            .unwrap_or_default();
        let av = summary
            .get("assert_verify")
            .and_then(|v| v.as_object())
            .cloned()
            .unwrap_or_default();
        let sv = summary
            .get("script_verify")
            .and_then(|v| v.as_object())
            .cloned()
            .unwrap_or_default();
        let mdig = metrics_digest(summary.get("metrics").and_then(|v| v.as_object()));

        let gate_map = gate;
        let mut row = Map::new();
        row.insert("scenario_id".into(), json!(_scenario_id_of(r)));
        row.insert(
            "run_id".into(),
            r.get("run_id")
                .cloned()
                .or_else(|| summary.get("run_id").cloned())
                .unwrap_or(Json::Null),
        );
        row.insert(
            "status".into(),
            json!(r
                .get("status")
                .and_then(|v| v.as_str())
                .map(String::from)
                .unwrap_or_else(|| {
                    if r.get("executed").and_then(|v| v.as_bool()) == Some(false) {
                        "error".to_string()
                    } else {
                        "?".to_string()
                    }
                })),
        );
        row.insert(
            "executed".into(),
            json!(r.get("executed").and_then(|v| v.as_bool()).unwrap_or(false)),
        );
        row.insert(
            "gate".into(),
            gate_map.get("gate").cloned().unwrap_or(json!("?")),
        );
        row.insert(
            "ok".into(),
            gate_map.get("ok").cloned().unwrap_or(json!(false)),
        );
        row.insert(
            "hard_reasons".into(),
            gate_map.get("hard_reasons").cloned().unwrap_or(json!([])),
        );
        row.insert(
            "soft_reasons".into(),
            gate_map.get("soft_reasons").cloned().unwrap_or(json!([])),
        );
        row.insert(
            "assert_pass".into(),
            av.get("pass").cloned().unwrap_or(Json::Null),
        );
        row.insert(
            "script_pass".into(),
            sv.get("pass").cloned().unwrap_or(Json::Null),
        );
        row.insert(
            "judge_verdict".into(),
            verdict.get("verdict").cloned().unwrap_or(Json::Null),
        );
        row.insert(
            "judge_score".into(),
            verdict.get("score").cloned().unwrap_or(Json::Null),
        );
        row.insert(
            "duration_ms".into(),
            summary
                .get("duration_ms")
                .cloned()
                .or_else(|| r.get("duration_ms").cloned())
                .unwrap_or(Json::Null),
        );
        row.insert(
            "turn_count".into(),
            summary.get("turn_count").cloned().unwrap_or(Json::Null),
        );
        row.insert(
            "caller_mode".into(),
            summary
                .get("caller_mode")
                .cloned()
                .or_else(|| r.get("caller_mode").cloned())
                .unwrap_or(Json::Null),
        );
        row.insert(
            "dial_ms".into(),
            summary
                .get("dial_ms")
                .cloned()
                .or_else(|| r.get("dial_ms").cloned())
                .unwrap_or(Json::Null),
        );
        row.insert(
            "sip_status".into(),
            summary
                .get("sip_status")
                .cloned()
                .or_else(|| r.get("sip_status").cloned())
                .unwrap_or(Json::Null),
        );
        row.insert(
            "ttfw_ms".into(),
            mdig.get("ttfw_ms").cloned().unwrap_or(Json::Null),
        );
        row.insert(
            "turn_p50_ms".into(),
            mdig.get("turn_p50_ms").cloned().unwrap_or(Json::Null),
        );
        row.insert(
            "turn_p95_ms".into(),
            mdig.get("turn_p95_ms").cloned().unwrap_or(Json::Null),
        );
        row.insert(
            "recovery_p50_ms".into(),
            mdig.get("recovery_p50_ms").cloned().unwrap_or(Json::Null),
        );
        row.insert(
            "barge_count".into(),
            mdig.get("barge_count").cloned().unwrap_or(Json::Null),
        );
        row.insert(
            "barge_recovery_rate".into(),
            mdig.get("barge_recovery_rate")
                .cloned()
                .unwrap_or(Json::Null),
        );
        row.insert(
            "report_dir".into(),
            r.get("report_dir").cloned().unwrap_or(Json::Null),
        );
        row.insert(
            "error".into(),
            r.get("error").cloned().unwrap_or(Json::Null),
        );
        rows.push(Json::Object(row));

        let hard_fail = gate_map
            .get("hard_fail")
            .and_then(|v| v.as_bool())
            .unwrap_or(false);
        if hard_fail {
            failed_hard += 1;
        } else {
            let soft_fail = gate_map
                .get("soft_fail")
                .and_then(|v| v.as_bool())
                .unwrap_or(false);
            if soft_fail {
                failed_soft += 1;
                passed += 1; // soft still counts as CI pass when not strict
            } else {
                passed += 1;
            }
        }
    }

    let total = rows.len();
    let ok = failed_hard == 0;
    let mut m = Map::new();
    m.insert("suite".into(), json!(true));
    m.insert("ok".into(), json!(ok));
    m.insert("strict_judge".into(), json!(strict_judge));
    m.insert(
        "tag".into(),
        tag.map(|t| Json::String(t.to_string()))
            .unwrap_or(Json::Null),
    );
    m.insert(
        "generated_utc".into(),
        json!(jiff::Zoned::now()
            .strftime("%Y-%m-%dT%H:%M:%S%.f%:z")
            .to_string()),
    );
    let mut totals = Map::new();
    totals.insert("total".into(), json!(total));
    totals.insert("passed_gate".into(), json!(passed));
    totals.insert("failed_hard".into(), json!(failed_hard));
    totals.insert("failed_soft_judge".into(), json!(failed_soft));
    m.insert("totals".into(), Json::Object(totals));
    m.insert("matrix".into(), Json::Array(rows));
    m.insert("exit_code".into(), json!(if ok { 0 } else { 1 }));
    m
}

/// Human-readable suite matrix (port of `suite.py:suite_report_markdown`).
pub fn suite_report_markdown(report: &Map<String, Json>) -> String {
    let totals = report
        .get("totals")
        .and_then(|v| v.as_object())
        .cloned()
        .unwrap_or_default();
    let mut lines: Vec<String> = Vec::new();
    lines.push("# Suite report".to_string());
    lines.push("".to_string());
    lines.push(format!(
        "- generated: `{}`",
        report
            .get("generated_utc")
            .and_then(|v| v.as_str())
            .unwrap_or("")
    ));
    lines.push(format!(
        "- strict_judge: `{}`",
        report
            .get("strict_judge")
            .and_then(|v| v.as_bool())
            .unwrap_or(false)
    ));
    lines.push(format!(
        "- tag: `{}`",
        report.get("tag").and_then(|v| v.as_str()).unwrap_or("None")
    ));
    lines.push(format!(
        "- total: **{}** · gate pass: **{}** · hard fail: **{}** · soft judge fail: **{}**",
        totals.get("total").and_then(|v| v.as_i64()).unwrap_or(0),
        totals
            .get("passed_gate")
            .and_then(|v| v.as_i64())
            .unwrap_or(0),
        totals
            .get("failed_hard")
            .and_then(|v| v.as_i64())
            .unwrap_or(0),
        totals
            .get("failed_soft_judge")
            .and_then(|v| v.as_i64())
            .unwrap_or(0),
    ));
    lines.push(format!(
        "- suite ok (CI): **{}** (exit {})",
        report.get("ok").and_then(|v| v.as_bool()).unwrap_or(false),
        report
            .get("exit_code")
            .and_then(|v| v.as_i64())
            .unwrap_or(1)
    ));
    lines.push("".to_string());
    lines.push(
        "| scenario | gate | status | assert | script | judge | p50 | p95 | ttfw | duration | run_id |".to_string(),
    );
    lines.push("|---|---|---|---|---|---|---|---|---|---|---|".to_string());

    for row in report
        .get("matrix")
        .and_then(|v| v.as_array())
        .cloned()
        .unwrap_or_default()
    {
        let row = row.as_object().cloned().unwrap_or_default();
        let dur = row.get("duration_ms").and_then(|v| v.as_i64()).unwrap_or(0);
        let dur_s = format!("{:.1}s", dur as f64 / 1000.0);

        fn _ms(v: Option<&Json>) -> String {
            match v.and_then(|x| x.as_i64()) {
                Some(n) => format!("{n}ms"),
                None => "—".to_string(),
            }
        }

        let reasons: Vec<String> = row
            .get("hard_reasons")
            .and_then(|v| v.as_array())
            .map(|a| {
                a.iter()
                    .filter_map(|x| x.as_str().map(String::from))
                    .collect()
            })
            .unwrap_or_default();
        let reasons_s = if reasons.is_empty() {
            "—".to_string()
        } else {
            reasons.join(",")
        };
        lines.push(format!(
            "| {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | `{}` |",
            row.get("scenario_id")
                .and_then(|v| v.as_str())
                .unwrap_or("?"),
            row.get("gate").and_then(|v| v.as_str()).unwrap_or("?"),
            row.get("status").and_then(|v| v.as_str()).unwrap_or("?"),
            row.get("assert_pass")
                .map(|v| v.to_string())
                .unwrap_or_else(|| "None".into()),
            row.get("script_pass")
                .map(|v| v.to_string())
                .unwrap_or_else(|| "None".into()),
            row.get("judge_verdict")
                .and_then(|v| v.as_str())
                .unwrap_or("—"),
            _ms(row.get("turn_p50_ms")),
            _ms(row.get("turn_p95_ms")),
            _ms(row.get("ttfw_ms")),
            dur_s,
            row.get("run_id").and_then(|v| v.as_str()).unwrap_or("—"),
        ));
        if !reasons.is_empty() {
            lines.push(format!("| ↳ hard | {reasons_s} | | | | | | | | | |"));
        }
    }
    lines.push("".to_string());
    lines.join("\n")
}

/// Write suite-*.json and suite-*.md under reports_dir. Returns {json, markdown}
/// paths (port of `suite.py:write_suite_report`).
pub fn write_suite_report(
    report: &Map<String, Json>,
    reports_dir: &std::path::Path,
    stem: Option<&str>,
) -> Result<Map<String, Json>, std::io::Error> {
    std::fs::create_dir_all(reports_dir)?;
    let stamp = match stem {
        Some(s) => s.to_string(),
        None => jiff::Zoned::now().strftime("%Y%m%d-%H%M%S").to_string(),
    };
    let json_path = reports_dir.join(format!("{stamp}.json"));
    let md_path = reports_dir.join(format!("{stamp}.md"));
    std::fs::write(
        &json_path,
        serde_json::to_string_pretty(report).unwrap_or_default(),
    )?;
    std::fs::write(&md_path, suite_report_markdown(report))?;
    let mut m = Map::new();
    m.insert(
        "json".into(),
        json!(json_path.to_string_lossy().into_owned()),
    );
    m.insert(
        "markdown".into(),
        json!(md_path.to_string_lossy().into_owned()),
    );
    Ok(m)
}
