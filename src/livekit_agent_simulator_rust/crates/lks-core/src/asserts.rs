//! Deterministic post-run asserts — byte-parity port of `asserts.py` (core).
//!
//! Deterministic checks run here: tools, tool_order, transcript (regex
//! `must_not_match`), sip, and the deterministic outcome types (recovery,
//! latency, ttfa, turn_taking_audio, agent_must_respond, ended_by, handoff,
//! constraint_respected, backchannel_agent_continued). LLM-dependent outcomes
//! (goals_met, llm_bool) are marked `pending_judge` and excluded from the hard
//! pass; they are resolved by the judge layer (see run.rs merge).

use serde_json::{json, Map, Value as Json};

use crate::metrics::compute_voice_metrics;
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

fn f64_or_none(v: Option<&Json>) -> Option<f64> {
    v.and_then(|v| v.as_f64())
}

fn opt_f64_json(v: Option<f64>) -> Json {
    v.map(Json::from).unwrap_or(Json::Null)
}

/// Transcript texts incl. INTERIM finals (realtime agents often emit only
/// interim) — mirror asserts._transcript_texts. Non-empty, stripped.
fn transcript_texts(events: &[Map<String, Json>], role: &str) -> Vec<String> {
    let mut out = Vec::new();
    for e in events {
        let kind = ev_kind(e);
        if !kind.starts_with("transcript.") {
            continue;
        }
        if !(kind.ends_with(".final") || kind.ends_with(".interim")) {
            continue;
        }
        if role == "agent" && !kind.contains("agent") {
            continue;
        }
        if role == "user" && !kind.contains("user") {
            continue;
        }
        if let Some(t) = ev_spec(e).get("text").map(as_str) {
            let t = t.trim().to_string();
            if !t.is_empty() {
                out.push(t);
            }
        }
    }
    out
}

fn role_texts(events: &[Map<String, Json>], role: &str) -> Vec<String> {
    if role == "agent" || role == "user" {
        transcript_texts(events, role)
    } else {
        let mut t = transcript_texts(events, "agent");
        t.extend(transcript_texts(events, "user"));
        t
    }
}

/// Case-insensitive regex search (Python `re.search(pat, blob, re.I)`).
/// Returns Ok(true/false); Err on an invalid pattern.
fn regex_search(pattern: &str, blob: &str) -> Result<bool, regex::Error> {
    let re = regex::RegexBuilder::new(pattern)
        .case_insensitive(true)
        .build()?;
    Ok(re.is_match(blob))
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
    pub max_turn_p99_ms: Option<i64>,
    pub max_turn_max_ms: Option<i64>,
    pub max_ttfw_ms: Option<i64>,
    pub max_recovery_p50_ms: Option<i64>,
    pub max_recovery_p95_ms: Option<i64>,
    pub min_barge_recovery_rate: Option<f64>,
    pub require_turn_samples: i64,
    pub max_ttfa_p50_ms: Option<i64>,
    pub max_ttfa_p95_ms: Option<i64>,
    pub max_turn_audio_p50_ms: Option<i64>,
    pub max_turn_audio_p95_ms: Option<i64>,
    pub max_turn_audio_p99_ms: Option<i64>,
    pub max_turn_audio_max_ms: Option<i64>,
    pub require_audio_samples: i64,
    pub ended_by: Option<String>,
    pub min_goals: i64,
    pub goals: Vec<String>,
    pub must_not_phrases: Vec<String>,
    pub must_not_match: Option<String>,
    pub check_agent_transcript: bool,
}

impl Default for OutcomeExpect {
    /// Python dataclass field defaults.
    fn default() -> Self {
        Self {
            id: String::new(),
            otype: String::new(),
            phrases: Vec::new(),
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
            max_turn_p99_ms: None,
            max_turn_max_ms: None,
            max_ttfw_ms: None,
            max_recovery_p50_ms: None,
            max_recovery_p95_ms: None,
            min_barge_recovery_rate: None,
            require_turn_samples: 0,
            max_ttfa_p50_ms: None,
            max_ttfa_p95_ms: None,
            max_turn_audio_p50_ms: None,
            max_turn_audio_p95_ms: None,
            max_turn_audio_p99_ms: None,
            max_turn_audio_max_ms: None,
            require_audio_samples: 0,
            ended_by: None,
            min_goals: 0,
            goals: Vec::new(),
            must_not_phrases: Vec::new(),
            must_not_match: None,
            check_agent_transcript: false,
        }
    }
}

/// SipExpect mirror.
#[derive(Debug, Clone, PartialEq)]
pub struct SipExpect {
    pub participant_present: bool,
    pub call_status_any: Vec<String>,
    pub dial_answered: bool,
}

/// AssertSpec mirror.
#[derive(Debug, Clone, PartialEq, Default)]
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
            "max_turn_p99_ms": self.max_turn_p99_ms,
            "max_turn_max_ms": self.max_turn_max_ms,
            "max_ttfw_ms": self.max_ttfw_ms,
            "max_recovery_p50_ms": self.max_recovery_p50_ms,
            "max_recovery_p95_ms": self.max_recovery_p95_ms,
            "min_barge_recovery_rate": self.min_barge_recovery_rate,
            "require_turn_samples": self.require_turn_samples,
            "max_ttfa_p50_ms": self.max_ttfa_p50_ms,
            "max_ttfa_p95_ms": self.max_ttfa_p95_ms,
            "max_turn_audio_p50_ms": self.max_turn_audio_p50_ms,
            "max_turn_audio_p95_ms": self.max_turn_audio_p95_ms,
            "max_turn_audio_p99_ms": self.max_turn_audio_p99_ms,
            "max_turn_audio_max_ms": self.max_turn_audio_max_ms,
            "require_audio_samples": self.require_audio_samples,
            "ended_by": self.ended_by,
            "min_goals": self.min_goals,
            "goals": self.goals,
            "must_not_phrases": self.must_not_phrases,
            "must_not_match": self.must_not_match,
            "check_agent_transcript": self.check_agent_transcript,
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

/// Flatten tool.start spec to a dict blob for args_contains matching
/// (mirror asserts._tool_args_blob: payload.args/arguments/input/params).
fn tool_args_blob(spec: &Map<String, Json>) -> Map<String, Json> {
    if let Some(payload) = spec.get("payload").and_then(|v| v.as_object()) {
        for key in ["args", "arguments", "input", "params"] {
            if let Some(d) = payload.get(key).and_then(|v| v.as_object()) {
                return d.clone();
            }
        }
        return payload.clone();
    }
    for key in ["args", "arguments"] {
        if let Some(d) = spec.get(key).and_then(|v| v.as_object()) {
            return d.clone();
        }
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

/// Port of asserts._eval_tool_order: required tool.start name subsequence.
fn eval_tool_order(order: &[String], tool_starts: &[&Map<String, Json>]) -> Json {
    let mut actual: Vec<String> = Vec::new();
    for e in tool_starts {
        let name = ev_spec(e)
            .get("name")
            .map(as_str)
            .map(|s| s.trim().to_string())
            .unwrap_or_default();
        if !name.is_empty() {
            actual.push(name);
        }
    }
    let mut idx = 0usize;
    let mut matched: Vec<String> = Vec::new();
    for name in &actual {
        if idx < order.len() && name == &order[idx] {
            matched.push(name.clone());
            idx += 1;
        }
    }
    let ok = idx == order.len();
    json!({
        "check": "tool_order",
        "pass": ok,
        "type": "tools",
        "expected_order": order,
        "actual_order": actual,
        "matched_prefix": matched,
        "reason": if ok { Json::Null } else { Json::String(format!("required tools missing or out of order (matched {}/{}))", idx, order.len())) },
    })
}

/// Port of asserts._eval_sip_expect (check names + kinds byte-parity).
fn eval_sip_expect(sip: &SipExpect, events: &[Map<String, Json>]) -> Vec<Json> {
    let mut checks = Vec::new();
    let answered_any = events
        .iter()
        .any(|e| matches!(ev_kind(e), "outbound.dial_answered" | "inbound.answered"));
    if sip.participant_present {
        let present = events
            .iter()
            .any(|e| matches!(ev_kind(e), "sip.participant_connected"))
            || answered_any;
        checks.push(json!({
            "check": "sip_participant_present",
            "pass": present,
            "type": "sip",
            "actual": present,
        }));
    }
    if sip.dial_answered {
        checks.push(json!({
            "check": "sip_dial_answered",
            "pass": answered_any,
            "type": "sip",
            "actual": answered_any,
        }));
    }
    if !sip.call_status_any.is_empty() {
        let mut statuses: Vec<String> = Vec::new();
        for e in events {
            if ev_kind(e) != "sip.call_status" {
                continue;
            }
            let st = ev_spec(e)
                .get("status")
                .or_else(|| ev_spec(e).get("call_status"))
                .map(as_str)
                .unwrap_or_default();
            if !st.is_empty() {
                statuses.push(st);
            }
        }
        // Hairpin paths that skip attribute polling still answered.
        if answered_any {
            statuses.push("active".into());
        }
        let ok = statuses.iter().any(|s| sip.call_status_any.contains(s));
        checks.push(json!({
            "check": "sip_call_status",
            "pass": ok,
            "type": "sip",
            "expected_any": sip.call_status_any,
            "actual": statuses,
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

    let supported_types = [
        "transcript_contains",
        "llm_bool",
        "recovery",
        "latency",
        "ended_by",
        "goals_met",
        "constraint_respected",
        "backchannel_agent_continued",
        "handoff",
        "no_unplanned_handoff",
        "agent_must_respond",
        "ttfa",
        "turn_taking_audio",
    ];

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
            if !supported_types.contains(&otype.as_str()) {
                return Err(format!(
                    "{path_label}: outcomes[{i}].type unsupported: {otype}"
                ));
            }
            let phrases: Vec<String> = match m.get("phrases").or_else(|| m.get("contains_any")) {
                Some(Json::String(s)) => vec![s.clone()],
                Some(Json::Array(a)) => a.iter().map(as_str).collect(),
                _ => Vec::new(),
            };
            let max_ms = m
                .get("max_ms_after_barge_to_agent_final")
                .and_then(|v| v.as_i64());
            if otype == "latency" {
                let has_gate = [
                    "max_turn_p50_ms",
                    "max_turn_p95_ms",
                    "max_turn_p99_ms",
                    "max_turn_max_ms",
                    "max_ttfw_ms",
                    "max_recovery_p50_ms",
                    "max_recovery_p95_ms",
                    "min_barge_recovery_rate",
                ]
                .iter()
                .any(|k| m.get(*k).map(|v| !v.is_null()).unwrap_or(false));
                if !has_gate {
                    return Err(format!(
                        "{path_label}: outcomes[{i}] latency needs at least one threshold \
                         (max_turn_p50_ms / max_turn_p95_ms / max_ttfw_ms / …)"
                    ));
                }
            }
            let eb = if otype == "ended_by" {
                let eb = as_str(
                    m.get("ended_by")
                        .or_else(|| m.get("who"))
                        .unwrap_or(&Json::String("detect".into())),
                );
                if !matches!(eb.as_str(), "sim" | "agent" | "detect") {
                    return Err(format!(
                        "{path_label}: outcomes[{i}] ended_by must be 'sim' | 'agent' | 'detect'"
                    ));
                }
                Some(eb)
            } else {
                m.get("ended_by")
                    .or_else(|| m.get("who"))
                    .and_then(|v| v.as_str())
                    .map(String::from)
            };
            let mut mnp: Vec<String> = match m
                .get("must_not_phrases")
                .or_else(|| m.get("forbidden"))
                .or_else(|| m.get("must_not"))
            {
                Some(Json::String(s)) => vec![s.clone()],
                Some(Json::Array(a)) => a.iter().map(as_str).collect(),
                _ => Vec::new(),
            };
            let mnm = m
                .get("must_not_match")
                .and_then(|v| v.as_str())
                .filter(|s| !s.is_empty())
                .map(String::from);
            if otype == "constraint_respected" {
                // phrases also accepted as must_not list for brevity
                if mnp.is_empty() && !phrases.is_empty() {
                    mnp = phrases.clone();
                }
                if mnp.is_empty() && mnm.is_none() && m.get("prompt").is_none() {
                    return Err(format!(
                        "{path_label}: outcomes[{i}] constraint_respected needs \
                         must_not_phrases / must_not_match and/or prompt (LLM)"
                    ));
                }
            }
            let goals: Vec<String> = match m.get("goals") {
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
                max_turn_p99_ms: m.get("max_turn_p99_ms").and_then(|v| v.as_i64()),
                max_turn_max_ms: m.get("max_turn_max_ms").and_then(|v| v.as_i64()),
                max_ttfw_ms: m.get("max_ttfw_ms").and_then(|v| v.as_i64()),
                max_recovery_p50_ms: m.get("max_recovery_p50_ms").and_then(|v| v.as_i64()),
                max_recovery_p95_ms: m.get("max_recovery_p95_ms").and_then(|v| v.as_i64()),
                min_barge_recovery_rate: m.get("min_barge_recovery_rate").and_then(|v| v.as_f64()),
                require_turn_samples: m
                    .get("require_turn_samples")
                    .and_then(|v| v.as_i64())
                    .unwrap_or(0),
                max_ttfa_p50_ms: m.get("max_ttfa_p50_ms").and_then(|v| v.as_i64()),
                max_ttfa_p95_ms: m.get("max_ttfa_p95_ms").and_then(|v| v.as_i64()),
                max_turn_audio_p50_ms: m.get("max_turn_audio_p50_ms").and_then(|v| v.as_i64()),
                max_turn_audio_p95_ms: m.get("max_turn_audio_p95_ms").and_then(|v| v.as_i64()),
                max_turn_audio_p99_ms: m.get("max_turn_audio_p99_ms").and_then(|v| v.as_i64()),
                max_turn_audio_max_ms: m.get("max_turn_audio_max_ms").and_then(|v| v.as_i64()),
                require_audio_samples: m
                    .get("require_audio_samples")
                    .and_then(|v| v.as_i64())
                    .unwrap_or(0),
                ended_by: eb,
                min_goals: m.get("min_goals").and_then(|v| v.as_i64()).unwrap_or(1),
                goals,
                must_not_phrases: mnp,
                must_not_match: mnm,
                check_agent_transcript: m
                    .get("check_agent_transcript")
                    .and_then(|v| v.as_bool())
                    .unwrap_or(false),
            });
        }
    }

    let sip = spec
        .get("sip")
        .and_then(|v| v.as_object())
        .map(|m| SipExpect {
            participant_present: m
                .get("participant_present")
                .or_else(|| m.get("sip_participant_present"))
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

/// One threshold gate (mirror asserts._eval_latency_outcome._gate):
/// None limit → skip; None actual → fail "no sample"; `val > limit` → fail.
fn gate(
    actual: Option<f64>,
    limit: Option<i64>,
    label: &str,
    ok: &mut bool,
    reasons: &mut Vec<String>,
) {
    let Some(limit) = limit else { return };
    match actual {
        None => {
            *ok = false;
            reasons.push(format!(
                "{label}: no sample (need measured value ≤ {limit}ms)"
            ));
        }
        Some(val) => {
            if val > limit as f64 {
                *ok = false;
                reasons.push(format!("{label} {val:.0}ms > max {limit}ms"));
            }
        }
    }
}

/// Port of asserts._eval_latency_outcome — hard gate on turn_taking / TTFW /
/// recovery percentiles from the event stream.
fn eval_latency_outcome(oc: &OutcomeExpect, events: &[Map<String, Json>]) -> Json {
    let m = compute_voice_metrics(events);
    let tt = m.get("turn_taking_ms").and_then(|v| v.as_object());
    let rec = m.get("recovery_ms").and_then(|v| v.as_object());
    let mut ok = true;
    let mut reasons: Vec<String> = Vec::new();

    let n_turns = tt
        .and_then(|t| t.get("count"))
        .and_then(|v| v.as_i64())
        .unwrap_or(0);
    if oc.require_turn_samples > 0 && n_turns < oc.require_turn_samples {
        ok = false;
        reasons.push(format!(
            "turn samples {n_turns} < require_turn_samples {}",
            oc.require_turn_samples
        ));
    }

    let p = |blk: Option<&Map<String, Json>>, key: &str| blk.and_then(|b| f64_or_none(b.get(key)));
    gate(
        p(tt, "p50"),
        oc.max_turn_p50_ms,
        "turn_p50",
        &mut ok,
        &mut reasons,
    );
    gate(
        p(tt, "p95"),
        oc.max_turn_p95_ms,
        "turn_p95",
        &mut ok,
        &mut reasons,
    );
    gate(
        p(tt, "p99"),
        oc.max_turn_p99_ms,
        "turn_p99",
        &mut ok,
        &mut reasons,
    );
    gate(
        p(tt, "max"),
        oc.max_turn_max_ms,
        "turn_max",
        &mut ok,
        &mut reasons,
    );
    gate(
        f64_or_none(m.get("ttfw_ms")),
        oc.max_ttfw_ms,
        "ttfw",
        &mut ok,
        &mut reasons,
    );
    gate(
        p(rec, "p50"),
        oc.max_recovery_p50_ms,
        "recovery_p50",
        &mut ok,
        &mut reasons,
    );
    gate(
        p(rec, "p95"),
        oc.max_recovery_p95_ms,
        "recovery_p95",
        &mut ok,
        &mut reasons,
    );

    if let Some(min_rate) = oc.min_barge_recovery_rate {
        let rate = f64_or_none(m.get("barge_recovery_rate"));
        let barges = m.get("barge_count").and_then(|v| v.as_i64()).unwrap_or(0);
        if barges == 0 {
            ok = false;
            reasons.push(format!(
                "barge_recovery_rate: no barges fired (need rate >= {min_rate})"
            ));
        } else if rate.map(|r| r < min_rate).unwrap_or(true) {
            ok = false;
            reasons.push(format!(
                "barge_recovery_rate {} < min {min_rate}",
                rate.map(|r| r.to_string()).unwrap_or_else(|| "None".into())
            ));
        }
    }

    json!({
        "check": format!("outcome:{}", oc.id),
        "pass": ok,
        "type": "latency",
        "reasons": reasons,
        "actual": {
            "turn_p50_ms": opt_f64_json(p(tt, "p50")),
            "turn_p95_ms": opt_f64_json(p(tt, "p95")),
            "turn_p99_ms": opt_f64_json(p(tt, "p99")),
            "turn_max_ms": opt_f64_json(p(tt, "max")),
            "turn_count": n_turns,
            "ttfw_ms": m.get("ttfw_ms").cloned().unwrap_or(Json::Null),
            "recovery_p50_ms": opt_f64_json(p(rec, "p50")),
            "recovery_p95_ms": opt_f64_json(p(rec, "p95")),
            "barge_count": m.get("barge_count").cloned().unwrap_or(json!(0)),
            "barge_recovery_rate": m.get("barge_recovery_rate").cloned().unwrap_or(Json::Null),
        },
        "limits": {
            "max_turn_p50_ms": oc.max_turn_p50_ms,
            "max_turn_p95_ms": oc.max_turn_p95_ms,
            "max_turn_p99_ms": oc.max_turn_p99_ms,
            "max_turn_max_ms": oc.max_turn_max_ms,
            "max_ttfw_ms": oc.max_ttfw_ms,
            "max_recovery_p50_ms": oc.max_recovery_p50_ms,
            "max_recovery_p95_ms": oc.max_recovery_p95_ms,
            "min_barge_recovery_rate": oc.min_barge_recovery_rate,
            "require_turn_samples": if oc.require_turn_samples > 0 { Json::from(oc.require_turn_samples) } else { Json::Null },
        },
    })
}

/// Agent audio-onset timestamps (sim.agent.audio_onset), sorted.
fn agent_audio_onset_ms(events: &[Map<String, Json>]) -> Vec<i64> {
    let mut out: Vec<i64> = events
        .iter()
        .filter(|e| ev_kind(e) == "sim.agent.audio_onset")
        .map(ev_mono)
        .collect();
    out.sort();
    out
}

/// Port of asserts._eval_agent_must_respond — PASS iff >= 1 audio onset.
/// NO transcript fallback: text-only agent FAILS.
fn eval_agent_must_respond(oc: &OutcomeExpect, events: &[Map<String, Json>]) -> Json {
    let onsets = agent_audio_onset_ms(events);
    let ok = !onsets.is_empty();
    json!({
        "check": format!("outcome:{}", oc.id),
        "pass": ok,
        "type": "agent_must_respond",
        "agent_audio_onsets": onsets.len(),
        "reason": if ok { Json::Null } else { Json::String("no agent audio observed (no audio onset)".into()) },
    })
}

/// Port of asserts._eval_audio_latency_outcome — gate on audio-onset latency.
/// Missing sample → SKIP (not fail); require_audio_samples short → FAIL.
fn eval_audio_latency_outcome(
    oc: &OutcomeExpect,
    events: &[Map<String, Json>],
    metric: &str,
) -> Json {
    let m = compute_voice_metrics(events);
    let mut ok = true;
    let mut skipped = false;
    let mut reasons: Vec<String> = Vec::new();

    // (label, limit, value) gates — limit None skips the gate.
    type Gate<'a> = (&'a str, Option<i64>, Option<f64>);
    let (counts, actual, gates): (i64, Json, Vec<Gate>) = if metric == "ttfa" {
        let ttfa = f64_or_none(m.get("ttfa_run_ms"));
        let counts = m
            .get("agent_audio_onset_count")
            .and_then(|v| v.as_i64())
            .unwrap_or(0);
        (
            counts,
            m.get("ttfa_run_ms").cloned().unwrap_or(Json::Null),
            vec![
                ("ttfa_p50", oc.max_ttfa_p50_ms, None),
                ("ttfa_p95", oc.max_ttfa_p95_ms, ttfa),
            ],
        )
    } else {
        let blk = m.get("turn_taking_audio_ms").and_then(|v| v.as_object());
        let counts = blk
            .and_then(|b| b.get("count"))
            .and_then(|v| v.as_i64())
            .unwrap_or(0);
        let p = |k: &str| blk.and_then(|b| f64_or_none(b.get(k)));
        (
            counts,
            m.get("turn_taking_audio_ms").cloned().unwrap_or(json!({})),
            vec![
                ("turn_taking_audio_p50", oc.max_turn_audio_p50_ms, p("p50")),
                ("turn_taking_audio_p95", oc.max_turn_audio_p95_ms, p("p95")),
                ("turn_taking_audio_p99", oc.max_turn_audio_p99_ms, p("p99")),
                ("turn_taking_audio_max", oc.max_turn_audio_max_ms, p("max")),
            ],
        )
    };

    if oc.require_audio_samples > 0 && counts < oc.require_audio_samples {
        ok = false;
        reasons.push(format!(
            "audio samples {counts} < require_audio_samples {}",
            oc.require_audio_samples
        ));
    }

    if ok {
        let has_gate = gates.iter().any(|(_, limit, _)| limit.is_some());
        if has_gate && counts == 0 {
            skipped = true;
            reasons.push("insufficient samples (no audio latency sample)".into());
        } else {
            for (label, limit, value) in &gates {
                let Some(limit) = limit else { continue };
                match value {
                    None => {
                        skipped = true;
                        reasons.push(format!("{label}: insufficient samples"));
                    }
                    Some(v) => {
                        if *v > *limit as f64 {
                            ok = false;
                            reasons.push(format!("{label} {v:.0}ms > max {limit}ms"));
                        }
                    }
                }
            }
        }
    }

    json!({
        "check": format!("outcome:{}", oc.id),
        "pass": ok,
        "type": metric,
        "skipped": skipped,
        "reasons": reasons,
        "actual": actual,
        "agent_audio_onset_count": counts,
        "limits": {
            "max_ttfa_p50_ms": oc.max_ttfa_p50_ms,
            "max_ttfa_p95_ms": oc.max_ttfa_p95_ms,
            "max_turn_audio_p50_ms": oc.max_turn_audio_p50_ms,
            "max_turn_audio_p95_ms": oc.max_turn_audio_p95_ms,
            "max_turn_audio_p99_ms": oc.max_turn_audio_p99_ms,
            "max_turn_audio_max_ms": oc.max_turn_audio_max_ms,
            "require_audio_samples": if oc.require_audio_samples > 0 { Json::from(oc.require_audio_samples) } else { Json::Null },
        },
    })
}

/// Port of asserts._eval_ended_by_outcome — expected side: sim | agent | detect.
fn eval_ended_by_outcome(oc: &OutcomeExpect, events: &[Map<String, Json>]) -> Json {
    let sim_hangup = events
        .iter()
        .any(|e| matches!(ev_kind(e), "sim.hang_up" | "sim.script.hang_up"));
    let end_cond: Vec<&Map<String, Json>> = events
        .iter()
        .filter(|e| ev_kind(e) == "run.end_condition")
        .collect();

    let mut who = "detect";
    let mut reason_parts: Vec<String> = Vec::new();

    if sim_hangup {
        who = "sim";
        reason_parts.push("sim_hang_up event (via script)".into());
    } else if let Some(last) = end_cond.last() {
        let er = ev_spec(last).get("reason").map(as_str).unwrap_or_default();
        if er.contains("sim_end_call") {
            who = "sim";
            reason_parts.push(format!("end_reason: {er}"));
        } else if er == "agent_disconnected" || er == "dead_call_silence" {
            who = "agent";
            reason_parts.push(format!("end_reason: {er}"));
        } else if er == "max_turns" || er == "timeout" {
            reason_parts.push(format!("end_reason: {er} (no hang-up side)"));
        }
    } else if events.iter().any(|e| ev_kind(e) == "sim.end_call_token") {
        who = "sim";
        reason_parts.push("sim end_call_token".into());
    }

    let mut ok = true;
    let mut reasons: Vec<String> = Vec::new();
    if let Some(expected) = &oc.ended_by {
        if expected != "detect" && who != expected.as_str() {
            ok = false;
            reasons.push(format!("expected ended_by={expected}, detected={who}"));
        }
    }

    json!({
        "check": format!("outcome:{}", oc.id),
        "pass": ok,
        "type": "ended_by",
        "expected": oc.ended_by,
        "actual": who,
        "reasons": reasons,
        "details": if reason_parts.is_empty() { Json::Null } else { Json::String(reason_parts.join(", ")) },
    })
}

/// Port of asserts._eval_constraint_respected — forbidden material on the
/// CALLER transcript (optional regex; optional agent leak-echo check).
fn eval_constraint_respected(
    oc: &OutcomeExpect,
    events: &[Map<String, Json>],
    pending_llm: &mut Vec<Json>,
) -> Json {
    let mut blobs: Vec<(&str, String)> =
        vec![("user", transcript_texts(events, "user").join("\n"))];
    if oc.check_agent_transcript {
        blobs.push(("agent", transcript_texts(events, "agent").join("\n")));
    }

    let mut hits: Vec<String> = Vec::new();
    for (role, blob) in &blobs {
        let low = blob.to_lowercase();
        for phrase in &oc.must_not_phrases {
            if !phrase.is_empty() && low.contains(&phrase.to_lowercase()) {
                hits.push(format!("{role}:phrase:{phrase}"));
            }
        }
        if let Some(pat) = &oc.must_not_match {
            match regex_search(pat, blob) {
                Ok(true) => hits.push(format!("{role}:regex:{pat}")),
                Ok(false) => {}
                Err(_) => {
                    return json!({
                        "check": format!("outcome:{}", oc.id),
                        "pass": false,
                        "type": "constraint_respected",
                        "must_not_phrases": oc.must_not_phrases,
                        "must_not_match": oc.must_not_match,
                        "violations": [],
                        "reason": format!("invalid regex {pat:?}"),
                    });
                }
            }
        }
    }

    let has_hard = !oc.must_not_phrases.is_empty() || oc.must_not_match.is_some();
    if has_hard {
        let ok = hits.is_empty();
        return json!({
            "check": format!("outcome:{}", oc.id),
            "pass": ok,
            "type": "constraint_respected",
            "must_not_phrases": oc.must_not_phrases,
            "must_not_match": oc.must_not_match,
            "violations": hits,
        });
    }

    // LLM-only constraint: pending judge (soft — not consumed by hard gates).
    pending_llm.push(json!({
        "id": oc.id,
        "prompt": oc.prompt.clone().unwrap_or_else(|| oc.id.clone()),
        "constraint_respected": true,
    }));
    json!({
        "check": format!("outcome:{}", oc.id),
        "pass": true,
        "type": "constraint_respected",
        "pending_judge": true,
        "prompt": oc.prompt,
    })
}

/// Evaluate deterministic asserts (port of asserts.evaluate_asserts).
/// LLM-dependent outcomes (goals_met, llm_bool, prompt-only constraint) are
/// emitted as `pending_judge` checks excluded from the hard `pass`, and listed
/// in `pending_llm_outcomes` for the judge layer (run.rs).
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
        let blob = role_texts(events, role).join("\n");
        let mut ok = true;
        let mut reason: Option<String> = None;
        if !tr.contains_any.is_empty() {
            ok = tr
                .contains_any
                .iter()
                .any(|p| blob.to_lowercase().contains(&p.to_lowercase()));
            if !ok {
                reason = Some(format!(
                    "none of {:?} found in {role} transcript",
                    tr.contains_any
                ));
            }
        }
        if ok {
            if let Some(pat) = &tr.must_not_match {
                match regex_search(pat, &blob) {
                    Ok(true) => {
                        ok = false;
                        reason = Some(format!("matched forbidden pattern {pat:?}"));
                    }
                    Ok(false) => {}
                    Err(_) => {
                        ok = false;
                        reason = Some(format!("invalid regex {pat:?}"));
                    }
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

    // Outcome evaluation (deterministic; LLM types marked pending_judge).
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
    let mut pending_llm: Vec<Json> = Vec::new();

    for oc in &asserts.outcomes {
        match oc.otype.as_str() {
            "transcript_contains" => {
                let blob = role_texts(events, &oc.role).join("\n");
                let matched = !oc.phrases.is_empty()
                    && oc
                        .phrases
                        .iter()
                        .any(|p| blob.to_lowercase().contains(&p.to_lowercase()));
                let ok = if oc.negate { !matched } else { matched };
                let mut c = json!({
                    "check": format!("outcome:{}", oc.id),
                    "pass": ok,
                    "type": oc.otype,
                    "phrases": oc.phrases,
                });
                if oc.negate {
                    c["negate"] = json!(true);
                }
                checks.push(c);
            }
            "recovery" => {
                let first_barge = barge_ms.first().copied();
                let mut after = 0usize;
                if let Some(fb) = first_barge {
                    after = agent_finals.iter().filter(|&&t| t > fb).count();
                }
                let mut ok = after >= oc.min_agent_finals_after_barge_in as usize;
                if oc.min_interruptions > 0 && (interruptions as i64) < oc.min_interruptions {
                    ok = false;
                }
                let mut recovery_ms: Option<i64> = None;
                if ok && oc.max_ms_after_barge_to_agent_final.is_some() {
                    if let Some(fb) = first_barge {
                        match agent_finals.iter().copied().find(|&t| t > fb) {
                            None => ok = false,
                            Some(next) => {
                                recovery_ms = Some(next - fb);
                                ok = recovery_ms.unwrap()
                                    <= oc.max_ms_after_barge_to_agent_final.unwrap_or(i64::MAX);
                            }
                        }
                    } else {
                        ok = false;
                    }
                }
                checks.push(json!({
                    "check": format!("outcome:{}", oc.id),
                    "pass": ok,
                    "type": "recovery",
                    "agent_finals_after_barge_in": after,
                    "expected_min": oc.min_agent_finals_after_barge_in,
                    "interruptions": interruptions,
                    "recovery_ms": recovery_ms,
                    "max_ms_after_barge_to_agent_final": oc.max_ms_after_barge_to_agent_final,
                }));
            }
            "latency" => checks.push(eval_latency_outcome(oc, events)),
            "agent_must_respond" => checks.push(eval_agent_must_respond(oc, events)),
            "ttfa" => checks.push(eval_audio_latency_outcome(oc, events, "ttfa")),
            "turn_taking_audio" => {
                checks.push(eval_audio_latency_outcome(oc, events, "turn_taking_audio"))
            }
            "ended_by" => checks.push(eval_ended_by_outcome(oc, events)),
            "constraint_respected" => {
                checks.push(eval_constraint_respected(oc, events, &mut pending_llm))
            }
            "backchannel_agent_continued" => {
                let bc_cues: Vec<i64> = events
                    .iter()
                    .filter(|e| {
                        ev_kind(e) == "sim.script.cue"
                            && ev_spec(e).get("class").map(as_str).as_deref() == Some("backchannel")
                    })
                    .map(ev_mono)
                    .collect();
                if bc_cues.is_empty() {
                    checks.push(json!({
                        "outcome_id": oc.id,
                        "type": oc.otype,
                        "pass": true,
                        "skipped": true,
                        "reason": "no backchannel cues in run",
                    }));
                } else {
                    let first_bc = bc_cues[0];
                    let agent_after = agent_finals.iter().filter(|&&t| t > first_bc + 100).count();
                    let mut continued = agent_after >= 1;
                    let tool_near = events
                        .iter()
                        .filter(|e| {
                            matches!(ev_kind(e), "tool.start" | "sim.script.cue")
                                && ev_mono(e) >= first_bc - 2000
                                && ev_mono(e) <= first_bc + 5000
                        })
                        .count();
                    if continued && tool_near > 5 {
                        continued = false;
                    }
                    checks.push(json!({
                        "outcome_id": oc.id,
                        "type": oc.otype,
                        "pass": continued,
                        "continued": continued,
                        "agent_finals_after": agent_after,
                    }));
                }
            }
            "handoff" | "no_unplanned_handoff" => {
                let handoffs: Vec<&Map<String, Json>> =
                    events.iter().filter(|e| ev_kind(e) == "handoff").collect();
                let n = handoffs.len();
                if oc.otype == "no_unplanned_handoff" {
                    let ok = n == 0;
                    checks.push(json!({
                        "check": format!("outcome:{}", oc.id),
                        "pass": ok,
                        "type": oc.otype,
                        "handoffs": n,
                        "expected": "none",
                        "reason": if ok { Json::Null } else { Json::String(format!("unexpected handoff(s) occurred: {n}")) },
                    }));
                } else {
                    let ok = n as i64 >= oc.min_handoffs;
                    checks.push(json!({
                        "check": format!("outcome:{}", oc.id),
                        "pass": ok,
                        "type": "handoff",
                        "handoffs": n,
                        "expected_min": oc.min_handoffs,
                        "details": handoffs.iter().map(|e| json!({
                            "old_agent_id": ev_spec(e).get("old_agent_id").cloned().unwrap_or(Json::Null),
                            "new_agent_id": ev_spec(e).get("new_agent_id").cloned().unwrap_or(Json::Null),
                        })).collect::<Vec<_>>(),
                    }));
                }
            }
            "goals_met" => {
                pending_llm.push(json!({
                    "id": oc.id,
                    "prompt": oc.prompt.clone().unwrap_or_else(|| oc.id.clone()),
                    "goals_met": true,
                    "min_goals": oc.min_goals,
                    "goals": oc.goals,
                }));
                checks.push(json!({
                    "check": format!("outcome:{}", oc.id),
                    "pass": true, // deferred to judge layer
                    "type": "goals_met",
                    "pending_judge": true,
                    "min_goals": oc.min_goals,
                    "goals": oc.goals,
                }));
            }
            "llm_bool" => {
                pending_llm.push(json!({
                    "id": oc.id,
                    "prompt": oc.prompt.clone().unwrap_or_else(|| oc.id.clone()),
                }));
                checks.push(json!({
                    "check": format!("outcome:{}", oc.id),
                    "pass": true, // does not fail hard assert; judge layer decides
                    "type": "llm_bool",
                    "pending_judge": true,
                    "prompt": oc.prompt,
                }));
            }
            _ => {
                // Unknown type — parse_assert_spec rejects these; defensive.
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

    let hard = checks
        .iter()
        .filter(|c| {
            !c.get("pending_judge")
                .and_then(|v| v.as_bool())
                .unwrap_or(false)
        })
        .collect::<Vec<_>>();
    let pass = if hard.is_empty() {
        true
    } else {
        hard.iter()
            .all(|c| c.get("pass").and_then(|v| v.as_bool()).unwrap_or(false))
    };

    let mut out = Map::new();
    out.insert("pass".into(), json!(pass));
    out.insert("skipped".into(), json!(false));
    out.insert("checks".into(), Json::Array(checks));
    out.insert("pending_llm_outcomes".into(), Json::Array(pending_llm));
    out
}
