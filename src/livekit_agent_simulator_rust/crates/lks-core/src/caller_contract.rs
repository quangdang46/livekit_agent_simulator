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
pub const ENDED_BY: [&str; 6] = ["scenario", "caller", "agent", "timeout", "transport", "error"];

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
    /// end/hangup behavior — mirrors Python's `ends_call_allowed()`.
    pub fn ends_call_allowed(&self) -> bool {
        matches!(self.behavior.as_str(), "end" | "hangup" | "hang_up")
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
        ("financing", &["financing", "finance option", "loan", "payment plan"]),
        ("trade_in", &["trade in", "trade-in", "trade my"]),
        (
            "vehicle_change",
            &["different car", "another vehicle", "looking for a toyota", "instead"],
        ),
    ]
}

fn act_patterns() -> Vec<(&'static str, &'static [&'static str])> {
    vec![
        (
            "ask",
            &["what's", "what is", "how much", "could you tell me", "what are you asking", "do you offer"],
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
        ("confirm", &["so it's", "just to confirm", "is that right", "to confirm"]),
        ("deny", &["no thanks", "i don't think so", "that won't work"]),
        ("accept", &["sounds good", "that works", "i'll take it", "works for me", "okay, that's"]),
        ("reject", &["not interested", "no, i'd rather", "i'll pass"]),
        ("provide", &["i'm calling about", "i want", "i'd like to"]),
        ("arrange_visit", &["come by", "hold it until", "schedule a time", "book a time"]),
        ("end", &["goodbye", "bye", "thanks, that's all", "have a good day"]),
    ]
}

fn target_keywords() -> Vec<(&'static str, &'static [&'static str])> {
    vec![
        (
            "price",
            &[
                "price", "$", "cost", "fee", "charge", "quote", "budget",
                "lower the price", "monthly fee", "how much",
            ],
        ),
        (
            "delivery_date",
            &[
                "deliver", "delivery", "arrival", "arrive", "shipping", "ship",
                "eta", "when will", "how long",
            ],
        ),
        (
            "order_status",
            &["order", "status", "delayed", "delay", "shipment", "tracking"],
        ),
        (
            "hours",
            &["hours", "open", "close", "opening", "what time"],
        ),
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

        // Prefer contract.behavior on ties (mirrors Python's tie-break).
        let best_act = hits
            .iter()
            .max_by_key(|(act, count)| (**count, **act == contract.behavior.as_str()))
            .map(|(act, _)| act.to_string())
            .unwrap();
        let primary_hits = hits[best_act.as_str()];

        let mut all_acts: Vec<String> = vec![best_act.clone()];
        for act in hits.keys() {
            if !all_acts.contains(&act.to_string()) {
                all_acts.push(act.to_string());
            }
        }
        for tag in detected_intent_tags {
            if !all_acts.contains(&tag) {
                all_acts.push(tag);
            }
        }

        let target = target_evidence(utterance, &contract.target);

        ObservedAct { act: best_act, target, confidence: confidence_for(primary_hits), all_acts }
    }
}

// ---------------------------------------------------------------------
// ContractValidator — deterministic pipeline (mirrors
// caller_contract/validator.py::ContractValidator). Same check order,
// same reason strings.
// ---------------------------------------------------------------------

const END_CALL_PHRASES: [&str; 6] =
    ["goodbye", "bye", "thanks, that's all", "that's all i needed", "i'll let you go", "have a good day"];

fn word_count(text: &str) -> usize {
    text.split_whitespace().filter(|w| !w.is_empty()).count()
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

    pub fn validate(&self, candidate: &CandidateUtterance, contract: &BehaviorContract) -> ValidationResult {
        // 1. Schema: empty utterance is malformed.
        if candidate.utterance.trim().is_empty() {
            return ValidationResult { verdict: Verdict::Invalid, reason: Some("SCHEMA_INVALID".to_string()) };
        }

        // 2. Act.
        if candidate.act != contract.behavior {
            return ValidationResult { verdict: Verdict::Invalid, reason: Some("ACT_MISMATCH".to_string()) };
        }

        // 3. Target claim shape — candidate.target is a generator claim,
        // NEVER evidence (mirrors validator.py step 3). The authoritative
        // target verdict comes from observed.target below; no claim-equality
        // check here by design.

        // 4. Slot / numeric constraints.
        if let Some(max_budget) = contract.constraints.max_budget {
            if let Some(claimed) = candidate.slots.get("max_budget").and_then(|v| v.as_f64()) {
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
        let must_not_end =
            contract.constraints.must_not.iter().any(|m| m == "end_call") || !contract.ends_call_allowed();
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
        if let Some(_hit) = lexical_forbidden_intent_hit(&candidate.utterance, &contract.constraints.forbidden_intents)
        {
            return ValidationResult {
                verdict: Verdict::Invalid,
                reason: Some("FORBIDDEN_INTENT_DETECTED".to_string()),
            };
        }

        // 8. Optional semantic verifier.
        if let Some(verifier) = &self.semantic_verifier {
            let observed = verifier.classify(&candidate.utterance, contract);

            if observed.confidence < SEMANTIC_CONFIDENCE_THRESHOLD {
                return ValidationResult { verdict: Verdict::Unknown, reason: Some("LOW_CONFIDENCE".to_string()) };
            }

            for act in &observed.all_acts {
                if act != &contract.behavior && contract.constraints.forbidden_intents.contains(act) {
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

        ValidationResult { verdict: Verdict::Valid, reason: None }
    }
}

// ---------------------------------------------------------------------
// Turn debounce formula (mirrors orchestrator.py::TurnDetector.poll()'s
// core check). Pure function, no state machine needed for parity.
// ---------------------------------------------------------------------

pub fn turn_complete_after_debounce(last_stop_ms: i64, now_ms: i64, silence_debounce_ms: i64) -> bool {
    now_ms - last_stop_ms >= silence_debounce_ms
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
        serde_json::from_str(&raw).unwrap_or_else(|e| panic!("invalid JSON in {}: {}", path.display(), e))
    }

    fn build_contract(raw: &Value) -> BehaviorContract {
        serde_json::from_value(raw.clone()).expect("contract shape mismatch")
    }

    fn build_candidate(raw: &Value) -> CandidateUtterance {
        serde_json::from_value(raw.clone()).expect("candidate shape mismatch")
    }

    const VALIDATOR_VECTOR_FILES: [&str; 9] = [
        "validator_valid_pass.json",
        "validator_act_mismatch.json",
        "validator_target_mismatch.json",
        "validator_slot_violation.json",
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
            let validator =
                ContractValidator::new(Some(RuleBasedSemanticVerifier));

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
                assert_eq!(
                    result.reason.as_deref(),
                    Some(expected_reason),
                    "{}: reason mismatch",
                    filename
                );
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
            assert_eq!(turn_complete_after_debounce(last_stop, now, debounce), expected, "{:?}", case);
        }
    }

    #[test]
    fn staleness_vector_matches_rust_is_current() {
        let data = load("staleness_drop.json");
        for case in data["cases"].as_array().unwrap() {
            let identity: GenerationIdentity = serde_json::from_value(case["identity"].clone()).unwrap();
            let current: GenerationIdentity = serde_json::from_value(case["current"].clone()).unwrap();
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
    fn failure_codes_vector_matches_rust_constants() {
        let data = load("failure_codes.json");
        let expected_reasons: Vec<String> = data["expected_failure_reasons"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_str().unwrap().to_string())
            .collect();
        let mut actual_reasons: Vec<String> = FAILURE_REASONS.iter().map(|s| s.to_string()).collect();
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
}
