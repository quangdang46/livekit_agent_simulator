//! Judgment result types — byte-parity port of `evals/types.py` (no I/O).

use serde_json::{json, Map, Value as Json};

fn as_str(v: &Json) -> String {
    match v {
        Json::String(s) => s.clone(),
        Json::Number(n) => n.to_string(),
        Json::Bool(b) => b.to_string(),
        Json::Null => String::new(),
        other => other.to_string(),
    }
}

fn as_bool(v: &Json) -> bool {
    match v {
        Json::Null => false,
        Json::Bool(b) => *b,
        Json::Number(n) => {
            if let Some(i) = n.as_i64() {
                i != 0
            } else {
                n.as_f64().map(|f| f != 0.0).unwrap_or(true)
            }
        }
        Json::String(s) => !s.is_empty(),
        _ => true,
    }
}

fn as_f64(v: &Json) -> Option<f64> {
    match v {
        Json::Number(n) => n.as_f64(),
        Json::String(s) => s.parse().ok(),
        _ => None,
    }
}

/// Verdict values.
pub const VERDICTS: [&str; 5] = ["pass", "fail", "maybe", "skipped", "error"];
pub const CONFIDENCES: [&str; 3] = ["low", "medium", "high"];

#[derive(Debug, Clone, PartialEq)]
pub struct CriterionScore {
    pub criterion: String,
    pub met: bool,
    pub evidence: String,
    pub relevant: bool,
}

impl CriterionScore {
    pub fn to_dict(&self) -> Json {
        json!({
            "criterion": self.criterion,
            "met": self.met,
            "evidence": self.evidence,
            "relevant": self.relevant,
        })
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct ConversationFeedback {
    pub issue: String,
    pub severity: String,
    pub agent_line: String,
    pub why: String,
}

impl ConversationFeedback {
    pub fn to_dict(&self) -> Json {
        json!({
            "issue": self.issue,
            "severity": self.severity,
            "agent_line": self.agent_line,
            "why": self.why,
        })
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct ReviewIssue {
    pub title: String,
    pub severity: String,
    pub evidence: String,
    pub impact: String,
    pub recommendation: String,
}

impl ReviewIssue {
    pub fn to_dict(&self) -> Json {
        json!({
            "title": self.title,
            "severity": self.severity,
            "evidence": self.evidence,
            "impact": self.impact,
            "recommendation": self.recommendation,
        })
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct JudgmentResult {
    pub verdict: String,
    pub score: Option<f64>,
    pub criteria: Vec<CriterionScore>,
    pub confidence: Option<String>,
    pub needs_human_review: bool,
    pub critical_failure: bool,
    pub notes: String,
    pub judge_id: Option<String>,
    pub conversation_feedback: Vec<ConversationFeedback>,
    pub overall_summary: String,
    pub strengths: Vec<String>,
    pub issues: Vec<ReviewIssue>,
    pub missing_checks: Vec<String>,
    pub language_naturalness: Vec<String>,
    pub final_assessment: Map<String, Json>,
}

impl Default for JudgmentResult {
    fn default() -> Self {
        Self {
            verdict: String::new(),
            score: None,
            criteria: Vec::new(),
            confidence: None,
            needs_human_review: false,
            critical_failure: false,
            notes: String::new(),
            judge_id: None,
            conversation_feedback: Vec::new(),
            overall_summary: String::new(),
            strengths: Vec::new(),
            issues: Vec::new(),
            missing_checks: Vec::new(),
            language_naturalness: Vec::new(),
            final_assessment: Map::new(),
        }
    }
}

impl JudgmentResult {
    /// Python `to_dict`: omits falsy keys (needs_human_review/critical_failure only when True).
    pub fn to_dict(&self) -> Map<String, Json> {
        let mut d = Map::new();
        d.insert("verdict".into(), json!(self.verdict));
        d.insert(
            "score".into(),
            self.score.map(|s| json!(s)).unwrap_or(Json::Null),
        );
        d.insert(
            "criteria".into(),
            Json::Array(self.criteria.iter().map(|c| c.to_dict()).collect()),
        );
        d.insert("notes".into(), json!(self.notes));
        if !self.conversation_feedback.is_empty() {
            d.insert(
                "conversation_feedback".into(),
                Json::Array(
                    self.conversation_feedback
                        .iter()
                        .map(|f| f.to_dict())
                        .collect(),
                ),
            );
        }
        if !self.overall_summary.is_empty() {
            d.insert("overall_summary".into(), json!(self.overall_summary));
        }
        if !self.strengths.is_empty() {
            d.insert("strengths".into(), json!(self.strengths));
        }
        if !self.issues.is_empty() {
            d.insert(
                "issues".into(),
                Json::Array(self.issues.iter().map(|i| i.to_dict()).collect()),
            );
        }
        if !self.missing_checks.is_empty() {
            d.insert("missing_checks".into(), json!(self.missing_checks));
        }
        if !self.language_naturalness.is_empty() {
            d.insert(
                "language_naturalness".into(),
                json!(self.language_naturalness),
            );
        }
        if !self.final_assessment.is_empty() {
            d.insert(
                "final_assessment".into(),
                Json::Object(self.final_assessment.clone()),
            );
        }
        if let Some(c) = &self.confidence {
            d.insert("confidence".into(), json!(c));
        }
        if self.needs_human_review {
            d.insert("needs_human_review".into(), json!(true));
        }
        if self.critical_failure {
            d.insert("critical_failure".into(), json!(true));
        }
        if let Some(j) = &self.judge_id {
            d.insert("judge_id".into(), json!(j));
        }
        d
    }
}

fn str_list(raw: &Json) -> Vec<String> {
    let mut out: Vec<String> = Vec::new();
    if let Some(items) = raw.as_array() {
        for item in items {
            if let Json::Object(m) = item {
                let s = m
                    .get("point")
                    .or_else(|| m.get("item"))
                    .or_else(|| m.get("issue"))
                    .map(as_str)
                    .unwrap_or_default();
                if !s.is_empty() {
                    out.push(s);
                }
            } else if !item.is_null() {
                out.push(as_str(item));
            }
        }
    }
    out.into_iter().filter(|s| !s.is_empty()).collect()
}

/// Normalize LLM JSON into JudgmentResult (tolerant of partial payloads).
/// Repair + parse LLM JSON output (port of `_parse_llm_json` core: fence strip,
/// first-`{` slice, balanced-container truncation). Returns a serde Map.
pub fn repair_json(text: &str) -> Map<String, Json> {
    let mut t = text.trim().to_string();
    // Strip markdown fences.
    if t.starts_with("```") {
        if let Some(idx) = t.find('\n') {
            t = t[idx + 1..].to_string();
        }
        if t.ends_with("```") {
            t = t[..t.len() - 3].to_string();
        }
    }
    // Slice from the first `{`.
    let Some(start) = t.find('{') else {
        return Map::new();
    };
    t = t[start..].to_string();
    // Truncate at the first top-level `}` close.
    let mut depth = 0i32;
    let mut end = 0usize;
    let bytes = t.as_bytes();
    let mut in_str = false;
    let mut esc = false;
    for (i, &b) in bytes.iter().enumerate() {
        if in_str {
            if esc {
                esc = false;
            } else if b == b'\\' {
                esc = true;
            } else if b == b'"' {
                in_str = false;
            }
            continue;
        }
        match b {
            b'"' => in_str = true,
            b'{' | b'[' => depth += 1,
            b'}' | b']' => {
                depth -= 1;
                if depth == 0 {
                    end = i + 1;
                    break;
                }
            }
            _ => {}
        }
    }
    if end == 0 {
        end = t.len();
    }
    let candidate = &t[..end];
    serde_json::from_str(candidate).unwrap_or_default()
}

pub fn parse_judgment_payload(raw: &Map<String, Json>) -> JudgmentResult {
    let mut verdict_raw = as_str(raw.get("verdict").unwrap_or(&Json::String("error".into())))
        .trim()
        .to_lowercase();
    if !VERDICTS.contains(&verdict_raw.as_str()) {
        verdict_raw = "error".to_string();
    }

    let mut criteria: Vec<CriterionScore> = Vec::new();
    // Ungrounded "met" claims (no evidence cited) are flipped to unmet and
    // flag the whole judgment for human review (port of commit #99 — don't
    // trust ungrounded met=true).
    let mut ungrounded_criteria = false;
    if let Some(items) = raw.get("criteria").and_then(|v| v.as_array()) {
        for item in items {
            let Some(m) = item.as_object() else { continue };
            let met = m
                .get("met")
                .map(as_bool)
                .or_else(|| m.get("pass").map(as_bool))
                .unwrap_or(false);
            let relevant = m.get("relevant").map(as_bool).unwrap_or(true);
            let evidence = as_str(
                m.get("evidence")
                    .or_else(|| m.get("rationale"))
                    .unwrap_or(&Json::String("".into())),
            )
            .trim()
            .to_string();
            let met = if met && evidence.is_empty() {
                ungrounded_criteria = true;
                false
            } else {
                met
            };
            criteria.push(CriterionScore {
                criterion: as_str(
                    m.get("criterion")
                        .or_else(|| m.get("id"))
                        .unwrap_or(&Json::String("".into())),
                ),
                met,
                evidence,
                relevant,
            });
        }
    }

    let score = raw
        .get("score")
        .and_then(|v| if v.is_null() { None } else { as_f64(v) });

    let mut confidence: Option<String> = None;
    if let Some(c) = raw.get("confidence").and_then(|v| v.as_str()) {
        let cl = c.to_lowercase();
        if CONFIDENCES.contains(&cl.as_str()) {
            confidence = Some(cl);
        }
    }

    let mut conversation_feedback: Vec<ConversationFeedback> = Vec::new();
    if let Some(items) = raw.get("conversation_feedback").and_then(|v| v.as_array()) {
        for item in items {
            let Some(m) = item.as_object() else { continue };
            conversation_feedback.push(ConversationFeedback {
                issue: as_str(
                    m.get("issue")
                        .or_else(|| m.get("criterion"))
                        .unwrap_or(&Json::String("".into())),
                ),
                severity: as_str(m.get("severity").unwrap_or(&Json::String("low".into()))),
                agent_line: as_str(
                    m.get("agent_line")
                        .or_else(|| m.get("quote"))
                        .unwrap_or(&Json::String("".into())),
                ),
                why: as_str(
                    m.get("why")
                        .or_else(|| m.get("impact"))
                        .unwrap_or(&Json::String("".into())),
                ),
            });
        }
    }

    let mut issues: Vec<ReviewIssue> = Vec::new();
    if let Some(items) = raw.get("issues").and_then(|v| v.as_array()) {
        for item in items {
            let Some(m) = item.as_object() else { continue };
            issues.push(ReviewIssue {
                title: as_str(
                    m.get("title")
                        .or_else(|| m.get("issue"))
                        .unwrap_or(&Json::String("".into())),
                ),
                severity: as_str(m.get("severity").unwrap_or(&Json::String("Minor".into()))),
                evidence: as_str(
                    m.get("evidence")
                        .or_else(|| m.get("agent_line"))
                        .unwrap_or(&Json::String("".into())),
                ),
                impact: as_str(
                    m.get("impact")
                        .or_else(|| m.get("why"))
                        .unwrap_or(&Json::String("".into())),
                ),
                recommendation: as_str(
                    m.get("recommendation")
                        .or_else(|| m.get("improvement"))
                        .or_else(|| m.get("how_to_improve"))
                        .unwrap_or(&Json::String("".into())),
                ),
            });
        }
    }

    let strengths = {
        let s = str_list(raw.get("strengths").unwrap_or(&Json::Null));
        if !s.is_empty() {
            s
        } else {
            str_list(raw.get("works").unwrap_or(&Json::Null))
        }
    };

    JudgmentResult {
        verdict: verdict_raw,
        score,
        criteria,
        confidence,
        needs_human_review: raw.get("needs_human_review").map(as_bool).unwrap_or(false)
            || ungrounded_criteria,
        critical_failure: raw.get("critical_failure").map(as_bool).unwrap_or(false),
        notes: as_str(
            raw.get("notes")
                .or_else(|| raw.get("reasoning"))
                .unwrap_or(&Json::String("".into())),
        ),
        judge_id: raw.get("judge_id").map(as_str).filter(|s| !s.is_empty()),
        conversation_feedback,
        overall_summary: as_str(
            raw.get("overall_summary")
                .unwrap_or(&Json::String("".into())),
        ),
        strengths,
        issues,
        missing_checks: str_list(raw.get("missing_checks").unwrap_or(&Json::Null)),
        language_naturalness: str_list(raw.get("language_naturalness").unwrap_or(&Json::Null)),
        final_assessment: raw
            .get("final_assessment")
            .and_then(|v| v.as_object())
            .cloned()
            .unwrap_or_default(),
    }
}

/// Relevancy gate (port of `evals/relevancy.py`) — drop irrelevant criteria from
/// the pass/fail math and recompute the verdict.
pub fn apply_relevancy(result: JudgmentResult) -> JudgmentResult {
    if result.verdict == "skipped" || result.verdict == "error" {
        return result;
    }
    if result.criteria.is_empty() {
        return result;
    }
    let relevant: Vec<&CriterionScore> = result.criteria.iter().filter(|c| c.relevant).collect();
    if relevant.is_empty() {
        return JudgmentResult {
            verdict: "maybe".into(),
            confidence: result.confidence.clone().or_else(|| Some("low".into())),
            needs_human_review: true,
            notes: {
                let mut n = result.notes.clone();
                if !n.is_empty() {
                    n.push(' ');
                }
                n.push_str("All criteria marked irrelevant.");
                n
            },
            ..result
        };
    }
    let unmet = relevant.iter().any(|c| !c.met);
    if unmet {
        return JudgmentResult {
            verdict: "fail".into(),
            needs_human_review: result.needs_human_review
                || result.confidence.as_deref() == Some("low"),
            ..result
        };
    }
    // All relevant criteria met — promote fail → pass (irrelevant fails). The
    // promotion is based on the model's own "irrelevant" self-label, so flag
    // for human review instead of trusting it blindly (Python relevancy.py).
    let mut verdict = if result.verdict == "fail" {
        "pass".to_string()
    } else if result.verdict == "pass" || result.verdict == "maybe" {
        result.verdict.clone()
    } else {
        "pass".to_string()
    };
    if !matches!(verdict.as_str(), "pass" | "fail" | "maybe") {
        verdict = "pass".to_string();
    }
    let needs_human_review = if result.verdict == "fail" && verdict == "pass" {
        true
    } else {
        result.needs_human_review
    };
    JudgmentResult {
        verdict,
        needs_human_review,
        ..result
    }
}

/// Multi-judge aggregation (port of `evals/aggregate.py`) — LiveKit
/// JudgeGroup-shaped (all|majority|any).
pub fn verdict_points(verdict: &str) -> f64 {
    match verdict.to_lowercase().as_str() {
        "pass" => 1.0,
        "maybe" => 0.5,
        _ => 0.0,
    }
}

fn flatten_str_list(results: &[Map<String, Json>], key: &str) -> Vec<String> {
    let mut out = Vec::new();
    for r in results {
        if let Some(arr) = r.get(key).and_then(|v| v.as_array()) {
            for item in arr {
                let text = if let Some(obj) = item.as_object() {
                    obj.get("point")
                        .or_else(|| obj.get("item"))
                        .or_else(|| obj.get("issue"))
                        .and_then(|v| v.as_str())
                        .unwrap_or("")
                        .to_string()
                } else {
                    item.as_str().unwrap_or("").to_string()
                };
                if !text.is_empty() {
                    out.push(text);
                }
            }
        }
    }
    out
}

fn flatten_issues(results: &[Map<String, Json>]) -> Vec<Json> {
    let mut out = Vec::new();
    for r in results {
        if let Some(arr) = r.get("issues").and_then(|v| v.as_array()) {
            for item in arr {
                if item.is_object() {
                    out.push(item.clone());
                }
            }
        }
    }
    out
}

/// Aggregate per-judge results (all|majority|any). Returns the combined dict.
pub fn aggregate_judges(results: &[Map<String, Json>], mode: &str) -> Map<String, Json> {
    let mut combined = Map::new();
    if results.is_empty() {
        combined.insert("verdict".into(), json!("skipped"));
        combined.insert("notes".into(), json!("No judges."));
        return combined;
    }
    let verdict_of = |r: &Map<String, Json>| {
        r.get("verdict")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_lowercase()
    };
    let passes: Vec<&Map<String, Json>> =
        results.iter().filter(|r| verdict_of(r) == "pass").collect();
    let fails: Vec<&Map<String, Json>> =
        results.iter().filter(|r| verdict_of(r) == "fail").collect();
    let maybes: Vec<&Map<String, Json>> = results
        .iter()
        .filter(|r| verdict_of(r) == "maybe")
        .collect();
    let errors: Vec<&Map<String, Json>> = results
        .iter()
        .filter(|r| verdict_of(r) == "error")
        .collect();
    let mut n = results.len();
    let mode_l = mode.to_lowercase();

    if !errors.is_empty() && passes.is_empty() && fails.is_empty() && maybes.is_empty() {
        let notes: Vec<String> = errors
            .iter()
            .map(|r| {
                r.get("notes")
                    .and_then(|v| v.as_str())
                    .unwrap_or("error")
                    .to_string()
            })
            .collect();
        combined.insert("verdict".into(), json!("error"));
        combined.insert("score".into(), Json::Null);
        combined.insert("mode".into(), json!(mode_l));
        combined.insert(
            "judges".into(),
            Json::Array(results.iter().cloned().map(Json::Object).collect()),
        );
        combined.insert("passed_count".into(), json!(0));
        combined.insert("failed_count".into(), json!(0));
        combined.insert("maybe_count".into(), json!(0));
        combined.insert("error_count".into(), json!(errors.len()));
        combined.insert("needs_human_review".into(), json!(true));
        combined.insert(
            "notes".into(),
            json!(format!("multi-judge errors: {}", notes.join("; "))
                .chars()
                .take(500)
                .collect::<String>()),
        );
        return combined;
    }

    let (ok, soft) = match mode_l.as_str() {
        "any" => (
            !passes.is_empty(),
            passes.is_empty() && !maybes.is_empty() && fails.is_empty(),
        ),
        "majority" => (
            passes.len() as f64 > n as f64 / 2.0,
            passes.len() as f64 + 0.5 * maybes.len() as f64 > n as f64 / 2.0,
        ),
        _ => {
            // all: errors/skips excluded from scoring
            let scored: Vec<&Map<String, Json>> = results
                .iter()
                .filter(|r| !matches!(verdict_of(r).as_str(), "error" | "skipped"))
                .collect();
            if scored.is_empty() {
                combined.insert("verdict".into(), json!("error"));
                combined.insert("score".into(), Json::Null);
                combined.insert("mode".into(), json!(mode_l));
                combined.insert(
                    "judges".into(),
                    Json::Array(results.iter().cloned().map(Json::Object).collect()),
                );
                combined.insert("needs_human_review".into(), json!(true));
                combined.insert(
                    "notes".into(),
                    json!("multi-judge: no scorable groups (errors/skips only)"),
                );
                return combined;
            }
            n = scored.len();
            let s_pass = scored.iter().filter(|r| verdict_of(r) == "pass").count();
            let s_maybe = scored.iter().filter(|r| verdict_of(r) == "maybe").count();
            (
                scored.iter().all(|r| verdict_of(r) == "pass"),
                s_pass + s_maybe == scored.len(),
            )
        }
    };
    let verdict = if ok {
        "pass"
    } else if soft {
        "maybe"
    } else {
        "fail"
    };

    // Score: mean of numeric scores; else verdict-points × 100.
    let scores: Vec<f64> = results
        .iter()
        .filter_map(|r| r.get("score").and_then(|v| v.as_f64()))
        .collect();
    let avg: f64 = if !scores.is_empty() {
        scores.iter().sum::<f64>() / scores.len() as f64
    } else {
        results
            .iter()
            .map(|r| verdict_points(&verdict_of(r)))
            .sum::<f64>()
            / n as f64
            * 100.0
    };
    let needs_review = results.iter().any(|r| {
        r.get("needs_human_review")
            .and_then(|v| v.as_bool())
            .unwrap_or(false)
    }) || verdict == "maybe";

    combined.insert("verdict".into(), json!(verdict));
    combined.insert("score".into(), json!(avg));
    combined.insert("mode".into(), json!(mode_l));
    combined.insert(
        "judges".into(),
        Json::Array(results.iter().cloned().map(Json::Object).collect()),
    );
    combined.insert("passed_count".into(), json!(passes.len()));
    combined.insert("failed_count".into(), json!(fails.len()));
    combined.insert("maybe_count".into(), json!(maybes.len()));
    combined.insert("needs_human_review".into(), json!(needs_review));
    combined.insert(
        "notes".into(),
        json!(format!(
            "multi-judge mode={mode_l}: {}/{} passed",
            passes.len(),
            n
        )),
    );
    combined.insert(
        "overall_summary".into(),
        json!(results
            .iter()
            .filter_map(|r| r.get("overall_summary").and_then(|v| v.as_str()))
            .collect::<Vec<_>>()
            .join("\n")),
    );
    combined.insert(
        "strengths".into(),
        json!(flatten_str_list(results, "strengths")),
    );
    combined.insert("issues".into(), json!(flatten_issues(results)));
    combined.insert(
        "missing_checks".into(),
        json!(flatten_str_list(results, "missing_checks")),
    );
    combined.insert(
        "language_naturalness".into(),
        json!(flatten_str_list(results, "language_naturalness")),
    );
    combined
}
