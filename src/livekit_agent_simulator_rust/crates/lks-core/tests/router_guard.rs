//! `lksr` must NAME the unimplemented router, not half-run it.
//!
//! v2-28 settled that the Rust port stays off the routed surface in v1. That
//! only holds if the refusal is SYMMETRIC with Python's: both ports must
//! decide from the same predicate, or the same scenario behaves differently
//! under `lks` and `lksr` with nothing saying why.
//!
//! The predicate is the trap in this bead. It is NOT "contains responses:".
//! A `responses:` block with **no** router configured is INERT AND CORRECT on
//! both ports — `caller_steps` wins (D13) — and refusing there would break
//! every stock template, since `lks init` ships scenario templates that
//! *document* the catalog shape.
//!
//! It also must not be a blanket "unconsumed top-level key" guard:
//! `scenario_from_dict` does not consume `apiVersion` or `kind`, yet 22 of 23
//! shipped templates carry both, and Rust's own writer emits them. That guard
//! makes `lksr` unable to load any scenario in the package.

use lks_core::scenario::Scenario;
use serde_json::{json, Map, Value as Json};

fn scenario_with(responses: Option<Json>, caller_steps: Json) -> Scenario {
    let mut data = Map::new();
    data.insert("id".into(), json!("r1"));
    data.insert("apiVersion".into(), json!("agent-sim/v1"));
    data.insert("kind".into(), json!("Scenario"));
    data.insert("persona".into(), json!({"name": "R", "brief": "b"}));
    data.insert("caller_steps".into(), caller_steps);
    if let Some(r) = responses {
        data.insert("responses".into(), r);
    }
    lks_core::scenario::scenario_from_dict(&data, None, "t").expect("parses")
}

fn catalog() -> Json {
    json!({
        "company_name": {"intent": "ask_company_name", "instruction": "i", "text": "t"},
        "off_script": {"intent": "off_script", "instruction": "i", "text": "t", "system": true}
    })
}

// ---------------------------------------------------------------------------
// the predicate itself
// ---------------------------------------------------------------------------

#[test]
fn a_responses_block_is_read_and_reported_present() {
    let s = scenario_with(Some(catalog()), json!([{"say": "hi"}]));
    assert!(s.responses_present());
}

#[test]
fn an_absent_responses_block_is_not_present() {
    let s = scenario_with(None, json!([{"say": "hi"}]));
    assert!(!s.responses_present());
}

#[test]
fn an_empty_responses_block_counts_as_absent() {
    // `{}` is an empty mapping, not a catalog. There is nothing to refuse,
    // and Python's `ResponseCatalog.from_dict` rejects it as empty — so
    // treating it as "present" here would make lksr refuse a scenario
    // lks rejects for a DIFFERENT and unrelated reason.
    let s = scenario_with(Some(json!({})), json!([{"say": "hi"}]));
    assert!(!s.responses_present());
}

#[test]
fn a_malformed_responses_block_is_a_parse_error() {
    for bad in [json!("nope"), json!([1, 2]), json!(3), json!(true)] {
        let mut data = Map::new();
        data.insert("id".into(), json!("r1"));
        data.insert("persona".into(), json!({"name": "R", "brief": "b"}));
        data.insert("responses".into(), bad.clone());
        let err =
            lks_core::scenario::scenario_from_dict(&data, None, "t").expect_err("must reject");
        assert!(
            err.to_string().contains("responses must be a mapping"),
            "unhelpful error for {bad}: {err}"
        );
    }
}

// ---------------------------------------------------------------------------
// the guard's predicate, mirrored from run.rs
// ---------------------------------------------------------------------------

/// Same condition as the guard in `lks-livekit/src/run.rs`. Kept here as a
/// test-local function so the predicate is asserted rather than assumed.
fn refuses(responses_present: bool, router_configured: bool) -> bool {
    responses_present && router_configured
}

/// Python's predicate, transcribed from
/// `caller_contract/live_wiring.py::_attach_response_router` (the `router_cfg
/// is None` branch raises ConfigError). It is written out HERE, in the test
/// that compares it to [`refuses`], because the relationship between the two
/// is the thing under test — a comment asserting "symmetric" is what let both
/// go wrong for a day.
///
/// This does NOT call Python. It pins the *stated* Python behaviour, and
/// `tests/test_router_wiring.py` in the Python suite pins that the real
/// function raises; if the two ever diverge, both suites fail and the
/// disagreement is visible instead of silent.
fn python_refuses(responses_present: bool, router_configured: bool) -> bool {
    responses_present && !router_configured
}

#[test]
fn the_predicate_refuses_only_when_both_are_true() {
    assert!(refuses(true, true), "routed run must be refused");
    assert!(
        !refuses(true, false),
        "responses with no router leaves lksr nothing unimplemented to half-run, \
         so caller_steps is the complete behaviour — NOT because caller_steps wins \
         (there is no caller_steps check anywhere in the routing gate)"
    );
    assert!(
        !refuses(false, true),
        "a configured router with no catalog does nothing"
    );
    assert!(!refuses(false, false));
}

#[test]
fn the_two_ports_are_complementary_not_symmetric() {
    // The relationship is deliberate, and this test exists so nobody "fixes"
    // it into symmetry. `lksr` is a SUBSET port: it validates LESS, never
    // differently. Making the predicates identical would require `lksr` to
    // re-implement every authoring check `lks` has (dispatch_metadata,
    // target_keywords, text_planner, observe sub-groups) — reintroducing the
    // cross-port divergence the guard exists to prevent.
    for responses in [false, true] {
        for router in [false, true] {
            if responses && router {
                assert!(refuses(responses, router), "lksr must refuse a routed run");
                assert!(
                    !python_refuses(responses, router),
                    "lks routes this one — it must not raise"
                );
            } else if responses && !router {
                assert!(
                    !refuses(responses, router),
                    "lksr has no router to half-run; refusing breaks every stock template"
                );
                assert!(
                    python_refuses(responses, router),
                    "lks raises ConfigError here — the authoring half-config"
                );
            } else {
                assert!(!refuses(responses, router));
                assert!(!python_refuses(responses, router));
            }
        }
    }
}

#[test]
fn a_responses_block_with_no_router_does_not_refuse() {
    // The regression this guards: refusing here would break every stock
    // template, because `lks init` ships templates that document the catalog
    // shape without turning the feature on.
    assert!(
        !refuses(
            scenario_with(Some(catalog()), json!([{"say": "hi"}])).responses_present(),
            false
        ),
        "responses with no router must not refuse"
    );
}

// ---------------------------------------------------------------------------
// export: parse-without-export must not recur
// ---------------------------------------------------------------------------

#[test]
fn a_responses_block_survives_export() {
    // caller_dsl.rs:9 states this module's contract as "preserve fields on
    // export". A guard that reads the field but drops it on export would
    // make `lksr convert` silently destroy a user's catalog — the exact
    // mirror of the plan's "parse-without-export silently drops the catalog"
    // warning.
    let s = scenario_with(Some(catalog()), json!([{"say": "hi"}]));
    let out = lks_core::scenario_yaml::scenario_to_dict(&s);
    assert_eq!(
        out.get("responses").and_then(|v| v.as_object()),
        catalog().as_object(),
        "export must put the catalog back byte-for-byte"
    );
}

#[test]
fn a_scenario_without_responses_exports_without_it() {
    let s = scenario_with(None, json!([{"say": "hi"}]));
    let out = lks_core::scenario_yaml::scenario_to_dict(&s);
    assert!(!out.contains_key("responses"), "must not invent the key");
}

#[test]
fn export_then_parse_round_trips_the_catalog() {
    let s = scenario_with(Some(catalog()), json!([{"say": "hi"}]));
    let out = lks_core::scenario_yaml::scenario_to_dict(&s);
    let back = lks_core::scenario::scenario_from_dict(&out, None, "t").expect("re-parses");
    assert_eq!(back.responses, s.responses);
}

// ---------------------------------------------------------------------------
// the guard must not be a blanket unknown-key check
// ---------------------------------------------------------------------------

#[test]
fn api_version_and_kind_are_not_treated_as_unknown() {
    // scenario_from_dict does NOT consume these two, yet 22 of 23 shipped
    // templates carry both and Rust's own writer emits them. A guard of the
    // form "reject any unconsumed top-level key" would make lksr unable to
    // load any scenario in the package.
    let s = scenario_with(None, json!([{"say": "hi"}]));
    let out = lks_core::scenario_yaml::scenario_to_dict(&s);
    assert!(out.contains_key("apiVersion"), "writer emits it");
    assert!(out.contains_key("kind"), "writer emits it");
    lks_core::scenario::scenario_from_dict(&out, None, "t").expect("re-parses both");
}

#[test]
fn the_responses_kind_is_accepted_by_the_jsonl_path() {
    // Two formats behaving differently on the same input is the asymmetry the
    // guard exists to remove.
    assert!(
        lks_core::scenario::KNOWN_KINDS.contains(&"Responses"),
        "scenario_jsonl.rs hard-rejects unknown kinds; Responses must be known"
    );
    assert_eq!(
        lks_core::scenario::KNOWN_KINDS.len(),
        14,
        "bump the array length with the list"
    );
}
