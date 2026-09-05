//! Tests for evals judgment types (parse_judgment_payload + to_dict).
use lks_core::evals::parse_judgment_payload;
use serde_json::{json, Map};

fn as_map(v: serde_json::Value) -> Map<String, serde_json::Value> {
    v.as_object().unwrap().clone()
}

#[test]
fn parse_judgment_valid_payload() {
    let raw = as_map(json!({
        "verdict": "PASS",
        "score": 85.0,
        "confidence": "high",
        "notes": "good",
        "criteria": [
            {"criterion": "responsiveness", "met": true, "evidence": "agent replied"}
        ],
        "strengths": ["clear answers"],
        "issues": [{"title": "slow ttfw", "severity": "Minor"}]
    }));
    let j = parse_judgment_payload(&raw);
    assert_eq!(j.verdict, "pass", "verdict lowercased");
    assert_eq!(j.score, Some(85.0));
    assert_eq!(j.confidence.as_deref(), Some("high"));
    assert_eq!(j.criteria.len(), 1);
    assert!(j.criteria[0].met);
    assert_eq!(j.strengths, vec!["clear answers"]);
    assert_eq!(j.issues.len(), 1);
    assert_eq!(j.issues[0].title, "slow ttfw");
}

#[test]
fn parse_judgment_unknown_verdict_errors() {
    let raw = as_map(json!({"verdict": "bogus"}));
    let j = parse_judgment_payload(&raw);
    assert_eq!(j.verdict, "error");
    assert_eq!(j.score, None);
}

#[test]
fn parse_judgment_missing_verdict_errors() {
    let raw = as_map(json!({}));
    let j = parse_judgment_payload(&raw);
    assert_eq!(j.verdict, "error");
}

#[test]
fn parse_judgment_bad_score_none() {
    let raw = as_map(json!({"verdict": "pass", "score": "not-a-number"}));
    let j = parse_judgment_payload(&raw);
    assert_eq!(j.score, None);
}

#[test]
fn parse_judgment_criteria_pass_alias() {
    // `pass` is accepted as a met alias (grounded by evidence — see #99).
    let raw = as_map(json!({
        "verdict": "pass",
        "criteria": [{"criterion": "c1", "pass": true, "evidence": "quoted line"}]
    }));
    let j = parse_judgment_payload(&raw);
    assert!(j.criteria[0].met, "pass alias → met");
    assert_eq!(j.needs_human_review, false);
}

#[test]
fn parse_judgment_ungrounded_met_not_trusted() {
    // Python test_parse_judgment_ungrounded_met_criterion_is_not_trusted (#99):
    // met=true with NO evidence flips to unmet + flags human review.
    let raw = as_map(json!({
        "verdict": "pass",
        "criteria": [{"criterion": "asked for company name", "pass": true, "evidence": ""}]
    }));
    let j = parse_judgment_payload(&raw);
    assert!(!j.criteria[0].met, "ungrounded met must flip to unmet");
    assert!(j.needs_human_review, "ungrounded claim flags human review");
}

#[test]
fn parse_judgment_strengths_works_fallback() {
    let raw = as_map(json!({"verdict": "pass", "works": ["w1"]}));
    let j = parse_judgment_payload(&raw);
    assert_eq!(j.strengths, vec!["w1"], "works fallback");
}

#[test]
fn parse_judgment_feedback_aliases() {
    let raw = as_map(json!({
        "verdict": "maybe",
        "conversation_feedback": [
            {"issue": "interrupts", "severity": "high", "quote": "agent kept talking", "impact": "frustrating"}
        ]
    }));
    let j = parse_judgment_payload(&raw);
    assert_eq!(j.conversation_feedback.len(), 1);
    let f = &j.conversation_feedback[0];
    assert_eq!(f.issue, "interrupts");
    assert_eq!(f.agent_line, "agent kept talking");
    assert_eq!(f.why, "frustrating");
}

#[test]
fn to_dict_omits_falsy_keys() {
    let raw = as_map(json!({"verdict": "pass", "score": 90.0}));
    let j = parse_judgment_payload(&raw);
    let d = j.to_dict();
    assert_eq!(d["verdict"], json!("pass"));
    assert_eq!(d["score"], json!(90.0));
    // omits falsy keys: needs_human_review / critical_failure only when True
    assert!(!d.contains_key("needs_human_review"));
    assert!(!d.contains_key("critical_failure"));
    assert!(!d.contains_key("strengths"));
}

#[test]
fn to_dict_includes_true_flags() {
    let raw = as_map(json!({
        "verdict": "fail",
        "needs_human_review": true,
        "critical_failure": true,
        "confidence": "low"
    }));
    let j = parse_judgment_payload(&raw);
    let d = j.to_dict();
    assert_eq!(d["needs_human_review"], json!(true));
    assert_eq!(d["critical_failure"], json!(true));
    assert_eq!(d["confidence"], json!("low"));
}

#[test]
fn parse_judgment_review_issue_aliases() {
    let raw = as_map(json!({
        "verdict": "fail",
        "issues": [{
            "title": "t",
            "severity": "Major",
            "evidence": "e",
            "impact": "i",
            "recommendation": "r"
        }]
    }));
    let j = parse_judgment_payload(&raw);
    let i = &j.issues[0];
    assert_eq!(i.title, "t");
    assert_eq!(i.recommendation, "r");
}
