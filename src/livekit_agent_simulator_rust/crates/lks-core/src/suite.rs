//! Suite gate — score one execute result for CI (port of `suite.py`).
//! Never hard-fails CI for skipped/error judge (misconfig or transport) even with strict_judge.

use serde_json::{json, Map, Value as Json};

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
