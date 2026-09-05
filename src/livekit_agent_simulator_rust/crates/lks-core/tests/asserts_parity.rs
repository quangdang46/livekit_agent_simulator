//! Asserts parity — ported from `tests/test_asserts.py` + `tests/test_evals_judge.py`
//! (grounding #99) + judge preset expansion. Deterministic outcomes evaluated
//! from synthetic event streams must match the Python evaluator.

use lks_core::asserts::{evaluate_asserts, parse_assert_spec, AssertSpec, OutcomeExpect};
use serde_json::{json, Value as Json};

fn ev(kind: &str, mono: i64, spec: Json) -> Json {
    json!({"kind": kind, "ts_mono_ms": mono, "spec": spec})
}

fn spec_from(json_spec: Json) -> AssertSpec {
    parse_assert_spec(json_spec.as_object().unwrap(), "Assert").expect("parse ok")
}

fn check_by_type<'a>(res: &'a serde_json::Map<String, Json>, ty: &str) -> &'a Json {
    res["checks"]
        .as_array()
        .unwrap()
        .iter()
        .find(|c| c.get("type").and_then(|v| v.as_str()) == Some(ty))
        .unwrap_or_else(|| panic!("no {ty} check: {res:?}"))
}

// ---------------------------------------------------------------- parse

#[test]
fn parse_latency_outcome() {
    let spec = spec_from(json!({
        "outcomes": [
            {"id": "speed", "type": "latency", "max_turn_p95_ms": 3500,
             "max_ttfw_ms": 5000, "require_turn_samples": 1}
        ]
    }));
    let oc = &spec.outcomes[0];
    assert_eq!(oc.otype, "latency");
    assert_eq!(oc.max_turn_p95_ms, Some(3500));
    assert_eq!(oc.max_ttfw_ms, Some(5000));
    assert_eq!(oc.require_turn_samples, 1);
}

#[test]
fn parse_latency_requires_threshold() {
    let err = parse_assert_spec(
        json!({"outcomes": [{"id": "empty", "type": "latency"}]})
            .as_object()
            .unwrap(),
        "Assert",
    )
    .unwrap_err();
    assert!(err.to_lowercase().contains("threshold") || err.to_lowercase().contains("latency"));
}

#[test]
fn parse_outcome_negate_field() {
    let spec = spec_from(json!({
        "outcomes": [{"id": "no_retry", "type": "transcript_contains", "negate": true,
                      "phrases": ["again, please"]}]
    }));
    assert!(spec.outcomes[0].negate);
}

#[test]
fn parse_ended_by_outcome() {
    let spec = spec_from(json!({
        "outcomes": [
            {"id": "caller_hung", "type": "ended_by", "who": "sim"},
            {"id": "detect_only", "type": "ended_by", "ended_by": "detect"},
        ]
    }));
    assert_eq!(spec.outcomes[0].ended_by.as_deref(), Some("sim"));
    assert_eq!(spec.outcomes[1].ended_by.as_deref(), Some("detect"));
}

#[test]
fn parse_ended_by_invalid() {
    let err = parse_assert_spec(
        json!({"outcomes": [{"id": "x", "type": "ended_by", "ended_by": "robot"}]})
            .as_object()
            .unwrap(),
        "Assert",
    )
    .unwrap_err();
    assert!(err.contains("ended_by"));
}

#[test]
fn parse_outcome_unsupported_type() {
    let err = parse_assert_spec(
        json!({"outcomes": [{"id": "x", "type": "no_such_type"}]})
            .as_object()
            .unwrap(),
        "Assert",
    )
    .unwrap_err();
    assert!(err.contains("unsupported"));
}

// ---------------------------------------------------------------- latency

fn turn_events() -> Vec<Json> {
    vec![
        ev(
            "transcript.agent.final",
            500,
            json!({"text": "hi", "turn_taking_ms": 800}),
        ),
        ev(
            "transcript.agent.final",
            2000,
            json!({"text": "ok", "turn_taking_ms": 1200}),
        ),
    ]
}

#[test]
fn outcome_latency_pass_and_fail() {
    let events: Vec<serde_json::Map<String, Json>> = turn_events()
        .iter()
        .map(|e| e.as_object().unwrap().clone())
        .collect();
    let ok = evaluate_asserts(
        &events,
        &AssertSpec {
            outcomes: vec![OutcomeExpect {
                id: "speed".into(),
                otype: "latency".into(),
                max_turn_p95_ms: Some(2000),
                max_ttfw_ms: Some(1000),
                require_turn_samples: 1,
                ..Default::default()
            }],
            ..Default::default()
        },
    );
    assert!(ok["pass"].as_bool().unwrap(), "{ok:?}");
    let c = check_by_type(&ok, "latency");
    assert!(c["actual"]["turn_p95_ms"].as_f64().is_some());

    let bad = evaluate_asserts(
        &events,
        &AssertSpec {
            outcomes: vec![OutcomeExpect {
                id: "speed".into(),
                otype: "latency".into(),
                max_turn_p95_ms: Some(500), // too tight
                ..Default::default()
            }],
            ..Default::default()
        },
    );
    assert!(!bad["pass"].as_bool().unwrap());
    let c = check_by_type(&bad, "latency");
    let reasons: Vec<String> = c["reasons"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| v.as_str().unwrap_or_default().to_string())
        .collect();
    assert!(
        reasons.iter().any(|r| r.contains("turn_p95")),
        "{reasons:?}"
    );
}

#[test]
fn outcome_latency_barge_recovery_rate() {
    let events: Vec<serde_json::Map<String, Json>> = vec![
        ev("sim.script.cue", 1000, json!({"barge_in": true})),
        ev("transcript.agent.final", 2000, json!({"text": "ok"})),
    ]
    .iter()
    .map(|e| e.as_object().unwrap().clone())
    .collect::<Vec<serde_json::Map<String, Json>>>();
    let assert_spec = || AssertSpec {
        outcomes: vec![OutcomeExpect {
            id: "rec_rate".into(),
            otype: "latency".into(),
            min_barge_recovery_rate: Some(0.9),
            ..Default::default()
        }],
        ..Default::default()
    };
    let ok = evaluate_asserts(&events, &assert_spec());
    assert!(ok["pass"].as_bool().unwrap());

    let no_barge: Vec<serde_json::Map<String, Json>> =
        vec![ev("transcript.agent.final", 100, json!({"text": "x"}))]
            .iter()
            .map(|e| e.as_object().unwrap().clone())
            .collect::<Vec<serde_json::Map<String, Json>>>();
    assert!(!evaluate_asserts(&no_barge, &assert_spec())["pass"]
        .as_bool()
        .unwrap());
}

// ---------------------------------------------------------------- ended_by

#[test]
fn outcome_ended_by_sim_hang() {
    let events = vec![ev(
        "sim.hang_up",
        100,
        json!({"by": "sim", "source": "script"}),
    )]
    .iter()
    .map(|e| e.as_object().unwrap().clone())
    .collect::<Vec<serde_json::Map<String, Json>>>();
    let assert_spec = |who: &str| AssertSpec {
        outcomes: vec![OutcomeExpect {
            id: "h".into(),
            otype: "ended_by".into(),
            ended_by: Some(who.into()),
            ..Default::default()
        }],
        ..Default::default()
    };
    assert!(evaluate_asserts(&events, &assert_spec("sim"))["pass"]
        .as_bool()
        .unwrap());
    assert!(evaluate_asserts(&events, &assert_spec("detect"))["pass"]
        .as_bool()
        .unwrap());
    // agent expectation fails when sim hung up.
    assert!(!evaluate_asserts(&events, &assert_spec("agent"))["pass"]
        .as_bool()
        .unwrap());
}

#[test]
fn outcome_ended_by_agent() {
    let events = vec![ev(
        "run.end_condition",
        100,
        json!({"reason": "agent_disconnected"}),
    )]
    .iter()
    .map(|e| e.as_object().unwrap().clone())
    .collect::<Vec<serde_json::Map<String, Json>>>();
    let assert_spec = |who: &str| AssertSpec {
        outcomes: vec![OutcomeExpect {
            id: "ag".into(),
            otype: "ended_by".into(),
            ended_by: Some(who.into()),
            ..Default::default()
        }],
        ..Default::default()
    };
    assert!(evaluate_asserts(&events, &assert_spec("agent"))["pass"]
        .as_bool()
        .unwrap());
    assert!(!evaluate_asserts(&events, &assert_spec("sim"))["pass"]
        .as_bool()
        .unwrap());
}

// ---------------------------------------------------------------- audio asserts

fn audio_events(agent_onsets: &[i64], user_sources: &[i64]) -> Vec<Json> {
    let mut evs = Vec::new();
    for m in user_sources {
        evs.push(ev("sim.caller.audio_source_start", *m, json!({})));
    }
    for m in agent_onsets {
        evs.push(ev("sim.agent.audio_onset", *m, json!({})));
    }
    evs
}

fn maps(evs: &[Json]) -> Vec<serde_json::Map<String, Json>> {
    evs.iter().map(|e| e.as_object().unwrap().clone()).collect()
}

#[test]
fn agent_must_respond_passes_with_audio_onset() {
    let spec = spec_from(json!({"outcomes": [{"id": "a", "type": "agent_must_respond"}]}));
    let res = evaluate_asserts(&maps(&audio_events(&[1000], &[])), &spec);
    assert!(res["pass"].as_bool().unwrap());
}

#[test]
fn agent_must_respond_fails_without_audio() {
    let spec = spec_from(json!({"outcomes": [{"id": "a", "type": "agent_must_respond"}]}));
    let events = vec![ev("transcript.agent.final", 1000, json!({"text": "hi"}))];
    let res = evaluate_asserts(&maps(&events), &spec);
    assert!(!res["pass"].as_bool().unwrap());
    assert_eq!(res["checks"][0]["type"], "agent_must_respond");
    assert_eq!(res["checks"][0]["agent_audio_onsets"], 0);
}

#[test]
fn ttfa_skips_when_no_sample() {
    let spec =
        spec_from(json!({"outcomes": [{"id": "a", "type": "ttfa", "max_ttfa_p95_ms": 1000}]}));
    let res = evaluate_asserts(&[], &spec);
    assert!(res["pass"].as_bool().unwrap());
    assert_eq!(res["checks"][0]["skipped"], json!(true));
}

#[test]
fn ttfa_fails_when_slow_with_sample() {
    let events = audio_events(&[2000], &[500]);
    let spec =
        spec_from(json!({"outcomes": [{"id": "a", "type": "ttfa", "max_ttfa_p95_ms": 1000}]}));
    let res = evaluate_asserts(&maps(&events), &spec);
    assert!(!res["pass"].as_bool().unwrap());
    assert_eq!(res["checks"][0]["skipped"], json!(false));
}

#[test]
fn ttfa_require_audio_samples_fails_when_short() {
    let spec =
        spec_from(json!({"outcomes": [{"id": "a", "type": "ttfa", "require_audio_samples": 2}]}));
    let res = evaluate_asserts(&maps(&audio_events(&[1000], &[])), &spec);
    assert!(!res["pass"].as_bool().unwrap());
}

#[test]
fn turn_taking_audio_skips_when_no_sample() {
    let spec = spec_from(
        json!({"outcomes": [{"id": "a", "type": "turn_taking_audio", "max_turn_audio_p95_ms": 1000}]}),
    );
    let res = evaluate_asserts(&[], &spec);
    assert!(res["pass"].as_bool().unwrap());
    assert_eq!(res["checks"][0]["skipped"], json!(true));
}

#[test]
fn turn_taking_audio_fails_when_slow() {
    // user source 1000 → agent onset 3000 = 2000ms latency, p95 gate 1000ms.
    let events = audio_events(&[3000], &[1000]);
    let spec = spec_from(
        json!({"outcomes": [{"id": "a", "type": "turn_taking_audio", "max_turn_audio_p95_ms": 1000}]}),
    );
    let res = evaluate_asserts(&maps(&events), &spec);
    assert!(!res["pass"].as_bool().unwrap());
}

#[test]
fn turn_taking_audio_passes_when_fast() {
    let events = audio_events(&[1500], &[1000]);
    let spec = spec_from(
        json!({"outcomes": [{"id": "a", "type": "turn_taking_audio", "max_turn_audio_p95_ms": 1000}]}),
    );
    let res = evaluate_asserts(&maps(&events), &spec);
    assert!(res["pass"].as_bool().unwrap());
}

// ---------------------------------------------------------------- backchannel

#[test]
fn backchannel_agent_continued_pass_and_tool_storm() {
    let bc = |first_bc: i64, extra: Vec<Json>| {
        let mut evs: Vec<Json> = vec![
            ev("sim.script.cue", first_bc, json!({"class": "backchannel"})),
            // agent continued after the backchannel (+100ms guard).
            ev(
                "transcript.agent.final",
                first_bc + 500,
                json!({"text": "sure"}),
            ),
        ];
        evs.extend(extra);
        evs.iter()
            .map(|e| e.as_object().unwrap().clone())
            .collect::<Vec<serde_json::Map<String, Json>>>()
    };
    let spec =
        spec_from(json!({"outcomes": [{"id": "bc", "type": "backchannel_agent_continued"}]}));
    let res = evaluate_asserts(&bc(1000, vec![]), &spec);
    assert!(res["pass"].as_bool().unwrap(), "{res:?}");
    assert_eq!(res["checks"][0]["continued"], json!(true));

    // No backchannel cues → skipped.
    let no_bc = evaluate_asserts(
        &maps(&[ev("transcript.agent.final", 100, json!({"text": "x"}))]),
        &spec,
    );
    assert!(no_bc["pass"].as_bool().unwrap());
    assert_eq!(no_bc["checks"][0]["skipped"], json!(true));

    // Tool storm (>5 tool.start/sim.script.cue near the backchannel) → fail.
    let mut storm = Vec::new();
    for i in 0..7 {
        storm.push(ev("tool.start", 1000 + i, json!({"name": "t"})));
    }
    storm.push(ev("transcript.agent.final", 1500, json!({"text": "sure"})));
    let res = evaluate_asserts(&bc(1000, storm), &spec);
    assert!(!res["pass"].as_bool().unwrap(), "{res:?}");
}

// ---------------------------------------------------------------- transcript regex

#[test]
fn transcript_must_not_match_regex() {
    // (asserts.py uses re.search(pat, blob, re.I) — regex, case-insensitive)
    let events = vec![ev(
        "transcript.agent.final",
        100,
        json!({"text": "Your total is 50 USD"}),
    )];
    let assert_spec = |pat: Option<&str>| AssertSpec {
        transcript: vec![lks_core::asserts::TranscriptExpect {
            role: "agent".into(),
            contains_any: vec![],
            must_not_match: pat.map(String::from),
        }],
        ..Default::default()
    };
    // Forbidden regex (case-insensitive): \b\d+\s*usd\b
    let res = evaluate_asserts(&maps(&events), &assert_spec(Some(r"\b\d+\s*usd\b")));
    assert!(!res["pass"].as_bool().unwrap(), "{res:?}");

    // Pattern that does not match → pass.
    let res = evaluate_asserts(&maps(&events), &assert_spec(Some(r"\bvnd\b")));
    assert!(res["pass"].as_bool().unwrap());

    // Substring patterns no longer match when regex semantics required.
    let res = evaluate_asserts(&maps(&events), &assert_spec(Some(r"\btotal is \d+ USD\b")));
    assert!(!res["pass"].as_bool().unwrap());

    // Invalid regex → check fails with reason (Python would raise; we fail hard).
    let res = evaluate_asserts(&maps(&events), &assert_spec(Some("([bad")));
    assert!(!res["pass"].as_bool().unwrap());
}

// ---------------------------------------------------------------- constraint

#[test]
fn constraint_respected_hard_and_pending() {
    let events = vec![
        ev(
            "transcript.user.final",
            100,
            json!({"text": "my card is 1234 5678"}),
        ),
        ev("transcript.agent.final", 200, json!({"text": "got it"})),
    ];
    let spec = spec_from(json!({
        "outcomes": [{"id": "pci", "type": "constraint_respected",
                      "must_not_match": "\\b\\d{4}[- ]?\\d{4}\\b"}]
    }));
    let res = evaluate_asserts(&maps(&events), &spec);
    assert!(!res["pass"].as_bool().unwrap(), "{res:?}");
    let violations = res["checks"][0]["violations"].as_array().unwrap();
    assert!(violations[0].as_str().unwrap().starts_with("user:regex:"));

    // Agent leak echo (check_agent_transcript=true): agent repeating the card fails too.
    let leak_events = vec![ev(
        "transcript.agent.final",
        200,
        json!({"text": "you said 1234 5678 right?"}),
    )];
    let spec = spec_from(json!({
        "outcomes": [{"id": "pci", "type": "constraint_respected",
                      "must_not_match": "\\b\\d{4}[- ]?\\d{4}\\b",
                      "check_agent_transcript": true}]
    }));
    let res = evaluate_asserts(&maps(&leak_events), &spec);
    assert!(!res["pass"].as_bool().unwrap());
    assert_eq!(
        res["checks"][0]["violations"][0],
        json!("agent:regex:\\b\\d{4}[- ]?\\d{4}\\b")
    );

    // Prompt-only → pending_judge, excluded from hard pass.
    let spec = spec_from(json!({
        "outcomes": [{"id": "soft", "type": "constraint_respected", "prompt": "no swearing"}]
    }));
    let res = evaluate_asserts(&maps(&events), &spec);
    assert!(res["pass"].as_bool().unwrap());
    assert_eq!(res["checks"][0]["pending_judge"], json!(true));
    let pending = res["pending_llm_outcomes"].as_array().unwrap();
    assert_eq!(pending.len(), 1);
    assert_eq!(pending[0]["id"], json!("soft"));
}

// ---------------------------------------------------------------- pending merge

#[test]
fn pending_outcomes_excluded_from_hard_pass() {
    let events = vec![ev("transcript.agent.final", 100, json!({"text": "hello"}))];
    let spec = spec_from(json!({
        "outcomes": [
            {"id": "g", "type": "goals_met", "goals": ["book a table"], "min_goals": 1},
            {"id": "b", "type": "llm_bool", "prompt": "was the agent polite?"},
        ]
    }));
    let res = evaluate_asserts(&maps(&events), &spec);
    // Pending checks never fail the hard pass…
    assert!(res["pass"].as_bool().unwrap(), "{res:?}");
    let checks = res["checks"].as_array().unwrap();
    assert!(checks.iter().all(|c| c.get("pending_judge").is_some()));
    // …and are listed for the judge layer.
    let pending = res["pending_llm_outcomes"].as_array().unwrap();
    assert_eq!(pending.len(), 2);
    assert_eq!(pending[0]["goals_met"], json!(true));
    assert_eq!(pending[0]["goals"], json!(["book a table"]));
    assert_eq!(pending[1]["prompt"], json!("was the agent polite?"));
}

// ---------------------------------------------------------------- presets

#[test]
fn preset_expansion_matches_python() {
    let out = lks_core::presets::expand_criteria(&["builtin:task_completion".into()]).unwrap();
    assert!(out[0].starts_with("Task completion: Did the caller's goal finish"));
    assert_eq!(out.len(), 1);

    // Unknown key → error like Python KeyError.
    let err = lks_core::presets::expand_criteria(&["builtin:does_not_exist".into()]).unwrap_err();
    assert_eq!(err, "Unknown judge builtin preset: does_not_exist");

    // Plain strings pass through.
    let out = lks_core::presets::expand_criteria(&["was the agent polite?".into()]).unwrap();
    assert_eq!(out[0], "was the agent polite?");
}

#[test]
fn preset_judge_group_expansion() {
    let group = json!({
        "id": "natural",
        "builtin": "empathy",
        "criteria": ["builtin:coherence", "extra custom rule"],
    });
    let out = lks_core::presets::expand_judge_group(group.as_object().unwrap()).unwrap();
    let criteria = out["criteria"].as_array().unwrap();
    assert_eq!(criteria.len(), 3);
    assert!(criteria[0]
        .as_str()
        .unwrap()
        .starts_with("Empathy and professionalism"));
    assert!(criteria[1]
        .as_str()
        .unwrap()
        .starts_with("Coherence (LiveKit-style)"));
    assert_eq!(criteria[2], json!("extra custom rule"));

    // Unknown builtin → error.
    let bad = json!({"id": "x", "builtin": "nope"});
    assert!(lks_core::presets::expand_judge_group(bad.as_object().unwrap()).is_err());
}

// ---------------------------------------------------------------- grounding (#99)

#[test]
fn ungrounded_met_flipped_to_unmet() {
    // met=true with empty evidence must NOT count as met (Python fix #99).
    let raw = json!({
        "verdict": "pass",
        "score": 90,
        "criteria": [
            {"criterion": "a", "met": true, "evidence": "quoted line"},
            {"criterion": "b", "met": true, "evidence": ""},
            {"criterion": "c", "met": false, "evidence": ""},
        ]
    });
    let res = lks_core::evals::apply_relevancy(lks_core::evals::parse_judgment_payload(
        raw.as_object().unwrap(),
    ))
    .to_dict();
    assert_eq!(res["verdict"], json!("fail"), "{res:?}");
    assert_eq!(res["needs_human_review"], json!(true));
    let criteria = res["criteria"].as_array().unwrap();
    assert_eq!(criteria[0]["met"], json!(true));
    assert_eq!(criteria[1]["met"], json!(false), "ungrounded met flipped");
    assert_eq!(criteria[2]["met"], json!(false));
}

#[test]
fn fail_to_pass_promotion_flags_human_review() {
    // All relevant criteria met but the model self-labeled "fail" → promoted to
    // pass, but needs_human_review=true (Python relevancy.py promotion).
    let raw = json!({
        "verdict": "fail",
        "score": 60,
        "criteria": [
            {"criterion": "a", "met": true, "evidence": "quoted"},
            {"criterion": "b", "met": true, "evidence": "quoted"},
        ]
    });
    let res = lks_core::evals::apply_relevancy(lks_core::evals::parse_judgment_payload(
        raw.as_object().unwrap(),
    ))
    .to_dict();
    assert_eq!(res["verdict"], json!("pass"));
    assert_eq!(res["needs_human_review"], json!(true));
}
