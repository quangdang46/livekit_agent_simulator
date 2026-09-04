//! Tests for asserts.rs (deterministic tool/transcript/outcome checks).
use lks_core::asserts::{
    evaluate_asserts, AssertSpec, OutcomeExpect, SipExpect, ToolExpect, TranscriptExpect,
};
use serde_json::{json, Map, Value as Json};

fn ev(kind: &str, ts: i64, spec: serde_json::Value) -> Map<String, Json> {
    let mut m = Map::new();
    m.insert("kind".into(), json!(kind));
    m.insert("ts_mono_ms".into(), json!(ts));
    m.insert("spec".into(), spec);
    m
}

fn tool_start(name: &str, ts: i64) -> Map<String, Json> {
    ev("tool.start", ts, json!({"name": name}))
}

fn agent_final(text: &str, ts: i64) -> Map<String, Json> {
    ev("transcript.agent.final", ts, json!({"text": text}))
}

fn user_final(text: &str, ts: i64) -> Map<String, Json> {
    ev("transcript.user.final", ts, json!({"text": text}))
}

fn barge_cue(ts: i64) -> Map<String, Json> {
    ev(
        "sim.script.cue",
        ts,
        json!({"step_id": "b", "barge_in": true, "class": "correction"}),
    )
}

#[test]
fn empty_asserts_skips() {
    let a = AssertSpec {
        tools: vec![],
        transcript: vec![],
        outcomes: vec![],
        sip: None,
        tool_order: vec![],
    };
    let res = evaluate_asserts(&[], &a);
    assert_eq!(res["pass"], json!(true));
    assert_eq!(res["skipped"], json!(true));
}

#[test]
fn tool_assert_count() {
    let events = vec![
        tool_start("search_tools", 100),
        tool_start("search_tools", 200),
    ];
    let a = AssertSpec {
        tools: vec![ToolExpect {
            name: "search_tools".into(),
            min_count: 2,
            max_count: None,
            args_contains: Map::new(),
        }],
        transcript: vec![],
        outcomes: vec![],
        sip: None,
        tool_order: vec![],
    };
    let res = evaluate_asserts(&events, &a);
    assert_eq!(res["pass"], json!(true));
    let checks = res["checks"].as_array().unwrap();
    assert_eq!(checks[0]["actual"], json!(2));
}

#[test]
fn tool_assert_min_fails() {
    let events = vec![tool_start("search", 100)];
    let a = AssertSpec {
        tools: vec![ToolExpect {
            name: "search".into(),
            min_count: 2,
            max_count: None,
            args_contains: Map::new(),
        }],
        transcript: vec![],
        outcomes: vec![],
        sip: None,
        tool_order: vec![],
    };
    let res = evaluate_asserts(&events, &a);
    assert_eq!(res["pass"], json!(false));
}

#[test]
fn tool_order_subsequence() {
    let events = vec![
        tool_start("a", 100),
        tool_start("x", 200),
        tool_start("b", 300),
    ];
    let a = AssertSpec {
        tools: vec![],
        transcript: vec![],
        outcomes: vec![],
        sip: None,
        tool_order: vec!["a".into(), "b".into()],
    };
    let res = evaluate_asserts(&events, &a);
    assert_eq!(res["pass"], json!(true));
}

#[test]
fn tool_order_wrong_sequence_fails() {
    let events = vec![tool_start("b", 100), tool_start("a", 200)];
    let a = AssertSpec {
        tools: vec![],
        transcript: vec![],
        outcomes: vec![],
        sip: None,
        tool_order: vec!["a".into(), "b".into()],
    };
    let res = evaluate_asserts(&events, &a);
    assert_eq!(res["pass"], json!(false));
}

#[test]
fn transcript_contains() {
    let events = vec![
        agent_final("We have the car available", 100),
        user_final("I want the silver one", 200),
    ];
    let a = AssertSpec {
        tools: vec![],
        transcript: vec![TranscriptExpect {
            role: "agent".into(),
            contains_any: vec!["car available".into()],
            must_not_match: None,
        }],
        outcomes: vec![],
        sip: None,
        tool_order: vec![],
    };
    let res = evaluate_asserts(&events, &a);
    assert_eq!(res["pass"], json!(true));
}

#[test]
fn transcript_must_not_match() {
    let events = vec![user_final("Give me my card number 4111", 100)];
    let a = AssertSpec {
        tools: vec![],
        transcript: vec![TranscriptExpect {
            role: "user".into(),
            contains_any: vec![],
            must_not_match: Some("card number".into()),
        }],
        outcomes: vec![],
        sip: None,
        tool_order: vec![],
    };
    let res = evaluate_asserts(&events, &a);
    assert_eq!(res["pass"], json!(false));
}

#[test]
fn outcome_transcript_contains_negate_pass_when_absent() {
    let events = vec![agent_final("Thank you, that's everything.", 100)];
    let a = AssertSpec {
        tools: vec![],
        transcript: vec![],
        outcomes: vec![OutcomeExpect {
            id: "no_retry".into(),
            otype: "transcript_contains".into(),
            phrases: vec!["again, please".into()],
            negate: true,
            prompt: None,
            role: "any".into(),
            min_agent_finals_after_barge_in: 1,
            min_interruptions: 0,
            max_ms_after_barge_to_agent_final: None,
            min_handoffs: 1,
            no_unplanned_handoff: false,
            max_turn_p50_ms: None,
            max_turn_p95_ms: None,
            max_ttfw_ms: None,
            max_recovery_p95_ms: None,
            min_barge_recovery_rate: None,
            ended_by: None,
            min_goals: 0,
            must_not_phrases: vec![],
            must_not_match: None,
        }],
        sip: None,
        tool_order: vec![],
    };
    let res = evaluate_asserts(&events, &a);
    assert_eq!(res["pass"], json!(true), "phrase absent + negate → pass");
}

#[test]
fn outcome_transcript_contains_negate_fail_when_present() {
    let events = vec![agent_final("Could you say that again, please?", 100)];
    let a = AssertSpec {
        tools: vec![],
        transcript: vec![],
        outcomes: vec![OutcomeExpect {
            id: "no_retry".into(),
            otype: "transcript_contains".into(),
            phrases: vec!["again, please".into()],
            negate: true,
            prompt: None,
            role: "any".into(),
            min_agent_finals_after_barge_in: 1,
            min_interruptions: 0,
            max_ms_after_barge_to_agent_final: None,
            min_handoffs: 1,
            no_unplanned_handoff: false,
            max_turn_p50_ms: None,
            max_turn_p95_ms: None,
            max_ttfw_ms: None,
            max_recovery_p95_ms: None,
            min_barge_recovery_rate: None,
            ended_by: None,
            min_goals: 0,
            must_not_phrases: vec![],
            must_not_match: None,
        }],
        sip: None,
        tool_order: vec![],
    };
    let res = evaluate_asserts(&events, &a);
    assert_eq!(res["pass"], json!(false), "phrase present + negate → fail");
}

#[test]
fn recovery_outcome_after_barge() {
    let events = vec![barge_cue(100), agent_final("ok let me check", 500)];
    let a = AssertSpec {
        tools: vec![],
        transcript: vec![],
        outcomes: vec![OutcomeExpect {
            id: "r1".into(),
            otype: "recovery".into(),
            phrases: vec![],
            negate: false,
            prompt: None,
            role: "any".into(),
            min_agent_finals_after_barge_in: 1,
            min_interruptions: 0,
            max_ms_after_barge_to_agent_final: None,
            min_handoffs: 1,
            no_unplanned_handoff: false,
            max_turn_p50_ms: None,
            max_turn_p95_ms: None,
            max_ttfw_ms: None,
            max_recovery_p95_ms: None,
            min_barge_recovery_rate: None,
            ended_by: None,
            min_goals: 0,
            must_not_phrases: vec![],
            must_not_match: None,
        }],
        sip: None,
        tool_order: vec![],
    };
    let res = evaluate_asserts(&events, &a);
    assert_eq!(
        res["pass"],
        json!(true),
        "agent final after barge → recovery ok"
    );
}

#[test]
fn no_unplanned_handoff_assert() {
    let events = vec![ev(
        "handoff",
        100,
        json!({"old_agent_id": "a", "new_agent_id": "b"}),
    )];
    let a = AssertSpec {
        tools: vec![],
        transcript: vec![],
        outcomes: vec![OutcomeExpect {
            id: "h1".into(),
            otype: "no_unplanned_handoff".into(),
            phrases: vec![],
            negate: false,
            prompt: None,
            role: "any".into(),
            min_agent_finals_after_barge_in: 1,
            min_interruptions: 0,
            max_ms_after_barge_to_agent_final: None,
            min_handoffs: 1,
            no_unplanned_handoff: true,
            max_turn_p50_ms: None,
            max_turn_p95_ms: None,
            max_ttfw_ms: None,
            max_recovery_p95_ms: None,
            min_barge_recovery_rate: None,
            ended_by: None,
            min_goals: 0,
            must_not_phrases: vec![],
            must_not_match: None,
        }],
        sip: None,
        tool_order: vec![],
    };
    let res = evaluate_asserts(&events, &a);
    assert_eq!(
        res["pass"],
        json!(false),
        "handoff present → no_unplanned fails"
    );
}

#[test]
fn sip_participant_assert() {
    let events = vec![ev(
        "sip.participant_connected",
        100,
        json!({"identity": "sip-in-1234"}),
    )];
    let a = AssertSpec {
        tools: vec![],
        transcript: vec![],
        outcomes: vec![],
        sip: Some(SipExpect {
            participant_present: true,
            call_status_any: vec![],
            dial_answered: false,
        }),
        tool_order: vec![],
    };
    let res = evaluate_asserts(&events, &a);
    assert_eq!(res["pass"], json!(true));
}
