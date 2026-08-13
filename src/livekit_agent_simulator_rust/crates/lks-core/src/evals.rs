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
pub fn parse_judgment_payload(raw: &Map<String, Json>) -> JudgmentResult {
    let mut verdict_raw = as_str(raw.get("verdict").unwrap_or(&Json::String("error".into())))
        .trim()
        .to_lowercase();
    if !VERDICTS.contains(&verdict_raw.as_str()) {
        verdict_raw = "error".to_string();
    }

    let mut criteria: Vec<CriterionScore> = Vec::new();
    if let Some(items) = raw.get("criteria").and_then(|v| v.as_array()) {
        for item in items {
            let Some(m) = item.as_object() else { continue };
            let met = m
                .get("met")
                .map(as_bool)
                .or_else(|| m.get("pass").map(as_bool))
                .unwrap_or(false);
            let relevant = m.get("relevant").map(as_bool).unwrap_or(true);
            criteria.push(CriterionScore {
                criterion: as_str(
                    m.get("criterion")
                        .or_else(|| m.get("id"))
                        .unwrap_or(&Json::String("".into())),
                ),
                met,
                evidence: as_str(
                    m.get("evidence")
                        .or_else(|| m.get("rationale"))
                        .unwrap_or(&Json::String("".into())),
                ),
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
        needs_human_review: raw.get("needs_human_review").map(as_bool).unwrap_or(false),
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
