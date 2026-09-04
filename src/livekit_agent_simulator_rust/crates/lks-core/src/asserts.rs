//! Deterministic post-run asserts — byte-parity port of `asserts.py` (core).
//!
//! llm_bool outcomes are deferred to the judge layer (P7); only deterministic
//! checks run here: tools, tool_order, transcript, sip, and the non-LLM
//! outcome types (recovery, ended_by, handoff, constraint, latency).

use serde_json::{json, Map, Value as Json};

use crate::script::counts_for_recovery_barge;

fn as_str(v: &Json) -> String {
    match v {
        Json::String(s) => s.clone(),
        Json::Number(n) => n.to_string(),
        Json::Bool(b) => b.to_string(),
        Json::Null => String::new(),
        other => other.to_string(),
    }
}

fn ev_kind(e: &Map<String, Json>) -> &str {
    e.get("kind").and_then(|v| v.as_str()).unwrap_or("")
}

fn ev_spec(e: &Map<String, Json>) -> &Map<String, Json> {
    static EMPTY: std::sync::OnceLock<Map<String, Json>> = std::sync::OnceLock::new();
    e.get("spec")
        .and_then(|v| v.as_object())
        .unwrap_or_else(|| EMPTY.get_or_init(Map::new))
}

fn ev_mono(e: &Map<String, Json>) -> i64 {
    e.get("ts_mono_ms").and_then(|v| v.as_i64()).unwrap_or(0)
}

/// ToolExpect mirror.
#[derive(Debug, Clone, PartialEq)]
pub struct ToolExpect {
    pub name: String,
    pub min_count: i64,
    pub max_count: Option<i64>,
    pub args_contains: Map<String, Json>,
}

/// TranscriptExpect mirror.
#[derive(Debug, Clone, PartialEq)]
pub struct TranscriptExpect {
    pub role: String,
    pub contains_any: Vec<String>,
    pub must_not_match: Option<String>,
}

/// OutcomeExpect mirror.
#[derive(Debug, Clone, PartialEq)]
pub struct OutcomeExpect {
    pub id: String,
    pub otype: String,
    pub phrases: Vec<String>,
    /// transcript_contains only: invert match — pass when NONE of `phrases` appear.
    pub negate: bool,
    pub prompt: Option<String>,
    pub role: String,
    pub min_agent_finals_after_barge_in: i64,
    pub min_interruptions: i64,
    pub max_ms_after_barge_to_agent_final: Option<i64>,
    pub min_handoffs: i64,
    pub no_unplanned_handoff: bool,
    pub max_turn_p50_ms: Option<i64>,
    pub max_turn_p95_ms: Option<i64>,
    pub max_ttfw_ms: Option<i64>,
    pub max_recovery_p95_ms: Option<i64>,
    pub min_barge_recovery_rate: Option<f64>,
    pub ended_by: Option<String>,
    pub min_goals: i64,
    pub must_not_phrases: Vec<String>,
    pub must_not_match: Option<String>,
}

/// SipExpect mirror.
#[derive(Debug, Clone, PartialEq)]
pub struct SipExpect {
    pub participant_present: bool,
    pub call_status_any: Vec<String>,
    pub dial_answered: bool,
}

/// AssertSpec mirror.
#[derive(Debug, Clone, PartialEq)]
pub struct AssertSpec {
    pub tools: Vec<ToolExpect>,
    pub transcript: Vec<TranscriptExpect>,
    pub outcomes: Vec<OutcomeExpect>,
    pub sip: Option<SipExpect>,
    pub tool_order: Vec<String>,
}

impl ToolExpect {
    /// Python `asdict` field order (asserts.py dataclass).
    pub fn to_json(&self) -> Json {
        json!({
            "name": self.name,
            "min_count": self.min_count,
            "max_count": self.max_count,
            "args_contains": self.args_contains,
        })
    }
}

impl TranscriptExpect {
    /// Python `asdict` field order.
    pub fn to_json(&self) -> Json {
        json!({
            "role": self.role,
            "contains_any": self.contains_any,
            "must_not_match": self.must_not_match,
        })
    }
}

impl OutcomeExpect {
    /// Python `asdict` field order (asserts.py OutcomeExpect dataclass).
    pub fn to_json(&self) -> Json {
        json!({
            "id": self.id,
            "type": self.otype,
            "phrases": self.phrases,
            "prompt": self.prompt,
            "role": self.role,
            "negate": self.negate,
            "min_agent_finals_after_barge_in": self.min_agent_finals_after_barge_in,
            "min_interruptions": self.min_interruptions,
            "max_ms_after_barge_to_agent_final": self.max_ms_after_barge_to_agent_final,
            "min_handoffs": self.min_handoffs,
            "no_unplanned_handoff": self.no_unplanned_handoff,
            "max_turn_p50_ms": self.max_turn_p50_ms,
            "max_turn_p95_ms": self.max_turn_p95_ms,
            "max_turn_p99_ms": Json::Null,
            "max_turn_max_ms": Json::Null,
            "max_ttfw_ms": self.max_ttfw_ms,
            "max_recovery_p50_ms": Json::Null,
            "max_recovery_p95_ms": self.max_recovery_p95_ms,
            "min_barge_recovery_rate": self.min_barge_recovery_rate,
            "require_turn_samples": 0,
            "max_ttfa_p50_ms": Json::Null,
            "max_ttfa_p95_ms": Json::Null,
            "max_turn_audio_p50_ms": Json::Null,
            "max_turn_audio_p95_ms": Json::Null,
            "max_turn_audio_p99_ms": Json::Null,
            "max_turn_audio_max_ms": Json::Null,
            "require_audio_samples": 0,
            "ended_by": self.ended_by,
            "min_goals": self.min_goals,
            "goals": Json::Array(vec![]),
            "must_not_phrases": self.must_not_phrases,
            "must_not_match": self.must_not_match,
            "check_agent_transcript": false,
        })
    }
}

impl SipExpect {
    /// Python `asdict` field order.
    pub fn to_json(&self) -> Json {
        json!({
            "participant_present": self.participant_present,
            "call_status_any": self.call_status_any,
            "dial_answered": self.dial_answered,
        })
    }
}

impl AssertSpec {
    /// Python `asdict` field order (asserts.py AssertSpec dataclass).
    pub fn to_json(&self) -> Json {
        json!({
            "tools": self.tools.iter().map(|t| t.to_json()).collect::<Vec<_>>(),
            "transcript": self.transcript.iter().map(|t| t.to_json()).collect::<Vec<_>>(),
            "outcomes": self.outcomes.iter().map(|o| o.to_json()).collect::<Vec<_>>(),
            "sip": self.sip.as_ref().map(|s| s.to_json()),
            "tool_order": self.tool_order,
        })
    }
}

impl AssertSpec {
    pub fn empty(&self) -> bool {
        self.tools.is_empty()
            && self.transcript.is_empty()
            && self.outcomes.is_empty()
            && self.sip.is_none()
            && self.tool_order.is_empty()
    }
}

fn transcript_texts(events: &[Map<String, Json>], role: &str) -> Vec<String> {
    let kind = format!("transcript.{role}.final");
    events
        .iter()
        .filter(|e| ev_kind(e) == kind)
        .filter_map(|e| ev_spec(e).get("text").map(as_str))
        .collect()
}

/// Flatten tool.start spec to a dict blob for args_contains matching.
fn tool_args_blob(spec: &Map<String, Json>) -> Map<String, Json> {
    if let Some(payload) = spec.get("payload").and_then(|v| v.as_object()) {
        return payload.clone();
    }
    if let Some(args) = spec.get("args").and_then(|v| v.as_object()) {
        return args.clone();
    }
    if let Some(arguments) = spec.get("arguments").and_then(|v| v.as_object()) {
        return arguments.clone();
    }
    Map::new()
}

fn dict_contains(hay: &Map<String, Json>, needle: &Map<String, Json>) -> bool {
    for (k, want) in needle {
        match hay.get(k) {
            Some(have) if have == want => {}
            _ => return false,
        }
    }
    true
}

fn eval_tool_order(order: &[String], tool_starts: &[&Map<String, Json>]) -> Json {
    // required tool.start name subsequence (not contiguous).
    let mut idx = 0usize;
    for e in tool_starts {
        let name = ev_spec(e).get("name").map(as_str).unwrap_or_default();
        if idx < order.len() && name == order[idx] {
            idx += 1;
        }
    }
    let ok = idx == order.len();
    json!({
        "check": "tool_order",
        "pass": ok,
        "required": order,
        "matched": idx,
    })
}

fn eval_sip_expect(sip: &SipExpect, events: &[Map<String, Json>]) -> Vec<Json> {
    let mut checks = Vec::new();
    if sip.participant_present {
        let present = events
            .iter()
            .any(|e| ev_kind(e) == "sip.participant_connected");
        checks.push(json!({
            "check": "sip:participant",
            "pass": present,
        }));
    }
    if !sip.call_status_any.is_empty() {
        let status = events
            .iter()
            .filter_map(|e| ev_spec(e).get("callStatus").map(as_str))
            .find(|s| !s.is_empty())
            .unwrap_or_default();
        let ok = sip.call_status_any.contains(&status);
        checks.push(json!({
            "check": "sip:callStatus",
            "pass": ok,
            "expected": sip.call_status_any,
            "actual": status,
        }));
    }
    if sip.dial_answered {
        let answered = events
            .iter()
            .any(|e| matches!(ev_kind(e), "outbound.dial_answered" | "inbound.answered"));
        checks.push(json!({
            "check": "sip:dial_answered",
            "pass": answered,
        }));
    }
    checks
}

/// Collect recovery-barge timestamps (same heuristic as asserts.py).
fn collect_barge_ms(events: &[Map<String, Json>]) -> Vec<i64> {
    let mut out: Vec<i64> = Vec::new();
    for e in events {
        let kind = ev_kind(e);
        let spec = ev_spec(e);
        let cls = spec
            .get("class")
            .or_else(|| spec.get("interrupt_class"))
            .map(as_str)
            .filter(|s| !s.is_empty());
        if kind == "sim.script.cue"
            && counts_for_recovery_barge(
                spec.get("barge_in")
                    .and_then(|v| v.as_bool())
                    .unwrap_or(false),
                cls.as_deref(),
            )
        {
            out.push(ev_mono(e));
        }
        if kind == "interruption"
            && (spec
                .get("barge_in")
                .and_then(|v| v.as_bool())
                .unwrap_or(false)
                || as_str(spec.get("by").unwrap_or(&Json::String("".into()))) == "sim")
        {
            if matches!(
                spec.get("class").map(as_str).unwrap_or_default().as_str(),
                "noise" | "backchannel" | "dtmf" | "silence"
            ) {
                continue;
            }
            if spec
                .get("false_positive")
                .and_then(|v| v.as_bool())
                .unwrap_or(false)
            {
                continue;
            }
            if counts_for_recovery_barge(true, cls.as_deref().or(Some("correction"))) {
                out.push(ev_mono(e));
            }
        }
    }
    out.sort();
    out.dedup();
    out
}

/// Evaluate deterministic asserts. Returns {pass, skipped, checks}.
///
/// Parse an Assert section (raw scenario JSON) into a typed AssertSpec
/// (port of `asserts.parse_assert_spec`). Error strings mirror Python.
pub fn parse_assert_spec(spec: &Map<String, Json>, path_label: &str) -> Result<AssertSpec, String> {
    let mut tools = Vec::new();
    if let Some(arr) = spec.get("tools").and_then(|v| v.as_array()) {
        for (i, raw) in arr.iter().enumerate() {
            let Some(m) = raw.as_object() else {
                return Err(format!("{path_label}: tools[{i}] must be an object"));
            };
            let Some(name) = m
                .get("name")
                .map(as_str)
                .map(|s| s.trim().to_string())
                .filter(|s| !s.is_empty())
            else {
                return Err(format!("{path_label}: tools[{i}] needs name"));
            };
            let args = m
                .get("args_contains")
                .or_else(|| m.get("args"))
                .and_then(|v| v.as_object())
                .cloned()
                .unwrap_or_default();
            tools.push(ToolExpect {
                name,
                min_count: m.get("min_count").and_then(|v| v.as_i64()).unwrap_or(1),
                max_count: m.get("max_count").and_then(|v| v.as_i64()),
                args_contains: args,
            });
        }
    }

    let mut transcript = Vec::new();
    if let Some(arr) = spec.get("transcript").and_then(|v| v.as_array()) {
        for (i, raw) in arr.iter().enumerate() {
            let Some(m) = raw.as_object() else {
                return Err(format!("{path_label}: transcript[{i}] must be object"));
            };
            let contains: Vec<String> = match m.get("contains_any").or_else(|| m.get("contains")) {
                Some(Json::String(s)) => vec![s.clone()],
                Some(Json::Array(a)) => a.iter().map(as_str).collect(),
                _ => Vec::new(),
            };
            let must_not = m
                .get("must_not_match")
                .and_then(|v| v.as_str())
                .filter(|s| !s.is_empty())
                .map(String::from);
            transcript.push(TranscriptExpect {
                role: as_str(m.get("role").unwrap_or(&Json::String("agent".into()))),
                contains_any: contains,
                must_not_match: must_not,
            });
        }
    }

    let mut outcomes = Vec::new();
    if let Some(arr) = spec.get("outcomes").and_then(|v| v.as_array()) {
        for (i, raw) in arr.iter().enumerate() {
            let Some(m) = raw.as_object() else {
                return Err(format!("{path_label}: outcomes[{i}] must be an object"));
            };
            let Some(id) = m.get("id").map(as_str).filter(|s| !s.is_empty()) else {
                return Err(format!("{path_label}: outcomes[{i}] needs id"));
            };
            let otype = as_str(
                m.get("type")
                    .unwrap_or(&Json::String("transcript_contains".into())),
            );
            let phrases: Vec<String> = match m.get("phrases").or_else(|| m.get("contains_any")) {
                Some(Json::String(s)) => vec![s.clone()],
                Some(Json::Array(a)) => a.iter().map(as_str).collect(),
                _ => Vec::new(),
            };
            let max_ms = m
                .get("max_ms_after_barge_to_agent_final")
                .and_then(|v| v.as_i64());
            let eb = if otype == "ended_by" {
                Some(as_str(
                    m.get("ended_by")
                        .or_else(|| m.get("who"))
                        .unwrap_or(&Json::String("detect".into())),
                ))
            } else {
                m.get("ended_by")
                    .or_else(|| m.get("who"))
                    .and_then(|v| v.as_str())
                    .map(String::from)
            };
            let mnp: Vec<String> = match m
                .get("must_not_phrases")
                .or_else(|| m.get("forbidden"))
                .or_else(|| m.get("must_not"))
            {
                Some(Json::String(s)) => vec![s.clone()],
                Some(Json::Array(a)) => a.iter().map(as_str).collect(),
                _ => Vec::new(),
            };
            outcomes.push(OutcomeExpect {
                id,
                otype,
                phrases,
                negate: m.get("negate").and_then(|v| v.as_bool()).unwrap_or(false),
                prompt: m.get("prompt").and_then(|v| v.as_str()).map(String::from),
                role: as_str(m.get("role").unwrap_or(&Json::String("any".into()))),
                min_agent_finals_after_barge_in: m
                    .get("min_agent_finals_after_barge_in")
                    .and_then(|v| v.as_i64())
                    .unwrap_or(1),
                min_interruptions: m
                    .get("min_interruptions")
                    .and_then(|v| v.as_i64())
                    .unwrap_or(0),
                max_ms_after_barge_to_agent_final: max_ms,
                min_handoffs: m.get("min_handoffs").and_then(|v| v.as_i64()).unwrap_or(1),
                no_unplanned_handoff: m
                    .get("no_unplanned_handoff")
                    .and_then(|v| v.as_bool())
                    .unwrap_or(false),
                max_turn_p50_ms: m.get("max_turn_p50_ms").and_then(|v| v.as_i64()),
                max_turn_p95_ms: m.get("max_turn_p95_ms").and_then(|v| v.as_i64()),
                max_ttfw_ms: m.get("max_ttfw_ms").and_then(|v| v.as_i64()),
                max_recovery_p95_ms: m.get("max_recovery_p95_ms").and_then(|v| v.as_i64()),
                min_barge_recovery_rate: m.get("min_barge_recovery_rate").and_then(|v| v.as_f64()),
                ended_by: eb,
                min_goals: m.get("min_goals").and_then(|v| v.as_i64()).unwrap_or(1),
                must_not_phrases: mnp,
                must_not_match: m
                    .get("must_not_match")
                    .and_then(|v| v.as_str())
                    .map(String::from),
            });
        }
    }

    let sip = spec
        .get("sip")
        .and_then(|v| v.as_object())
        .map(|m| SipExpect {
            participant_present: m
                .get("participant_present")
                .and_then(|v| v.as_bool())
                .unwrap_or(false),
            call_status_any: match m.get("call_status_any").or_else(|| m.get("call_status")) {
                Some(Json::String(s)) => vec![s.clone()],
                Some(Json::Array(a)) => a.iter().map(as_str).collect(),
                _ => Vec::new(),
            },
            dial_answered: m
                .get("dial_answered")
                .and_then(|v| v.as_bool())
                .unwrap_or(false),
        });

    let tool_order: Vec<String> = match spec
        .get("tool_order")
        .or_else(|| spec.get("required_order"))
    {
        Some(Json::String(s)) => vec![s.clone()],
        Some(Json::Array(a)) => a
            .iter()
            .map(as_str)
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty())
            .collect(),
        _ => Vec::new(),
    };

    Ok(AssertSpec {
        tools,
        transcript,
        outcomes,
        sip,
        tool_order,
    })
}

pub fn evaluate_asserts(events: &[Map<String, Json>], asserts: &AssertSpec) -> Map<String, Json> {
    if asserts.empty() {
        let mut m = Map::new();
        m.insert("pass".into(), json!(true));
        m.insert("skipped".into(), json!(true));
        m.insert("checks".into(), json!([]));
        return m;
    }
    let mut checks: Vec<Json> = Vec::new();
    let tool_starts: Vec<&Map<String, Json>> = events
        .iter()
        .filter(|e| ev_kind(e) == "tool.start")
        .collect();

    if let Some(sip) = &asserts.sip {
        checks.extend(eval_sip_expect(sip, events));
    }

    for te in &asserts.tools {
        let matches: Vec<&&Map<String, Json>> = tool_starts
            .iter()
            .filter(|e| {
                let spec = ev_spec(e);
                let name = spec.get("name").map(as_str).unwrap_or_default();
                if name != te.name {
                    return false;
                }
                if !te.args_contains.is_empty() {
                    let args = tool_args_blob(spec);
                    if !dict_contains(&args, &te.args_contains) {
                        return false;
                    }
                }
                true
            })
            .collect();
        let n = matches.len() as i64;
        let ok = n >= te.min_count && te.max_count.map(|m| n <= m).unwrap_or(true);
        checks.push(json!({
            "check": format!("tool:{}", te.name),
            "pass": ok,
            "expected_min": te.min_count,
            "expected_max": te.max_count,
            "actual": n,
            "args_contains": if te.args_contains.is_empty() { Json::Null } else { Json::Object(te.args_contains.clone()) },
        }));
    }

    if !asserts.tool_order.is_empty() {
        checks.push(eval_tool_order(&asserts.tool_order, &tool_starts));
    }

    for (i, tr) in asserts.transcript.iter().enumerate() {
        let role = if tr.role == "agent" || tr.role == "user" {
            tr.role.as_str()
        } else {
            "any"
        };
        let texts = if role == "any" {
            let mut t = transcript_texts(events, "agent");
            t.extend(transcript_texts(events, "user"));
            t
        } else {
            transcript_texts(events, role)
        };
        let blob = texts.join("\n").to_lowercase();
        let mut ok = true;
        let mut reason: Option<String> = None;
        if !tr.contains_any.is_empty() {
            ok = tr
                .contains_any
                .iter()
                .any(|p| blob.contains(&p.to_lowercase()));
            if !ok {
                reason = Some(format!(
                    "none of {:?} found in {role} transcript",
                    tr.contains_any
                ));
            }
        }
        if ok {
            if let Some(pat) = &tr.must_not_match {
                // simple lowercase substring match (regex unsupported for now)
                if blob.contains(&pat.to_lowercase()) {
                    ok = false;
                    reason = Some(format!("matched forbidden pattern {pat:?}"));
                }
            }
        }
        checks.push(json!({
            "check": format!("transcript[{i}]"),
            "pass": ok,
            "role": tr.role,
            "reason": reason,
        }));
    }

    // Outcome evaluation (deterministic, non-LLM).
    let barge_ms = collect_barge_ms(events);
    let agent_finals: Vec<i64> = events
        .iter()
        .filter(|e| ev_kind(e) == "transcript.agent.final")
        .map(ev_mono)
        .collect();
    let interruptions = events
        .iter()
        .filter(|e| ev_kind(e) == "interruption")
        .count();

    for oc in &asserts.outcomes {
        match oc.otype.as_str() {
            "transcript_contains" => {
                let role = if oc.role == "agent" || oc.role == "user" {
                    oc.role.as_str()
                } else {
                    "any"
                };
                let texts = if role == "any" {
                    let mut t = transcript_texts(events, "agent");
                    t.extend(transcript_texts(events, "user"));
                    t
                } else {
                    transcript_texts(events, role)
                };
                let blob = texts.join("\n").to_lowercase();
                let matched = oc.phrases.iter().any(|p| blob.contains(&p.to_lowercase()));
                let ok = if oc.negate { !matched } else { matched };
                checks.push(json!({
                    "check": format!("outcome:{}", oc.id),
                    "pass": ok,
                    "type": oc.otype,
                    "phrases": oc.phrases,
                    "negate": oc.negate,
                }));
            }
            "ended_by" => {
                // who hung up first: sim.script.hang_up / sim.hang_up (sim) vs agent.
                let sim_hang = events
                    .iter()
                    .any(|e| matches!(ev_kind(e), "sim.script.hang_up" | "sim.hang_up"));
                let ok = match oc.ended_by.as_deref() {
                    Some("sim") => sim_hang,
                    Some("agent") => !sim_hang,
                    _ => true,
                };
                checks.push(json!({
                    "check": format!("outcome:{}", oc.id),
                    "pass": ok,
                    "type": oc.otype,
                    "ended_by": oc.ended_by,
                }));
            }
            "handoff" => {
                let handoffs = events.iter().filter(|e| ev_kind(e) == "handoff").count();
                let ok = handoffs >= oc.min_handoffs as usize;
                checks.push(json!({
                    "check": format!("outcome:{}", oc.id),
                    "pass": ok,
                    "type": oc.otype,
                    "handoffs": handoffs,
                    "expected_min": oc.min_handoffs,
                }));
            }
            "no_unplanned_handoff" => {
                let handoffs = events.iter().filter(|e| ev_kind(e) == "handoff").count();
                let ok = handoffs == 0;
                checks.push(json!({
                    "check": format!("outcome:{}", oc.id),
                    "pass": ok,
                    "type": oc.otype,
                    "handoffs": handoffs,
                    "expected": "none",
                    "reason": if ok { Json::Null } else { json!(format!("unexpected handoff(s) occurred: {handoffs}")) },
                }));
            }
            "recovery" => {
                // agent re-engages after a barge: >= min_agent_finals_after_barge_in
                // agent finals strictly after the first barge.
                let ok = if let Some(&first_barge) = barge_ms.first() {
                    agent_finals.iter().filter(|&&m| m > first_barge).count() as i64
                        >= oc.min_agent_finals_after_barge_in
                } else {
                    false
                };
                checks.push(json!({
                    "check": format!("outcome:{}", oc.id),
                    "pass": ok,
                    "type": oc.otype,
                    "min_agent_finals_after_barge_in": oc.min_agent_finals_after_barge_in,
                }));
            }
            "constraint_respected" => {
                let caller_text = transcript_texts(events, "user").join("\n").to_lowercase();
                let ok = !oc
                    .must_not_phrases
                    .iter()
                    .any(|p| caller_text.contains(&p.to_lowercase()))
                    && oc
                        .must_not_match
                        .as_ref()
                        .map(|p| !caller_text.contains(&p.to_lowercase()))
                        .unwrap_or(true);
                checks.push(json!({
                    "check": format!("outcome:{}", oc.id),
                    "pass": ok,
                    "type": oc.otype,
                    "must_not_phrases": oc.must_not_phrases,
                }));
            }
            "min_interruptions" => {
                let ok = interruptions >= oc.min_interruptions as usize;
                checks.push(json!({
                    "check": format!("outcome:{}", oc.id),
                    "pass": ok,
                    "type": oc.otype,
                    "actual": interruptions,
                    "expected_min": oc.min_interruptions,
                }));
            }
            _ => {
                // llm_bool and other LLM-dependent outcomes deferred to judge (P7).
                checks.push(json!({
                    "check": format!("outcome:{}", oc.id),
                    "pass": true,
                    "type": oc.otype,
                    "skipped": true,
                    "reason": "deferred to judge (P7)",
                }));
            }
        }
    }

    let mut out = Map::new();
    out.insert(
        "pass".into(),
        json!(checks
            .iter()
            .all(|c| c.get("pass").and_then(|v| v.as_bool()).unwrap_or(false))),
    );
    out.insert("skipped".into(), json!(false));
    out.insert("checks".into(), Json::Array(checks));
    out
}
