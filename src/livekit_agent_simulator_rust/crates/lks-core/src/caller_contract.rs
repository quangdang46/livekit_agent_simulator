//! Caller Contract types + deterministic validator — Rust side of the
//! Python/Rust behavioral parity suite (P0-8).
//!
//! Mirrors `src/livekit_agent_simulator/caller_contract/` (Python, the
//! reference implementation — see NEW_ARCHITECTURE_FOR_LKS_AND_LKSR.md
//! §26: Python and Rust are independent codebases sharing only a
//! behavioral contract + golden test vectors, never code).
//!
//! Field names, enum string values, and verdict semantics MUST match the
//! Python side exactly — see `#[cfg(test)] mod parity_tests` below, which
//! reads the SAME JSON fixtures under `tests/fixtures/parity/` (repo
//! root) that the Python `test_parity_vectors.py` reads.

use std::collections::HashMap;

use serde::{Deserialize, Serialize};
use serde_json::Value;

// ---------------------------------------------------------------------
// Shared enums (single source of truth here, mirroring caller_contract/__init__.py)
// ---------------------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum Verdict {
    #[serde(rename = "VALID")]
    Valid,
    #[serde(rename = "INVALID")]
    Invalid,
    #[serde(rename = "UNKNOWN")]
    Unknown,
    #[serde(rename = "ERROR")]
    Error,
}

impl Verdict {
    pub fn as_str(&self) -> &'static str {
        match self {
            Verdict::Valid => "VALID",
            Verdict::Invalid => "INVALID",
            Verdict::Unknown => "UNKNOWN",
            Verdict::Error => "ERROR",
        }
    }

    pub fn is_valid(&self) -> bool {
        matches!(self, Verdict::Valid)
    }
}

/// The seven canonical failure reason codes. Must match Python's
/// `FailureReason` string values exactly.
pub const FAILURE_REASONS: [&str; 7] = [
    "CALLER_BEHAVIOR_VIOLATION",
    "LANGUAGE_GENERATION_ERROR",
    "VALIDATION_ERROR",
    "TTS_ERROR",
    "TRANSPORT_ERROR",
    "AGENT_TIMEOUT",
    "BEHAVIOR_TIMEOUT",
];

/// The six canonical call-end attribution values. Must match Python's
/// `EndedBy` string values exactly.
pub const ENDED_BY: [&str; 6] = [
    "scenario",
    "caller",
    "agent",
    "timeout",
    "transport",
    "error",
];

// ---------------------------------------------------------------------
// GenerationIdentity + is_current (staleness predicate)
// ---------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct GenerationIdentity {
    pub behavior_id: String,
    pub turn_id: i64,
    pub generation_id: i64,
    #[serde(default)]
    pub context_version: i64,
}

/// True iff the (behavior_id, turn_id, generation_id) triple still matches
/// `current` — context_version is deliberately excluded, matching the
/// Python `is_current()` contract exactly.
pub fn is_current(identity: &GenerationIdentity, current: &GenerationIdentity) -> bool {
    identity.behavior_id == current.behavior_id
        && identity.turn_id == current.turn_id
        && identity.generation_id == current.generation_id
}

// ---------------------------------------------------------------------
// ContractConstraints / BehaviorContract
// ---------------------------------------------------------------------

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ContractConstraints {
    #[serde(default = "default_max_turns")]
    pub max_turns: i64,
    pub max_budget: Option<f64>,
    pub max_words: Option<i64>,
    pub max_duration_s: Option<f64>,
    #[serde(default)]
    pub forbidden_intents: Vec<String>,
    #[serde(default)]
    pub must_not: Vec<String>,
}

fn default_max_turns() -> i64 {
    3
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BehaviorContract {
    pub behavior: String,
    pub target: Option<String>,
    pub constraints: ContractConstraints,
}

impl BehaviorContract {
    /// True only when the contract's OWN behavior is explicitly an
    /// end/hangup behavior — mirrors Python's `ends_call_allowed()`
    /// (case-insensitive: `behavior.lower() in {"end", "hangup", "hang_up"}`).
    pub fn ends_call_allowed(&self) -> bool {
        matches!(
            self.behavior.to_lowercase().as_str(),
            "end" | "hangup" | "hang_up"
        )
    }
}

// ---------------------------------------------------------------------
// CandidateUtterance (generator claim, not evidence)
// ---------------------------------------------------------------------

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CandidateUtterance {
    pub act: String,
    pub target: Option<String>,
    #[serde(default)]
    pub slots: HashMap<String, Value>,
    pub utterance: String,
    pub identity: GenerationIdentity,
}

// ---------------------------------------------------------------------
// ObservedAct (semantic verifier evidence, multi-label)
// ---------------------------------------------------------------------

#[derive(Debug, Clone)]
pub struct ObservedAct {
    pub act: String,
    #[allow(dead_code)]
    pub target: Option<String>,
    pub confidence: f64,
    pub all_acts: Vec<String>,
}

// ---------------------------------------------------------------------
// ValidationResult
// ---------------------------------------------------------------------

#[derive(Debug, Clone)]
pub struct ValidationResult {
    pub verdict: Verdict,
    pub reason: Option<String>,
}

impl ValidationResult {
    pub fn is_valid(&self) -> bool {
        self.verdict.is_valid()
    }
}

// ---------------------------------------------------------------------
// Semantic Intent Verification — rule/lexical baseline (mirrors
// caller_contract/semantic.py::RuleBasedSemanticVerifier). Same lexicon,
// same confidence threshold, same multi-label all_acts behavior.
// ---------------------------------------------------------------------

pub const SEMANTIC_CONFIDENCE_THRESHOLD: f64 = 0.5;
const NO_MATCH_CONFIDENCE: f64 = 0.2;

fn default_intent_keywords() -> Vec<(&'static str, &'static [&'static str])> {
    vec![
        (
            "financing",
            &["financing", "finance option", "loan", "payment plan"],
        ),
        ("trade_in", &["trade in", "trade-in", "trade my"]),
        (
            "vehicle_change",
            &[
                "different car",
                "another vehicle",
                "looking for a toyota",
                "instead",
            ],
        ),
    ]
}

fn act_patterns() -> Vec<(&'static str, &'static [&'static str])> {
    vec![
        (
            "ask",
            &[
                "what's",
                "what is",
                "what are",
                "what information",
                "what can",
                "what do",
                "what would",
                "how much",
                "how does",
                "how do",
                "could you tell me",
                "can you tell me",
                "could you let me know",
                "can you let me know",
                "i was wondering",
                "i wanted to ask",
                "i'd like to know",
                "i'd like to ask",
                "i have a question",
                "what are you asking",
                "are you looking for",
                "looking for",
            ],
        ),
        (
            "negotiate",
            &[
                "come down",
                "lower the price",
                "any room on",
                "closer to",
                "could you do",
                "would you consider",
                "meet me at",
                "my limit",
                "my budget",
            ],
        ),
        (
            "confirm",
            &["so it's", "just to confirm", "is that right", "to confirm"],
        ),
        (
            "deny",
            &["no thanks", "i don't think so", "that won't work"],
        ),
        (
            "accept",
            &[
                "sounds good",
                "that works",
                "i'll take it",
                "works for me",
                "okay, that's",
            ],
        ),
        ("reject", &["not interested", "no, i'd rather", "i'll pass"]),
        ("provide", &["i'm calling about", "i want", "i'd like to"]),
        (
            "arrange_visit",
            &["come by", "hold it until", "schedule a time", "book a time"],
        ),
        (
            "end",
            &["goodbye", "bye", "thanks, that's all", "have a good day"],
        ),
    ]
}

fn target_keywords() -> Vec<(&'static str, &'static [&'static str])> {
    vec![
        (
            "price",
            &[
                "price",
                "$",
                "cost",
                "fee",
                "charge",
                "quote",
                "budget",
                "lower the price",
                "monthly fee",
                "how much",
            ],
        ),
        (
            "delivery_date",
            &[
                "deliver",
                "delivery",
                "arrival",
                "arrive",
                "shipping",
                "ship",
                "eta",
                "when will",
                "how long",
            ],
        ),
        (
            "order_status",
            &[
                "order", "status", "delayed", "delay", "shipment", "tracking",
            ],
        ),
        ("hours", &["hours", "open", "close", "opening", "what time"]),
        ("plan", &["plan", "package", "subscription"]),
        ("status", &["status", "update", "checking on"]),
        ("charge", &["charge", "bill", "billing", "fee"]),
        ("fees", &["fee", "fees", "hidden", "cost", "charge"]),
    ]
}

/// Independent target evidence from the utterance text alone (mirrors
/// semantic.py::_target_evidence). Unknown targets yield None at this tier.
fn target_evidence(utterance: &str, target: &Option<String>) -> Option<String> {
    let target = target.as_ref()?;
    let keywords = target_keywords()
        .into_iter()
        .find(|(name, _)| name == target)
        .map(|(_, kws)| kws)?;
    let lowered = utterance.to_lowercase();
    if keywords.iter().any(|kw| lowered.contains(kw)) {
        Some(target.clone())
    } else {
        None
    }
}

fn confidence_for(hit_count: usize) -> f64 {
    match hit_count {
        0 => NO_MATCH_CONFIDENCE,
        1 => 0.75,
        2 => 0.85,
        _ => 0.92,
    }
}

pub struct RuleBasedSemanticVerifier;

impl RuleBasedSemanticVerifier {
    pub fn classify(&self, utterance: &str, contract: &BehaviorContract) -> ObservedAct {
        let lowered = utterance.to_lowercase();

        let mut hits: HashMap<&str, usize> = HashMap::new();
        for (act, patterns) in act_patterns() {
            let count = patterns.iter().filter(|p| lowered.contains(**p)).count();
            if count > 0 {
                hits.insert(act, count);
            }
        }

        let detected_intent_tags: Vec<String> = default_intent_keywords()
            .into_iter()
            .filter(|(_, keywords)| keywords.iter().any(|kw| lowered.contains(kw)))
            .map(|(intent, _)| intent.to_string())
            .collect();

        if hits.is_empty() {
            let mut all_acts = detected_intent_tags.clone();
            if all_acts.is_empty() {
                all_acts.push(contract.behavior.clone());
            }
            return ObservedAct {
                act: contract.behavior.clone(),
                target: target_evidence(utterance, &contract.target),
                confidence: NO_MATCH_CONFIDENCE,
                all_acts,
            };
        }

        // Prefer contract.behavior on ties; full key-ties resolve to the
        // first act in ACT_PATTERNS order (mirrors Python's max() over the
        // overall_hits dict, whose insertion follows ACT_PATTERNS order).
        // Never max_by_key over the HashMap — its order is random.
        let mut best_act = String::new();
        let mut best_key: (usize, bool) = (0, false);
        let mut first = true;
        for (act, _) in act_patterns() {
            if let Some(count) = hits.get(act) {
                let key = (*count, act == contract.behavior.as_str());
                if first || key > best_key {
                    best_key = key;
                    best_act = act.to_string();
                    first = false;
                }
            }
        }
        let primary_hits = hits[best_act.as_str()];

        // Deterministic all_acts order (mirrors Python: overall_hits dict
        // insertion follows ACT_PATTERNS iteration order, then intent tags
        // in DEFAULT_INTENT_KEYWORDS order). Never iterate the HashMap
        // directly — its order is random per process.
        let mut all_acts: Vec<String> = vec![best_act.clone()];
        for (act, _) in act_patterns() {
            if hits.contains_key(act) && !all_acts.contains(&act.to_string()) {
                all_acts.push(act.to_string());
            }
        }
        for tag in detected_intent_tags {
            if !all_acts.contains(&tag) {
                all_acts.push(tag);
            }
        }

        let target = target_evidence(utterance, &contract.target);

        ObservedAct {
            act: best_act,
            target,
            confidence: confidence_for(primary_hits),
            all_acts,
        }
    }
}

// ---------------------------------------------------------------------
// ContractValidator — deterministic pipeline (mirrors
// caller_contract/validator.py::ContractValidator). Same check order,
// same reason strings.
// ---------------------------------------------------------------------

const END_CALL_PHRASES: [&str; 6] = [
    "goodbye",
    "bye",
    "thanks, that's all",
    "that's all i needed",
    "i'll let you go",
    "have a good day",
];

fn word_count(text: &str) -> usize {
    text.split_whitespace().filter(|w| !w.is_empty()).count()
}

/// float() coercion for slot values (mirrors validator.py's
/// `float(claimed_budget)`): JSON numbers, numeric strings, and bools
/// (True=1.0) coerce; anything else (null, objects, non-numeric strings)
/// is ignored — never a violation by itself. A failed coercion in Python
/// raises ValueError out of validate(); here it is treated as absent
/// (the candidate shape gate already passed) so replay never diverges
/// on a value Python would have rejected at `candidate.validate()`.
fn slot_as_f64(value: Option<&Value>) -> Option<f64> {
    match value {
        Some(Value::Number(n)) => n.as_f64(),
        Some(Value::String(s)) => s.trim().parse::<f64>().ok(),
        Some(Value::Bool(b)) => Some(if *b { 1.0 } else { 0.0 }),
        _ => None,
    }
}

fn lexical_forbidden_intent_hit(utterance: &str, forbidden_intents: &[String]) -> Option<String> {
    let lowered = utterance.to_lowercase();
    let lexicon = default_intent_keywords();
    for intent in forbidden_intents {
        let keywords: Vec<&str> = lexicon
            .iter()
            .find(|(name, _)| *name == intent)
            .map(|(_, kws)| kws.to_vec())
            .unwrap_or_else(|| vec![]);
        let hit = if keywords.is_empty() {
            lowered.contains(&intent.replace('_', " "))
        } else {
            keywords.iter().any(|kw| lowered.contains(kw))
        };
        if hit {
            return Some(intent.clone());
        }
    }
    None
}

pub struct ContractValidator {
    semantic_verifier: Option<RuleBasedSemanticVerifier>,
}

impl ContractValidator {
    pub fn new(semantic_verifier: Option<RuleBasedSemanticVerifier>) -> Self {
        Self { semantic_verifier }
    }

    pub fn validate(
        &self,
        candidate: &CandidateUtterance,
        contract: &BehaviorContract,
    ) -> ValidationResult {
        // 1. Schema (mirrors validator.py step 1: candidate.validate() +
        // contract.validate() — malformed shape is INVALID with a
        // "SCHEMA_INVALID: <detail>" reason, not ERROR).
        if candidate.act.trim().is_empty() {
            return ValidationResult {
                verdict: Verdict::Invalid,
                reason: Some("SCHEMA_INVALID: act must be a non-empty string".to_string()),
            };
        }
        if candidate.utterance.trim().is_empty() {
            return ValidationResult {
                verdict: Verdict::Invalid,
                reason: Some("SCHEMA_INVALID: utterance must be a non-empty string".to_string()),
            };
        }
        if contract.behavior.trim().is_empty() {
            return ValidationResult {
                verdict: Verdict::Invalid,
                reason: Some("SCHEMA_INVALID: behavior must be a non-empty string".to_string()),
            };
        }
        if contract.constraints.max_turns < 1 {
            return ValidationResult {
                verdict: Verdict::Invalid,
                reason: Some("SCHEMA_INVALID: max_turns must be >= 1".to_string()),
            };
        }
        if matches!(contract.constraints.max_words, Some(w) if w < 1) {
            return ValidationResult {
                verdict: Verdict::Invalid,
                reason: Some("SCHEMA_INVALID: max_words must be >= 1".to_string()),
            };
        }
        if matches!(contract.constraints.max_duration_s, Some(d) if d <= 0.0) {
            return ValidationResult {
                verdict: Verdict::Invalid,
                reason: Some("SCHEMA_INVALID: max_duration_s must be > 0".to_string()),
            };
        }

        // 2. Act.
        if candidate.act != contract.behavior {
            return ValidationResult {
                verdict: Verdict::Invalid,
                reason: Some("ACT_MISMATCH".to_string()),
            };
        }

        // 3. Target claim shape — candidate.target is a generator claim,
        // NEVER evidence (mirrors validator.py step 3). The authoritative
        // target verdict comes from observed.target below; no claim-equality
        // check here by design.

        // 4. Slot / numeric constraints (mirrors validator.py step 4:
        // float() coercion — numeric strings and ints count, non-numeric
        // values are ignored, never a violation by themselves).
        if let Some(max_budget) = contract.constraints.max_budget {
            if let Some(claimed) = slot_as_f64(candidate.slots.get("max_budget")) {
                if claimed > max_budget {
                    return ValidationResult {
                        verdict: Verdict::Invalid,
                        reason: Some("SLOT_VIOLATION".to_string()),
                    };
                }
            }
        }

        // 5. Utterance size.
        if let Some(max_words) = contract.constraints.max_words {
            if word_count(&candidate.utterance) as i64 > max_words {
                return ValidationResult {
                    verdict: Verdict::Invalid,
                    reason: Some("UTTERANCE_TOO_LONG".to_string()),
                };
            }
        }

        // 6. Goodbye / end-call guard.
        let must_not_end = contract
            .constraints
            .must_not
            .iter()
            .any(|m| m == "end_call")
            || !contract.ends_call_allowed();
        if must_not_end && !contract.ends_call_allowed() {
            let lowered = candidate.utterance.to_lowercase();
            if END_CALL_PHRASES.iter().any(|p| lowered.contains(p)) {
                return ValidationResult {
                    verdict: Verdict::Invalid,
                    reason: Some("END_CALL_NOT_ALLOWED".to_string()),
                };
            }
        }

        // 7. Forbidden intents — lexical baseline (always runs).
        if let Some(_hit) = lexical_forbidden_intent_hit(
            &candidate.utterance,
            &contract.constraints.forbidden_intents,
        ) {
            return ValidationResult {
                verdict: Verdict::Invalid,
                reason: Some("FORBIDDEN_INTENT_DETECTED".to_string()),
            };
        }

        // 8. Optional semantic verifier.
        if let Some(verifier) = &self.semantic_verifier {
            let observed = verifier.classify(&candidate.utterance, contract);

            if observed.confidence < SEMANTIC_CONFIDENCE_THRESHOLD {
                return ValidationResult {
                    verdict: Verdict::Unknown,
                    reason: Some("LOW_CONFIDENCE".to_string()),
                };
            }

            for act in &observed.all_acts {
                if act != &contract.behavior && contract.constraints.forbidden_intents.contains(act)
                {
                    return ValidationResult {
                        verdict: Verdict::Invalid,
                        reason: Some("FORBIDDEN_INTENT_DETECTED".to_string()),
                    };
                }
            }

            if observed.act != contract.behavior {
                return ValidationResult {
                    verdict: Verdict::Invalid,
                    reason: Some("SEMANTIC_ACT_MISMATCH".to_string()),
                };
            }

            // Target evidence (authoritative): observed.target is the ONLY
            // target signal. None + pinned contract target -> UNKNOWN
            // (TARGET_UNVERIFIED); disagreement -> SEMANTIC_TARGET_MISMATCH.
            if contract.target.is_some() {
                match &observed.target {
                    None => {
                        return ValidationResult {
                            verdict: Verdict::Unknown,
                            reason: Some("TARGET_UNVERIFIED".to_string()),
                        };
                    }
                    Some(observed_target) => {
                        if Some(observed_target) != contract.target.as_ref() {
                            return ValidationResult {
                                verdict: Verdict::Invalid,
                                reason: Some("SEMANTIC_TARGET_MISMATCH".to_string()),
                            };
                        }
                    }
                }
            }
        }

        ValidationResult {
            verdict: Verdict::Valid,
            reason: None,
        }
    }
}

// ---------------------------------------------------------------------
// Orchestrator pure logic (mirrors orchestrator.py: BehaviorEvaluator,
// classify_agent_silence, TurnDetector, Orchestrator identity/timeout
// counters). Deterministic, no I/O — covered by orchestrator_evaluator.json.
// ---------------------------------------------------------------------

/// Agent-side verdict: did the AGENT satisfy the current behavior? Kept
/// intentionally simple/deterministic — mirrors Python's BehaviorEvaluator
/// exactly, including exact-substring semantics (e.g. "yes, that's fine"
/// SATISFIES but "yes, that is fine" does NOT).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EvaluatorVerdict {
    Satisfied,
    Partial,
    NotSatisfied,
}

impl EvaluatorVerdict {
    pub fn as_str(&self) -> &'static str {
        match self {
            EvaluatorVerdict::Satisfied => "SATISFIED",
            EvaluatorVerdict::Partial => "PARTIAL",
            EvaluatorVerdict::NotSatisfied => "NOT_SATISFIED",
        }
    }
}

const EVALUATOR_SATISFIED_PATTERNS: [&str; 9] = [
    "sure",
    "works for me",
    "works perfectly",
    "that works",
    "okay, that's",
    "yes, that's fine",
    "sounds good",
    "i can do",
    "we can do",
];

const EVALUATOR_PARTIAL_PATTERNS: [&str; 6] = [
    "probably",
    "might work",
    "let me check",
    "i'll try",
    "possibly",
    "not sure",
];

pub fn evaluate_behavior(contract_target: Option<&str>, agent_text: &str) -> EvaluatorVerdict {
    let text = agent_text.to_lowercase();
    if EVALUATOR_SATISFIED_PATTERNS
        .iter()
        .any(|p| text.contains(p))
    {
        return EvaluatorVerdict::Satisfied;
    }
    if EVALUATOR_PARTIAL_PATTERNS.iter().any(|p| text.contains(p)) {
        return EvaluatorVerdict::Partial;
    }
    // A bare price quote satisfies an `ask`/`negotiate` price behavior.
    // Mirrors Python's `re.search(r"\$\s?\d", text)`: '$', one optional
    // whitespace char, then a digit — NOT merely "$ somewhere + digit
    // somewhere" (e.g. "$ ... 3" far apart must NOT satisfy).
    if contract_target == Some("price") && price_quote_hit(&text) {
        return EvaluatorVerdict::Satisfied;
    }
    EvaluatorVerdict::NotSatisfied
}

fn price_quote_hit(text: &str) -> bool {
    let chars: Vec<char> = text.chars().collect();
    let mut i = 0;
    while i < chars.len() {
        if chars[i] == '$' {
            let mut j = i + 1;
            if j < chars.len() && chars[j].is_whitespace() {
                j += 1;
            }
            if j < chars.len() && chars[j].is_ascii_digit() {
                return true;
            }
        }
        i += 1;
    }
    false
}

/// Agent-not-speaking-yet classification (mirrors classify_agent_silence's
/// four-way split — transport_lost wins over agent_hung_up wins over
/// timeout wins over PROCESSING).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AgentSilenceOutcome {
    Processing,
    Timeout,
    Hangup,
    TransportError,
}

impl AgentSilenceOutcome {
    pub fn as_str(&self) -> &'static str {
        match self {
            AgentSilenceOutcome::Processing => "PROCESSING",
            AgentSilenceOutcome::Timeout => "TIMEOUT",
            AgentSilenceOutcome::Hangup => "HANGUP",
            AgentSilenceOutcome::TransportError => "TRANSPORT_ERROR",
        }
    }
}

pub fn classify_agent_silence(
    elapsed_ms: i64,
    turn_timeout_ms: i64,
    agent_hung_up: bool,
    transport_lost: bool,
) -> AgentSilenceOutcome {
    if transport_lost {
        return AgentSilenceOutcome::TransportError;
    }
    if agent_hung_up {
        return AgentSilenceOutcome::Hangup;
    }
    if elapsed_ms >= turn_timeout_ms {
        return AgentSilenceOutcome::Timeout;
    }
    AgentSilenceOutcome::Processing
}

/// Audio-level turn state (mirrors TurnState — same five string values).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TurnState {
    WaitingForAgent,
    AgentSpeaking,
    PossibleEnd,
    AgentTurnComplete,
    CallerTurn,
}

impl TurnState {
    pub fn as_str(&self) -> &'static str {
        match self {
            TurnState::WaitingForAgent => "WAITING_FOR_AGENT",
            TurnState::AgentSpeaking => "AGENT_SPEAKING",
            TurnState::PossibleEnd => "POSSIBLE_END",
            TurnState::AgentTurnComplete => "AGENT_TURN_COMPLETE",
            TurnState::CallerTurn => "CALLER_TURN",
        }
    }
}

/// TurnDetector state machine (mirrors orchestrator.py::TurnDetector).
/// wall-clock `now_ms` is always explicit — no monotonic clock inside,
/// so the machine is exactly replayable.
pub struct TurnDetector {
    pub silence_debounce_ms: i64,
    pub state: TurnState,
    pub chunk_count: u64,
    last_stop_ms: Option<i64>,
    intentional_interrupt: bool,
}

impl TurnDetector {
    pub fn new(silence_debounce_ms: i64) -> Self {
        Self {
            silence_debounce_ms,
            state: TurnState::WaitingForAgent,
            chunk_count: 0,
            last_stop_ms: None,
            intentional_interrupt: false,
        }
    }

    pub fn on_agent_audio_started(&mut self, now_ms: i64) {
        let _ = now_ms;
        self.state = TurnState::AgentSpeaking;
        self.chunk_count += 1;
        self.last_stop_ms = None;
    }

    pub fn on_agent_audio_stopped(&mut self, now_ms: i64) {
        if self.state == TurnState::AgentSpeaking {
            self.state = TurnState::PossibleEnd;
            self.last_stop_ms = Some(now_ms);
        }
    }

    pub fn mark_intentional_interrupt(&mut self) {
        self.intentional_interrupt = true;
    }

    pub fn intentional_interrupt(&self) -> bool {
        self.intentional_interrupt
    }

    pub fn poll(&mut self, now_ms: i64) -> TurnState {
        if self.state == TurnState::PossibleEnd {
            if let Some(last_stop) = self.last_stop_ms {
                if now_ms - last_stop >= self.silence_debounce_ms {
                    self.state = TurnState::AgentTurnComplete;
                }
            }
        }
        self.state
    }

    pub fn begin_caller_turn(&mut self) {
        self.state = TurnState::CallerTurn;
        self.chunk_count = 0;
        self.intentional_interrupt = false;
    }

    pub fn reset_for_next_agent_turn(&mut self) {
        self.state = TurnState::WaitingForAgent;
        self.chunk_count = 0;
        self.last_stop_ms = None;
        self.intentional_interrupt = false;
    }
}

/// Turn debounce formula (mirrors orchestrator.py::TurnDetector.poll()'s
/// core check). Pure function, no state machine needed for parity.
pub fn turn_complete_after_debounce(
    last_stop_ms: i64,
    now_ms: i64,
    silence_debounce_ms: i64,
) -> bool {
    now_ms - last_stop_ms >= silence_debounce_ms
}

/// Orchestrator identity/timeout counters (mirrors orchestrator.py::
/// Orchestrator minus the LiveKit-facing detector/evaluator wiring:
/// turn_id bumps + generation reset on advance_caller_turn, behavior-turn
/// budget on advance_behavior_turn, b<N> ids on start_behavior, and the
/// max_turns default-FAIL policy on check_max_turns).
pub struct OrchestratorState {
    behavior_seq: u64,
    behavior_id: String,
    turn_id: i64,
    generation_id: i64,
    behavior_turn_count: i64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BehaviorOutcome {
    Satisfied,
    Continue,
    FailedMaxTurns,
}

impl BehaviorOutcome {
    pub fn as_str(&self) -> &'static str {
        match self {
            BehaviorOutcome::Satisfied => "SATISFIED",
            BehaviorOutcome::Continue => "CONTINUE",
            BehaviorOutcome::FailedMaxTurns => "FAILED_MAX_TURNS",
        }
    }
}

impl OrchestratorState {
    pub fn new() -> Self {
        Self {
            behavior_seq: 0,
            behavior_id: "b0".to_string(),
            turn_id: 0,
            generation_id: 0,
            behavior_turn_count: 0,
        }
    }

    pub fn start_behavior(&mut self) -> String {
        self.behavior_seq += 1;
        self.behavior_id = format!("b{}", self.behavior_seq);
        self.behavior_turn_count = 0;
        self.behavior_id.clone()
    }

    pub fn current_identity(&self) -> GenerationIdentity {
        GenerationIdentity {
            behavior_id: self.behavior_id.clone(),
            turn_id: self.turn_id,
            generation_id: self.generation_id,
            context_version: 0,
        }
    }

    pub fn new_generation(&mut self) -> GenerationIdentity {
        self.generation_id += 1;
        self.current_identity()
    }

    pub fn advance_caller_turn(&mut self) {
        self.turn_id += 1;
        self.generation_id = 0;
    }

    pub fn advance_behavior_turn(&mut self) {
        self.behavior_turn_count += 1;
    }

    pub fn check_max_turns(&self, max_turns: i64) -> BehaviorOutcome {
        if self.behavior_turn_count >= max_turns {
            return BehaviorOutcome::FailedMaxTurns;
        }
        BehaviorOutcome::Continue
    }
}

impl Default for OrchestratorState {
    fn default() -> Self {
        Self::new()
    }
}

// ---------------------------------------------------------------------
// Seeded interruption policy (mirrors interaction_planner.py::
// CallerInteractionPlanner.should_interrupt). Pure function: SHA-256 over
// "{scenario_id}:{seed}:{agent_turn_index}", first 8 bytes big-endian /
// 2^64 compared against the per-rate threshold (low=0.15, medium=0.35,
// high=0.6). None interaction or absent rate means never. The interval
// gate lives in the driver, not here — same split as Python.
// ---------------------------------------------------------------------

pub const INTERRUPTION_PROBABILITY_LOW: f64 = 0.15;
pub const INTERRUPTION_PROBABILITY_MEDIUM: f64 = 0.35;
pub const INTERRUPTION_PROBABILITY_HIGH: f64 = 0.6;

// ---------------------------------------------------------------------
// Interaction planner delivery (mirrors interaction_planner.py: the
// DELIVERY layer — token-level ops on an already-validated utterance,
// never free-text generation). The transform whitelist is enforced BY
// CONSTRUCTION: only hesitation tokens from the fixed allowlist may be
// inserted; stumble duplicates an existing word with a "..." suffix.
// ---------------------------------------------------------------------

/// The ONLY tokens the planner may insert that are not already present
/// in the validated utterance (mirrors HESITATION_TOKEN_ALLOWLIST).
pub const HESITATION_TOKEN_ALLOWLIST: [&str; 3] = ["uh,", "um,", "hmm,"];
const DEFAULT_HESITATION_TOKEN: &str = "uh,";
const STUMBLE_SUFFIX: &str = "...";

/// Delivery-layer action kinds (mirrors InteractionActionKind — these
/// are delivery verbs, NEVER semantic Behaviors).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum InteractionActionKind {
    Speak,
    Dtmf,
    Silence,
    Hangup,
    Backchannel,
    BargeInTrigger,
}

impl InteractionActionKind {
    pub fn as_str(&self) -> &'static str {
        match self {
            InteractionActionKind::Speak => "SPEAK",
            InteractionActionKind::Dtmf => "DTMF",
            InteractionActionKind::Silence => "SILENCE",
            InteractionActionKind::Hangup => "HANGUP",
            InteractionActionKind::Backchannel => "BACKCHANNEL",
            InteractionActionKind::BargeInTrigger => "BARGE_IN_TRIGGER",
        }
    }
}

pub struct InteractionOutcome {
    pub kind: InteractionActionKind,
    pub tokens: Vec<String>,
    pub pre_delay_ms: i64,
    pub pace: Option<String>,
    pub dtmf_digits: Option<String>,
}

/// Minimal interaction config for the delivery layer (mirrors the
/// InteractionConfig fields plan_speak() reads: pace/hesitation/
//// stumble/pre_delay_ms — parsing/validation lives in caller_dsl.rs).
pub struct InteractionSpec {
    pub pace: Option<String>,
    pub hesitation: Option<String>,
    pub stumble: Option<String>,
    pub pre_delay_ms: i64,
}

/// Whitelist guard (mirrors verify_semantic_preserving): every token must
/// be a word from the original utterance (ignoring an appended stumble
/// "..." suffix) or a hesitation token from the allowlist.
pub fn verify_semantic_preserving(original_utterance: &str, tokens: &[String]) -> bool {
    let original_words: std::collections::HashSet<&str> =
        original_utterance.split_whitespace().collect();
    for tok in tokens {
        if HESITATION_TOKEN_ALLOWLIST.contains(&tok.as_str()) {
            continue;
        }
        let stripped = tok.strip_suffix(STUMBLE_SUFFIX).unwrap_or(tok);
        if !original_words.contains(stripped) {
            return false;
        }
    }
    true
}

pub fn plan_speak(
    validated_utterance: &str,
    interaction: Option<&InteractionSpec>,
) -> InteractionOutcome {
    let mut tokens: Vec<String> = validated_utterance
        .split_whitespace()
        .map(|s| s.to_string())
        .collect();
    let mut pre_delay_ms: i64 = 0;
    let mut pace: Option<String> = None;

    if let Some(ic) = interaction {
        pace = ic.pace.clone();
        pre_delay_ms = ic.pre_delay_ms;

        if let Some(h) = &ic.hesitation {
            if h != "none" {
                let insert_at = if tokens.len() > 1 { 1 } else { 0 };
                tokens.insert(insert_at, DEFAULT_HESITATION_TOKEN.to_string());
            }
        }

        if let Some(s) = &ic.stumble {
            if s != "none" && !tokens.is_empty() {
                let first_content = tokens
                    .iter()
                    .find(|t| !HESITATION_TOKEN_ALLOWLIST.contains(&t.as_str()))
                    .cloned()
                    .unwrap_or_else(|| tokens[0].clone());
                tokens.insert(0, format!("{}{}", first_content, STUMBLE_SUFFIX));
            }
        }
    }

    let outcome = InteractionOutcome {
        kind: InteractionActionKind::Speak,
        tokens,
        pre_delay_ms,
        pace,
        dtmf_digits: None,
    };
    debug_assert!(
        verify_semantic_preserving(validated_utterance, &outcome.tokens),
        "Interaction planner produced a token outside the semantic-preserving whitelist"
    );
    outcome
}

pub fn plan_dtmf(digits: &str) -> InteractionOutcome {
    InteractionOutcome {
        kind: InteractionActionKind::Dtmf,
        tokens: vec![],
        pre_delay_ms: 0,
        pace: None,
        dtmf_digits: Some(digits.to_string()),
    }
}

pub fn plan_silence() -> InteractionOutcome {
    InteractionOutcome {
        kind: InteractionActionKind::Silence,
        tokens: vec![],
        pre_delay_ms: 0,
        pace: None,
        dtmf_digits: None,
    }
}

pub fn plan_hangup() -> InteractionOutcome {
    InteractionOutcome {
        kind: InteractionActionKind::Hangup,
        tokens: vec![],
        pre_delay_ms: 0,
        pace: None,
        dtmf_digits: None,
    }
}

pub fn plan_backchannel(text: Option<&str>) -> InteractionOutcome {
    InteractionOutcome {
        kind: InteractionActionKind::Backchannel,
        tokens: vec![text.unwrap_or("uh-huh").to_string()],
        pre_delay_ms: 0,
        pace: None,
        dtmf_digits: None,
    }
}

pub fn trigger_barge_in() -> InteractionOutcome {
    InteractionOutcome {
        kind: InteractionActionKind::BargeInTrigger,
        tokens: vec![],
        pre_delay_ms: 0,
        pace: None,
        dtmf_digits: None,
    }
}

// ---------------------------------------------------------------------
// Record / replay primitives (mirrors record_replay.py: versioned
// RunRecord, RecordedAttempt, Recorder semantics, RecordedSemanticVerifier
// zero-AI fallback, ReplayLanguageBackend loud-divergence gates).
// Scope note (same as Python): these are the PRIMITIVES — CLI wiring is
// separate. Replay never re-invokes any verifier backend; every divergence
// (verdict, outcome, exhaustion) fails loudly, never silently.
// ---------------------------------------------------------------------

/// Current record format version (mirrors RECORD_FORMAT_VERSION).
pub const RECORD_FORMAT_VERSION: i32 = 2;
/// Legacy readable version (mirrors RECORD_FORMAT_V1).
pub const RECORD_FORMAT_V1: i32 = 1;

#[derive(Debug, Clone)]
pub struct RecordedAttempt {
    pub candidate: CandidateUtterance,
    pub verdict: String,
    pub reason: Option<String>,
    pub retry_index: i64,
    pub observed: Option<ObservedActSnapshot>,
    pub outcome_failure: Option<String>,
    pub outcome_ended_by: Option<String>,
}

/// Serializable snapshot of verifier evidence (mirrors the `observed`
/// dict on RecordedAttempt — act/target/confidence/all_acts as classified
/// at record time).
#[derive(Debug, Clone, PartialEq)]
pub struct ObservedActSnapshot {
    pub act: String,
    pub target: Option<String>,
    pub confidence: f64,
    pub all_acts: Vec<String>,
}

#[derive(Debug, Clone)]
pub struct RunRecord {
    pub scenario_id: String,
    pub seed: i64,
    pub attempts: Vec<RecordedAttempt>,
    pub format_version: i32,
}

#[derive(Debug, Clone, PartialEq)]
pub enum RecordError {
    UnsupportedVersion(i32),
    Empty,
}

impl std::fmt::Display for RecordError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            RecordError::UnsupportedVersion(v) => {
                write!(
                    f,
                    "unsupported record_format version {:?}, expected 2 (v1 also reads)",
                    v
                )
            }
            RecordError::Empty => write!(f, "record has no attempts"),
        }
    }
}

impl RunRecord {
    pub fn version_supported(version: i32) -> bool {
        version != RECORD_FORMAT_VERSION && version != RECORD_FORMAT_V1
    }

    fn attempt_from_value(raw: &Value) -> Result<RecordedAttempt, String> {
        let candidate: CandidateUtterance = serde_json::from_value(raw["candidate"].clone())
            .map_err(|e| format!("candidate shape mismatch: {}", e))?;
        let observed = match &raw["observed"] {
            Value::Null => None,
            v if v.is_object() => Some(ObservedActSnapshot {
                act: v["act"].as_str().unwrap_or("").to_string(),
                target: v["target"].as_str().map(|s| s.to_string()),
                confidence: v["confidence"].as_f64().unwrap_or(0.0),
                all_acts: v["all_acts"]
                    .as_array()
                    .map(|a| {
                        a.iter()
                            .filter_map(|x| x.as_str().map(|s| s.to_string()))
                            .collect()
                    })
                    .unwrap_or_default(),
            }),
            _ => None,
        };
        Ok(RecordedAttempt {
            candidate,
            verdict: raw["verdict"].as_str().unwrap_or("").to_string(),
            reason: raw["reason"].as_str().map(|s| s.to_string()),
            retry_index: raw["retry_index"].as_i64().unwrap_or(0),
            observed,
            outcome_failure: raw["outcome_failure"].as_str().map(|s| s.to_string()),
            outcome_ended_by: raw["outcome_ended_by"].as_str().map(|s| s.to_string()),
        })
    }

    pub fn from_value(raw: &Value) -> Result<Self, RecordError> {
        let version = raw["format_version"].as_i64().unwrap_or(-1) as i32;
        if version != RECORD_FORMAT_VERSION && version != RECORD_FORMAT_V1 {
            return Err(RecordError::UnsupportedVersion(version));
        }
        let attempts = raw["attempts"]
            .as_array()
            .cloned()
            .unwrap_or_default()
            .iter()
            .map(Self::attempt_from_value)
            .collect::<Result<Vec<_>, _>>()
            .map_err(|_| RecordError::Empty)?;
        Ok(Self {
            scenario_id: raw["scenario_id"].as_str().unwrap_or("").to_string(),
            seed: raw["seed"].as_i64().unwrap_or(0),
            attempts,
            format_version: version,
        })
    }

    pub fn from_json(raw: &str) -> Result<Self, RecordError> {
        let value: Value = serde_json::from_str(raw).map_err(|_| RecordError::Empty)?;
        Self::from_value(&value)
    }

    pub fn to_value(&self) -> Value {
        let attempts: Vec<Value> = self
            .attempts
            .iter()
            .map(|a| {
                serde_json::json!({
                    "candidate": {
                        "act": a.candidate.act,
                        "target": a.candidate.target,
                        "slots": a.candidate.slots,
                        "utterance": a.candidate.utterance,
                        "identity": {
                            "behavior_id": a.candidate.identity.behavior_id,
                            "turn_id": a.candidate.identity.turn_id,
                            "generation_id": a.candidate.identity.generation_id,
                            "context_version": a.candidate.identity.context_version,
                        },
                    },
                    "verdict": a.verdict,
                    "reason": a.reason,
                    "retry_index": a.retry_index,
                    "observed": a.observed.as_ref().map(|o| serde_json::json!({
                        "act": o.act,
                        "target": o.target,
                        "confidence": o.confidence,
                        "all_acts": o.all_acts,
                    })),
                    "outcome_failure": a.outcome_failure,
                    "outcome_ended_by": a.outcome_ended_by,
                })
            })
            .collect();
        serde_json::json!({
            "format_version": self.format_version,
            "scenario_id": self.scenario_id,
            "seed": self.seed,
            "attempts": attempts,
        })
    }
}

/// Zero-AI verifier (mirrors RecordedSemanticVerifier): replays recorded
/// observed evidence in order instead of classifying. A `None` slot (v1
/// record, or an attempt that never reached the semantic step) yields the
/// neutral low-confidence observation (forces UNKNOWN, never a false PASS).
/// Running past the end errors — never a silent divergence.
pub struct RecordedSemanticVerifier {
    observed: Vec<Option<ObservedActSnapshot>>,
    cursor: usize,
}

impl RecordedSemanticVerifier {
    pub fn new(record: &RunRecord) -> Self {
        Self {
            observed: record.attempts.iter().map(|a| a.observed.clone()).collect(),
            cursor: 0,
        }
    }

    /// Next recorded observation: `Ok(snapshot)` for a v2 slot, the
    /// neutral low-confidence observation for a `None` slot (v1 / attempt
    /// never reached the semantic step), `Err` when evidence runs out.
    pub fn classify_next(&mut self) -> Result<ObservedActSnapshot, String> {
        if self.cursor >= self.observed.len() {
            return Err(
                "VERIFIER_UNAVAILABLE: replay exhausted: no more recorded semantic evidence"
                    .to_string(),
            );
        }
        let slot = self.observed[self.cursor].clone();
        self.cursor += 1;
        Ok(slot.unwrap_or(ObservedActSnapshot {
            act: String::new(),
            target: None,
            confidence: 0.0,
            all_acts: vec![],
        }))
    }
}

/// Loud-divergence replay backend (mirrors ReplayLanguageBackend):
/// replays recorded candidates in order; assert_verdict/assert_outcome
/// fail loudly on divergence; exhaustion errors instead of inventing.
pub struct ReplayLanguageBackend {
    attempts: Vec<RecordedAttempt>,
    cursor: usize,
    last_verdict: Option<String>,
}

impl ReplayLanguageBackend {
    pub fn new(record: &RunRecord) -> Self {
        Self {
            attempts: record.attempts.clone(),
            cursor: 0,
            last_verdict: None,
        }
    }

    pub fn generate(&mut self) -> Result<&CandidateUtterance, String> {
        if self.cursor >= self.attempts.len() {
            return Err("replay exhausted: no more recorded attempts".to_string());
        }
        let attempt = &self.attempts[self.cursor];
        self.cursor += 1;
        self.last_verdict = Some(attempt.verdict.clone());
        Ok(&attempt.candidate)
    }

    pub fn assert_verdict(&self, actual_verdict: &str) -> Result<(), String> {
        match &self.last_verdict {
            None => Err("assert_verdict() called before any generate() call".to_string()),
            Some(recorded) if recorded == actual_verdict => Ok(()),
            Some(recorded) => Err(format!(
                "replay verdict mismatch: recorded {:?}, replayed run produced {:?}",
                recorded, actual_verdict
            )),
        }
    }

    pub fn assert_outcome(
        &self,
        failure_reason: Option<&str>,
        ended_by: &str,
    ) -> Result<(), String> {
        let last = match self.attempts.last() {
            None => return Ok(()),
            Some(a) => a,
        };
        if last.outcome_failure.is_none() && last.outcome_ended_by.is_none() {
            return Ok(()); // v1 record: no outcome evidence to assert
        }
        if last.outcome_failure.as_deref() != failure_reason
            || last.outcome_ended_by.as_deref() != Some(ended_by)
        {
            return Err(format!(
                "replay outcome mismatch: recorded ({:?}, {:?}), replayed run produced ({:?}, {:?})",
                last.outcome_failure, last.outcome_ended_by, failure_reason, ended_by
            ));
        }
        Ok(())
    }
}

/// Bounded-retry attempt count (mirrors validate_with_retry's loop:
/// max_retries+1 total generate→validate attempts).
pub fn retry_attempt_count(max_retries: u32) -> u32 {
    max_retries + 1
}

pub fn interruption_roll(scenario_id: &str, seed: u64, agent_turn_index: u64) -> f64 {
    use sha2::{Digest, Sha256};

    let key = format!("{scenario_id}:{seed}:{agent_turn_index}");
    let digest = Sha256::digest(key.as_bytes());
    let mut bytes = [0u8; 8];
    bytes.copy_from_slice(&digest[..8]);
    u64::from_be_bytes(bytes) as f64 / 18446744073709551616.0
}

pub fn should_interrupt(
    scenario_id: &str,
    seed: Option<u64>,
    agent_turn_index: u64,
    rate: Option<&str>,
) -> bool {
    let rate = match rate {
        Some(r) if !r.is_empty() => r,
        _ => return false,
    };
    let threshold = match rate {
        "low" => INTERRUPTION_PROBABILITY_LOW,
        "medium" => INTERRUPTION_PROBABILITY_MEDIUM,
        "high" => INTERRUPTION_PROBABILITY_HIGH,
        _ => return false,
    };
    interruption_roll(scenario_id, seed.unwrap_or(0), agent_turn_index) < threshold
}

// ---------------------------------------------------------------------
// Parity tests: read the SAME JSON fixtures the Python side reads.
// ---------------------------------------------------------------------

#[cfg(test)]
mod parity_tests {
    use super::*;
    use std::fs;
    use std::path::PathBuf;

    fn fixtures_dir() -> PathBuf {
        // crates/lks-core -> crates -> livekit_agent_simulator_rust -> src -> repo root
        PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../../../..")
            .join("tests")
            .join("fixtures")
            .join("parity")
    }

    fn load(name: &str) -> Value {
        let path = fixtures_dir().join(name);
        let raw = fs::read_to_string(&path)
            .unwrap_or_else(|e| panic!("failed to read fixture {}: {}", path.display(), e));
        serde_json::from_str(&raw)
            .unwrap_or_else(|e| panic!("invalid JSON in {}: {}", path.display(), e))
    }

    fn build_contract(raw: &Value) -> BehaviorContract {
        serde_json::from_value(raw.clone()).expect("contract shape mismatch")
    }

    fn build_candidate(raw: &Value) -> CandidateUtterance {
        serde_json::from_value(raw.clone()).expect("candidate shape mismatch")
    }

    const VALIDATOR_VECTOR_FILES: [&str; 11] = [
        "validator_valid_pass.json",
        "validator_act_mismatch.json",
        "validator_target_mismatch.json",
        "validator_slot_violation.json",
        "validator_slot_string_coercion.json",
        "validator_schema_invalid.json",
        "validator_utterance_too_long.json",
        "validator_end_call_not_allowed.json",
        "validator_forbidden_intent_lexical.json",
        "validator_forbidden_intent_nested_semantic.json",
        "validator_ambiguous_low_confidence.json",
    ];

    #[test]
    fn validator_vectors_produce_expected_verdict_in_rust() {
        for filename in VALIDATOR_VECTOR_FILES {
            let data = load(filename);
            let contract = build_contract(&data["contract"]);
            let candidate = build_candidate(&data["candidate"]);
            // Semantic verification is MANDATORY (mirrors validator.py:
            // explicit None still constructs the rule baseline). The flag
            // only records which tier the vector was authored against.
            let _use_semantic = data["use_semantic_verifier"].as_bool().unwrap_or(false);
            let validator = ContractValidator::new(Some(RuleBasedSemanticVerifier));

            let result = validator.validate(&candidate, &contract);

            let expected_verdict = data["expected_verdict"].as_str().unwrap();
            assert_eq!(
                result.verdict.as_str(),
                expected_verdict,
                "{}: expected {}, got {} (reason={:?})",
                filename,
                expected_verdict,
                result.verdict.as_str(),
                result.reason
            );

            if let Some(expected_reason) = data["expected_reason"].as_str() {
                // SCHEMA_INVALID carries a ": <detail>" suffix in Python (the
                // ValueError message); parity asserts the PREFIX only — the
                // Rust side returns the bare code. Verdict equality above is
                // exact.
                if expected_reason == "SCHEMA_INVALID" {
                    assert!(
                        result
                            .reason
                            .as_deref()
                            .is_some_and(|r| r.starts_with("SCHEMA_INVALID")),
                        "{}: reason mismatch: {:?}",
                        filename,
                        result.reason
                    );
                } else {
                    assert_eq!(
                        result.reason.as_deref(),
                        Some(expected_reason),
                        "{}: reason mismatch",
                        filename
                    );
                }
            }
        }
    }

    #[test]
    fn turn_debounce_vector_matches_rust_formula() {
        let data = load("turn_debounce.json");
        for case in data["cases"].as_array().unwrap() {
            let debounce = case["silence_debounce_ms"].as_i64().unwrap();
            let last_stop = case["last_stop_ms"].as_i64().unwrap();
            let now = case["now_ms"].as_i64().unwrap();
            let expected = case["expected_turn_complete"].as_bool().unwrap();
            assert_eq!(
                turn_complete_after_debounce(last_stop, now, debounce),
                expected,
                "{:?}",
                case
            );
        }
    }

    #[test]
    fn staleness_vector_matches_rust_is_current() {
        let data = load("staleness_drop.json");
        for case in data["cases"].as_array().unwrap() {
            let identity: GenerationIdentity =
                serde_json::from_value(case["identity"].clone()).unwrap();
            let current: GenerationIdentity =
                serde_json::from_value(case["current"].clone()).unwrap();
            let expected = case["expected_is_current"].as_bool().unwrap();
            assert_eq!(
                is_current(&identity, &current),
                expected,
                "{}",
                case["name"].as_str().unwrap_or("<unnamed>")
            );
        }
    }

    #[test]
    fn should_interrupt_vector_matches_rust_policy() {
        let data = load("should_interrupt.json");
        assert_eq!(
            data["none_interaction_expected"].as_bool(),
            Some(false),
            "fixture must assert None interaction never interrupts"
        );
        assert_eq!(
            data["no_rate_expected"].as_bool(),
            Some(false),
            "fixture must assert absent rate never interrupts"
        );
        assert!(!super::should_interrupt("s", Some(0), 0, None));
        assert!(!super::should_interrupt("s", Some(0), 0, Some("")));
        for case in data["cases"].as_array().unwrap() {
            let expected = case["expected"].as_bool().unwrap();
            let actual = super::should_interrupt(
                case["scenario_id"].as_str().unwrap(),
                Some(case["seed"].as_u64().unwrap()),
                case["turn"].as_u64().unwrap(),
                case["rate"].as_str(),
            );
            assert_eq!(actual, expected, "{:?}", case);
        }
    }

    #[test]
    fn semantic_lexicon_vector_matches_rust_classifier() {
        let data = load("semantic_lexicon.json");
        let verifier = super::RuleBasedSemanticVerifier;
        for case in data["cases"].as_array().unwrap() {
            let contract = super::BehaviorContract {
                behavior: case["behavior"].as_str().unwrap().to_string(),
                target: case["target"].as_str().map(|s| s.to_string()),
                constraints: super::ContractConstraints {
                    max_turns: 3,
                    max_budget: None,
                    max_words: None,
                    max_duration_s: None,
                    forbidden_intents: vec![],
                    must_not: vec![],
                },
            };
            let observed = verifier.classify(case["utterance"].as_str().unwrap(), &contract);
            let exp = &case["expected"];
            assert_eq!(observed.act, exp["act"].as_str().unwrap(), "{:?}", case);
            assert_eq!(
                observed.confidence,
                exp["confidence"].as_f64().unwrap(),
                "{:?}",
                case
            );
            assert_eq!(
                observed.target.as_deref(),
                exp["target"].as_str(),
                "{:?}",
                case
            );
            let expected_acts: Vec<String> = exp["all_acts"]
                .as_array()
                .unwrap()
                .iter()
                .map(|v| v.as_str().unwrap().to_string())
                .collect();
            assert_eq!(observed.all_acts, expected_acts, "{:?}", case);
        }
    }

    #[test]
    fn orchestrator_evaluator_vector_matches_rust_logic() {
        let data = load("orchestrator_evaluator.json");

        for case in data["evaluator_cases"].as_array().unwrap() {
            let target = case["contract_target"].as_str().map(|s| s.to_string());
            let actual =
                super::evaluate_behavior(target.as_deref(), case["text"].as_str().unwrap());
            assert_eq!(
                actual.as_str(),
                case["expected"].as_str().unwrap(),
                "{:?}",
                case
            );
        }

        for case in data["silence_cases"].as_array().unwrap() {
            let actual = super::classify_agent_silence(
                case["elapsed_ms"].as_i64().unwrap(),
                case["turn_timeout_ms"].as_i64().unwrap(),
                case["agent_hung_up"].as_bool().unwrap(),
                case["transport_lost"].as_bool().unwrap(),
            );
            assert_eq!(
                actual.as_str(),
                case["expected"].as_str().unwrap(),
                "{:?}",
                case
            );
        }

        let script = &data["turn_detector_script"];
        let mut detector =
            super::TurnDetector::new(script["silence_debounce_ms"].as_i64().unwrap());
        for step in script["steps"].as_array().unwrap() {
            match step["op"].as_str().unwrap() {
                "poll" => {
                    let state = detector.poll(step["at_ms"].as_i64().unwrap());
                    assert_eq!(
                        state.as_str(),
                        step["expected_state"].as_str().unwrap(),
                        "{:?}",
                        step
                    );
                }
                "started" => detector.on_agent_audio_started(step["at_ms"].as_i64().unwrap()),
                "stopped" => detector.on_agent_audio_stopped(step["at_ms"].as_i64().unwrap()),
                "begin_caller_turn" => detector.begin_caller_turn(),
                "reset" => detector.reset_for_next_agent_turn(),
                op => panic!("unknown turn-detector op {:?}", op),
            }
        }

        let script = &data["orchestrator_script"];
        let mut orch = super::OrchestratorState::new();
        for step in script["steps"].as_array().unwrap() {
            match step["op"].as_str().unwrap() {
                "identity" => {
                    let ident = orch.current_identity();
                    let exp = &step["expected"];
                    assert_eq!(
                        ident.behavior_id,
                        exp["behavior_id"].as_str().unwrap(),
                        "{:?}",
                        step
                    );
                    assert_eq!(
                        ident.turn_id,
                        exp["turn_id"].as_i64().unwrap(),
                        "{:?}",
                        step
                    );
                    assert_eq!(
                        ident.generation_id,
                        exp["generation_id"].as_i64().unwrap(),
                        "{:?}",
                        step
                    );
                }
                "advance_caller_turn" => orch.advance_caller_turn(),
                "advance_behavior_turn" => orch.advance_behavior_turn(),
                "new_generation" => {
                    orch.new_generation();
                }
                "start_behavior" => {
                    orch.start_behavior();
                }
                "check_max_turns" => {
                    let outcome = orch.check_max_turns(step["max_turns"].as_i64().unwrap());
                    assert_eq!(
                        outcome.as_str(),
                        step["expected"].as_str().unwrap(),
                        "{:?}",
                        step
                    );
                }
                op => panic!("unknown orchestrator op {:?}", op),
            }
        }
    }

    #[test]
    fn failure_codes_vector_matches_rust_constants() {
        let data = load("failure_codes.json");
        let expected_reasons: Vec<String> = data["expected_failure_reasons"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_str().unwrap().to_string())
            .collect();
        let mut actual_reasons: Vec<String> =
            FAILURE_REASONS.iter().map(|s| s.to_string()).collect();
        actual_reasons.sort();
        let mut expected_sorted = expected_reasons.clone();
        expected_sorted.sort();
        assert_eq!(actual_reasons, expected_sorted);

        let expected_ended_by: Vec<String> = data["expected_ended_by"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_str().unwrap().to_string())
            .collect();
        let mut actual_ended_by: Vec<String> = ENDED_BY.iter().map(|s| s.to_string()).collect();
        actual_ended_by.sort();
        let mut expected_eb_sorted = expected_ended_by.clone();
        expected_eb_sorted.sort();
        assert_eq!(actual_ended_by, expected_eb_sorted);
    }

    #[test]
    fn interaction_planner_vector_matches_rust_delivery() {
        let data = load("interaction_planner.json");

        let allowlist: Vec<String> = data["hesitation_allowlist"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_str().unwrap().to_string())
            .collect();
        let mut actual_allowlist: Vec<String> = super::HESITATION_TOKEN_ALLOWLIST
            .iter()
            .map(|s| s.to_string())
            .collect();
        actual_allowlist.sort();
        let mut expected_allowlist = allowlist.clone();
        expected_allowlist.sort();
        assert_eq!(actual_allowlist, expected_allowlist);

        for case in data["speak_cases"].as_array().unwrap() {
            let utterance = case["utterance"].as_str().unwrap();
            let ic = &case["interaction"];
            let interaction = if ic.is_null() {
                None
            } else {
                Some(super::InteractionSpec {
                    pace: ic["pace"].as_str().map(|s| s.to_string()),
                    hesitation: ic
                        .get("hesitation")
                        .and_then(|v| v.as_str())
                        .map(|s| s.to_string()),
                    stumble: ic
                        .get("stumble")
                        .and_then(|v| v.as_str())
                        .map(|s| s.to_string()),
                    pre_delay_ms: ic.get("pre_delay_ms").and_then(|v| v.as_i64()).unwrap_or(0),
                })
            };
            let outcome = super::plan_speak(utterance, interaction.as_ref());
            let exp = &case["expected"];
            let expected_tokens: Vec<String> = exp["tokens"]
                .as_array()
                .unwrap()
                .iter()
                .map(|v| v.as_str().unwrap().to_string())
                .collect();
            assert_eq!(outcome.tokens, expected_tokens, "{:?}", case);
            assert_eq!(
                outcome.pre_delay_ms,
                exp["pre_delay_ms"].as_i64().unwrap(),
                "{:?}",
                case
            );
            assert_eq!(outcome.pace.as_deref(), exp["pace"].as_str(), "{:?}", case);
            assert!(
                super::verify_semantic_preserving(utterance, &outcome.tokens),
                "{:?}: planner broke its own whitelist",
                case
            );
        }

        for case in data["preserving_cases"].as_array().unwrap() {
            let tokens: Vec<String> = case["tokens"]
                .as_array()
                .unwrap()
                .iter()
                .map(|v| v.as_str().unwrap().to_string())
                .collect();
            assert_eq!(
                super::verify_semantic_preserving(case["utterance"].as_str().unwrap(), &tokens),
                case["expected"].as_bool().unwrap(),
                "{:?}",
                case
            );
        }

        for case in data["action_cases"].as_array().unwrap() {
            let kind = match case["op"].as_str().unwrap() {
                "dtmf" => super::plan_dtmf(case["digits"].as_str().unwrap()).kind,
                "silence" => super::plan_silence().kind,
                "hangup" => super::plan_hangup().kind,
                "backchannel_default" => {
                    let o = super::plan_backchannel(None);
                    let expected_tokens: Vec<String> = case["expected_tokens"]
                        .as_array()
                        .unwrap()
                        .iter()
                        .map(|v| v.as_str().unwrap().to_string())
                        .collect();
                    assert_eq!(o.tokens, expected_tokens, "{:?}", case);
                    o.kind
                }
                "barge_in" => super::trigger_barge_in().kind,
                op => panic!("unknown planner action op {:?}", op),
            };
            assert_eq!(
                kind.as_str(),
                case["expected_kind"].as_str().unwrap(),
                "{:?}",
                case
            );
        }
    }

    #[test]
    fn record_replay_vector_matches_rust_primitives() {
        let data = load("record_replay.json");

        assert_eq!(
            super::RECORD_FORMAT_VERSION,
            data["format_version"].as_i64().unwrap() as i32
        );
        assert_eq!(
            super::RECORD_FORMAT_V1,
            data["v1_legacy_version"].as_i64().unwrap() as i32
        );
        for bad in data["rejected_versions"].as_array().unwrap() {
            assert!(super::RunRecord::version_supported(
                bad.as_i64().unwrap() as i32
            ));
        }

        // v1 legacy record: reads, observed is None, RecordedSemanticVerifier
        // falls back to the neutral low-confidence observation on the FIRST
        // classify() and errors when evidence runs out on the second.
        let v1 = super::RunRecord::from_value(&data["v1_record"]).expect("v1 must read");
        assert_eq!(v1.format_version, 1);
        assert!(v1.attempts[0].observed.is_none());
        let mut verifier = super::RecordedSemanticVerifier::new(&v1);
        let fallback = verifier
            .classify_next()
            .expect("first fallback observation");
        let exp_fallback = &data["v1_expected"]["fallback_observed"];
        assert_eq!(fallback.act, exp_fallback["act"].as_str().unwrap());
        assert_eq!(
            fallback.confidence,
            exp_fallback["confidence"].as_f64().unwrap()
        );
        let err = verifier.classify_next().expect_err("evidence exhausted");
        assert!(
            err.contains(
                data["v1_expected"]["second_classify_error_prefix"]
                    .as_str()
                    .unwrap()
            ),
            "unexpected exhaustion error: {}",
            err
        );

        // v2 record: round-trips, replays the recorded utterance verbatim,
        // rebuilds the identity triple, asserts verdict + outcome match and
        // raises ReplayMismatch on divergence.
        let v2 = super::RunRecord::from_value(&data["v2_record"]).expect("v2 must read");
        let v2_json = serde_json::to_string(&v2.to_value()).unwrap();
        let v2_rt = super::RunRecord::from_json(&v2_json).expect("v2 round-trip");
        assert_eq!(v2_rt.scenario_id, v2.scenario_id);
        assert_eq!(v2_rt.seed, v2.seed);
        assert_eq!(v2_rt.attempts.len(), 1);
        let mut replay = super::ReplayLanguageBackend::new(&v2_rt);
        let candidate = replay.generate().expect("one recorded attempt");
        assert_eq!(
            candidate.utterance,
            data["v2_expected"]["replayed_utterance"].as_str().unwrap()
        );
        let exp_ident = &data["v2_expected"]["rebuilt_identity"];
        assert_eq!(
            candidate.identity.behavior_id,
            exp_ident["behavior_id"].as_str().unwrap()
        );
        assert_eq!(
            candidate.identity.turn_id,
            exp_ident["turn_id"].as_i64().unwrap()
        );
        assert_eq!(
            candidate.identity.generation_id,
            exp_ident["generation_id"].as_i64().unwrap()
        );
        replay
            .assert_verdict("VALID")
            .expect("matching verdict passes");
        replay
            .assert_outcome(None, "scenario")
            .expect("matching outcome passes");
        assert!(replay.assert_verdict("INVALID").is_err());
        assert!(replay
            .assert_outcome(Some("CALLER_BEHAVIOR_VIOLATION"), "scenario")
            .is_err());
        assert!(replay.assert_outcome(None, "timeout").is_err());
        assert!(
            replay.generate().is_err(),
            "exhausted replay must fail loudly"
        );

        // Bounded retry (mirrors validate_with_retry's early exit): the run
        // stops at the FIRST VALID result, so a passing generator uses 1
        // attempt regardless of max_retries; an always-invalid generator
        // exhausts all max_retries+1 attempts with candidate None.
        for case in data["retry_cases"].as_array().unwrap() {
            let max_retries = case["max_retries"].as_i64().unwrap() as u32;
            let exhausted = case["always_invalid"].as_bool().unwrap();
            let attempts = if exhausted {
                retry_attempt_count(max_retries)
            } else {
                1
            };
            let expected_attempts = case["expected_attempts"].as_i64().unwrap() as u32;
            assert_eq!(attempts, expected_attempts, "{:?}", case);
            assert_eq!(
                !exhausted,
                !case["expected_candidate_null"].as_bool().unwrap(),
                "{:?}",
                case
            );
            assert_eq!(
                if exhausted { "INVALID" } else { "VALID" },
                case["expected_verdict"].as_str().unwrap(),
                "{:?}",
                case
            );
        }
    }
}
