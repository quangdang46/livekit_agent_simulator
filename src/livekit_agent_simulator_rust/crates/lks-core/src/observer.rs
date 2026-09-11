//! `Observer` transcript/turn state machine — pure Rust port of
//! `on_transcript` from `livekit_agent_simulator/livekit/observer.py`
//! (observer.py:458-591). No livekit types anywhere in this module: callers
//! (lks-livekit's room.rs/callers/*) feed it plain `(role, text, final,
//! source)` plus explicit clocks, exactly like `caller_contract.rs` is fed
//! plain strings/timestamps rather than live room objects.
//!
//! Byte-level spec: `docs/plans/PLAN-20260813-rust-full-port.md`, Appendix F,
//! "Transcription intake" + "`_accept_final` dedupe + turn model" sections —
//! implemented verbatim, don't re-derive. Parity fixture:
//! `tests/fixtures/parity/observer_transcript.json`, replayed identically by
//! `tests/test_parity_vectors.py` (Python) and this module's
//! `#[cfg(test)] mod parity_tests` (Rust).
//!
//! Explicitly out of scope for this port (see the plan's "explicitly out of
//! scope" section): RMS audio-onset detection, agent-room audio recording,
//! and `AgentSessionObserver`'s request/response snapshot subsystem.

use std::collections::{HashMap, HashSet};
use std::time::Instant;

use serde_json::{Map, Value as Json};

use crate::logging::event::EventWriter;

const TOPIC_LK_TRANSCRIPTION: &str = "lk.transcription";
const SOURCE_SIM_OPENAI: &str = "sim.openai";

/// Provider sim-transcript sources are the most trustworthy caller
/// transcripts; data-topic + lk.transcription are mirrors (observer.py:32-34).
const SIM_TRANSCRIPT_SOURCES: [&str; 2] = ["sim.gemini", "sim.openai"];
/// Lower index = higher priority when deduping finals from multiple sources
/// (observer.py:35-36) — do not unify/reorder.
const USER_FINAL_PRIORITY: [&str; 4] = ["sim.gemini", "sim.openai", "data", TOPIC_LK_TRANSCRIPTION];
const AGENT_FINAL_PRIORITY: [&str; 4] = ["data", TOPIC_LK_TRANSCRIPTION, "sim.gemini", "sim.openai"];

const BACKCHANNEL_GRACE_MS_DEFAULT: i64 = 700;
const BACKCHANNEL_MAX_WORDS_DEFAULT: usize = 6;
const TRANSCRIPT_DEDUPE_WINDOW_MS_DEFAULT: i64 = 15_000;

/// Port of `_normalize_text` (observer.py:54-55): strip, then drop ALL
/// whitespace — case-sensitive.
fn normalize_text(text: &str) -> String {
    text.trim().chars().filter(|c| !c.is_whitespace()).collect()
}

/// Port of `_similar_text` (observer.py:58-66).
fn similar_text(a: &str, b: &str) -> bool {
    if a == b {
        return true;
    }
    if a.is_empty() || b.is_empty() {
        return false;
    }
    let (shorter, longer) = if a.chars().count() <= b.chars().count() {
        (a, b)
    } else {
        (b, a)
    };
    if longer.contains(shorter) {
        (shorter.chars().count() as f64) / (longer.chars().count() as f64) >= 0.85
    } else {
        false
    }
}

/// Port of `_canonical_source` (observer.py:69-72).
fn canonical_source(source: &str) -> &str {
    if SIM_TRANSCRIPT_SOURCES.contains(&source) || source == TOPIC_LK_TRANSCRIPTION {
        source
    } else {
        "data"
    }
}

/// Port of `_source_priority_rank` (observer.py:75-81).
fn source_priority_rank(source: &str, role: &str) -> usize {
    let order: &[&str] = if role == "user" {
        &USER_FINAL_PRIORITY
    } else {
        &AGENT_FINAL_PRIORITY
    };
    let canonical = canonical_source(source);
    order.iter().position(|s| *s == canonical).unwrap_or(order.len())
}

/// Config knobs `Observer` needs from the full `ObserveConfig` — kept small
/// and livekit-independent rather than threading the whole config surface
/// (topic/pattern fields irrelevant to this state machine) into `lks-core`.
#[derive(Debug, Clone)]
pub struct ObserverConfig {
    pub agent_identity: String,
    pub sim_identity: String,
    /// "user" | "agent" — mirrors `Scenario.first_speaker`.
    pub first_speaker: String,
    pub transcript_dedupe_window_ms: i64,
    pub backchannel_grace_ms: i64,
    pub backchannel_max_words: usize,
}

impl Default for ObserverConfig {
    fn default() -> Self {
        Self {
            agent_identity: String::new(),
            sim_identity: String::new(),
            first_speaker: "agent".to_string(),
            transcript_dedupe_window_ms: TRANSCRIPT_DEDUPE_WINDOW_MS_DEFAULT,
            backchannel_grace_ms: BACKCHANNEL_GRACE_MS_DEFAULT,
            backchannel_max_words: BACKCHANNEL_MAX_WORDS_DEFAULT,
        }
    }
}

/// Port of `Observer` (observer.py) — transcript dedupe/backchannel/preamble
/// state machine only (the room-event-registration half stays in
/// `lks-livekit`, which owns the livekit types).
pub struct Observer {
    cfg: ObserverConfig,
    turn: i64,
    finalized_roles: HashSet<String>,
    current_turn_user_norm: Option<String>,
    /// (role, normalized text) -> (source, monotonic time) — port of
    /// `_recent_finals` (observer.py:151). Entries never pruned/expired,
    /// matching Python.
    dedupe_entries: HashMap<(String, String), (String, Instant)>,
    last_interim_key: Option<(String, String)>,

    // Public polling fields — mirror the Python instance attributes consumed
    // live by `caller_contract/agent_wait.py`'s `ObserverAgentWait` (not
    // wired to Rust's caller_contract in this pass; kept for future use).
    pub last_agent_final_mono: Option<Instant>,
    pub last_agent_final_text: Option<String>,
    pub agent_is_active_speaker: bool,
    pub agent_has_spoken: bool,
    pub agent_replied_this_turn: bool,
    pub user_has_spoken: bool,
    pub last_user_final_mono: Option<Instant>,
    last_activity_mono: Option<Instant>,
    any_activity: bool,
}

impl Observer {
    pub fn new(cfg: ObserverConfig) -> Self {
        Self {
            cfg,
            turn: 0,
            finalized_roles: HashSet::new(),
            current_turn_user_norm: None,
            dedupe_entries: HashMap::new(),
            last_interim_key: None,
            last_agent_final_mono: None,
            last_agent_final_text: None,
            agent_is_active_speaker: false,
            agent_has_spoken: false,
            agent_replied_this_turn: false,
            user_has_spoken: false,
            last_user_final_mono: None,
            last_activity_mono: None,
            any_activity: false,
        }
    }

    pub fn turn(&self) -> i64 {
        self.turn
    }

    /// Port of `any_activity_occurred` (observer.py:453-455).
    pub fn any_activity_occurred(&self) -> bool {
        self.any_activity
    }

    pub fn last_activity_mono(&self) -> Option<Instant> {
        self.last_activity_mono
    }

    fn role_has_final(&self, role: &str) -> bool {
        self.finalized_roles.contains(role)
    }

    /// Port of `_accept_final` (observer.py:432-447): drop duplicate finals
    /// from lower-priority sources within the dedupe window.
    fn accept_final(&mut self, role: &str, text: &str, source: &str, now: Instant) -> bool {
        let norm = normalize_text(text);
        if norm.is_empty() {
            return false;
        }
        let key = (role.to_string(), norm);
        let window = std::time::Duration::from_millis(self.cfg.transcript_dedupe_window_ms.max(0) as u64);
        if let Some((prev_source, prev_mono)) = self.dedupe_entries.get(&key) {
            if now.saturating_duration_since(*prev_mono) <= window
                && source_priority_rank(source, role) >= source_priority_rank(prev_source, role)
            {
                return false;
            }
        }
        self.dedupe_entries.insert(key, (source.to_string(), now));
        true
    }

    /// Port of `on_transcript` (observer.py:458-591) — the single chokepoint
    /// for every transcript source (lk.transcription text streams, data-topic
    /// `transcript_turn` payloads, and provider-native `sim.gemini`/
    /// `sim.openai` deltas). Ordering of side effects matches Python exactly;
    /// see the Appendix F spec referenced in the module doc comment.
    #[allow(clippy::too_many_arguments)]
    pub fn on_transcript(
        &mut self,
        w: &mut EventWriter,
        role: &str,
        text: &str,
        final_: bool,
        segment_id: Option<&str>,
        source: &str,
        now_mono: Instant,
        now_wall_ms: i64,
    ) {
        // (1) writer.update_dialogue — BEFORE dedupe; dropped finals still count as activity.
        w.update_dialogue(role, text, final_, Some(now_wall_ms));

        // (2) spec = {text, final} + segment_id if present.
        let mut spec: Map<String, Json> = Map::new();
        spec.insert("text".into(), Json::String(text.to_string()));
        spec.insert("final".into(), Json::Bool(final_));
        if let Some(sid) = segment_id {
            spec.insert("segment_id".into(), Json::String(sid.to_string()));
        }

        // (3) Any transcript: role==agent -> agent_has_spoken (interim counts).
        // ALWAYS: last_activity_mono/any_activity (dead-call net gate).
        if role == "agent" {
            self.agent_has_spoken = true;
        }
        self.last_activity_mono = Some(now_mono);
        self.any_activity = true;

        // (4) Not final: drop late interim after final; else emit interim, return.
        if !final_ {
            if self.role_has_final(role) {
                return;
            }
            w.emit(&format!("transcript.{role}.interim"), Some(&spec), source, None, None, true, None);
            return;
        }

        // (5) Ordering guard (OpenAI bridge only): emit a final-flavored
        // interim first so consumers never see interim-after-final.
        let interim_key = (role.to_string(), text.to_string());
        if source == SOURCE_SIM_OPENAI && self.last_interim_key.as_ref() != Some(&interim_key) {
            self.last_interim_key = Some(interim_key);
            let mut interim_spec = spec.clone();
            interim_spec.insert("final".into(), Json::Bool(false));
            w.emit(&format!("transcript.{role}.interim"), Some(&interim_spec), source, None, None, true, None);
        }

        // (6) Dedupe gate.
        if !self.accept_final(role, text, source, now_mono) {
            return;
        }

        if role == "user" {
            self.on_user_final(w, text, source, now_mono, spec);
        } else {
            self.on_agent_final(w, text, source, now_mono, spec);
        }
    }

    /// (7) USER branch (observer.py:511-559).
    fn on_user_final(
        &mut self,
        w: &mut EventWriter,
        text: &str,
        source: &str,
        now_mono: Instant,
        spec: Map<String, Json>,
    ) {
        let norm = normalize_text(text);
        let is_echo_of_prior_turn = self.agent_replied_this_turn
            && (Some(&norm) == self.current_turn_user_norm.as_ref()
                || similar_text(&norm, self.current_turn_user_norm.as_deref().unwrap_or("")));
        if is_echo_of_prior_turn {
            return;
        }

        let mut merge_as_same_turn = self.last_user_final_mono.is_some() && !self.agent_replied_this_turn;
        if !merge_as_same_turn {
            if let (Some(last_user), Some(last_agent)) =
                (self.last_user_final_mono, self.last_agent_final_mono)
            {
                if self.agent_replied_this_turn && last_agent >= last_user {
                    let agent_gap_ms = last_agent.duration_since(last_user).as_millis() as i64;
                    let agent_word_count = self
                        .last_agent_final_text
                        .as_deref()
                        .unwrap_or("")
                        .split_whitespace()
                        .count();
                    if agent_gap_ms <= self.cfg.backchannel_grace_ms
                        && agent_word_count <= self.cfg.backchannel_max_words
                    {
                        merge_as_same_turn = true;
                    }
                }
            }
        }

        if merge_as_same_turn {
            let mut merged_spec = spec;
            merged_spec.insert("same_turn".into(), Json::Bool(true));
            w.emit("transcript.user.final", Some(&merged_spec), source, None, None, true, None);
            self.agent_replied_this_turn = false;
            return;
        }

        self.user_has_spoken = true;
        self.turn += 1;
        self.current_turn_user_norm = Some(norm);
        w.begin_turn(self.turn);
        self.finalized_roles.clear();
        self.last_user_final_mono = Some(now_mono);
        self.agent_replied_this_turn = false;
        self.finalized_roles.insert("user".to_string());
        w.emit("transcript.user.final", Some(&spec), source, None, None, true, None);
    }

    /// (8) AGENT branch (observer.py:560-591).
    fn on_agent_final(
        &mut self,
        w: &mut EventWriter,
        text: &str,
        source: &str,
        now_mono: Instant,
        mut spec: Map<String, Json>,
    ) {
        if self.turn == 0 && self.cfg.first_speaker == "user" && !self.user_has_spoken {
            self.last_agent_final_mono = Some(now_mono);
            self.last_agent_final_text = Some(text.to_string());
            spec.insert(
                "note".into(),
                Json::String("agent spoke before user; not counted as a turn".into()),
            );
            w.emit("transcript.agent.preamble", Some(&spec), source, None, None, true, None);
            return;
        }
        if self.turn == 0 {
            self.turn = 1;
            w.begin_turn(self.turn);
        }
        if !self.agent_replied_this_turn {
            if let Some(last_user) = self.last_user_final_mono {
                if now_mono >= last_user {
                    let turn_taking_ms = now_mono.duration_since(last_user).as_millis() as i64;
                    spec.insert("turn_taking_ms".into(), Json::Number(turn_taking_ms.into()));
                }
            }
        }
        self.agent_replied_this_turn = true;
        self.last_agent_final_mono = Some(now_mono);
        self.last_agent_final_text = Some(text.to_string());
        self.finalized_roles.insert("agent".to_string());
        w.emit("transcript.agent.final", Some(&spec), source, None, None, true, None);
    }
}

// ---------------------------------------------------------------------
// Parity tests: read the SAME JSON fixture the Python side reads
// (tests/test_parity_vectors.py), following the exact convention already
// established by caller_contract.rs's parity_tests module.
// ---------------------------------------------------------------------

#[cfg(test)]
mod parity_tests {
    use super::*;
    use std::fs;
    use std::path::PathBuf;
    use std::time::Duration;

    fn fixtures_dir() -> PathBuf {
        // crates/lks-core -> crates -> livekit_agent_simulator_rust -> src -> repo root
        PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../../../..")
            .join("tests")
            .join("fixtures")
            .join("parity")
    }

    fn load(name: &str) -> Json {
        let path = fixtures_dir().join(name);
        let raw = fs::read_to_string(&path)
            .unwrap_or_else(|e| panic!("failed to read fixture {}: {}", path.display(), e));
        serde_json::from_str(&raw)
            .unwrap_or_else(|e| panic!("invalid JSON in {}: {}", path.display(), e))
    }

    fn tmp_writer() -> (PathBuf, EventWriter) {
        let dir = std::env::temp_dir().join(format!("lks-observer-test-{}", uuid::Uuid::new_v4()));
        let w = EventWriter::new("r-test", dir.clone(), "UTC", 2500).expect("EventWriter::new");
        (dir, w)
    }

    fn observer_from_case_config(cfg: &Json) -> Observer {
        let mut oc = ObserverConfig::default();
        if let Some(v) = cfg.get("agent_identity").and_then(|v| v.as_str()) {
            oc.agent_identity = v.to_string();
        }
        if let Some(v) = cfg.get("sim_identity").and_then(|v| v.as_str()) {
            oc.sim_identity = v.to_string();
        }
        if let Some(v) = cfg.get("first_speaker").and_then(|v| v.as_str()) {
            oc.first_speaker = v.to_string();
        }
        if let Some(v) = cfg.get("transcript_dedupe_window_ms").and_then(|v| v.as_i64()) {
            oc.transcript_dedupe_window_ms = v;
        }
        Observer::new(oc)
    }

    #[test]
    fn observer_transcript_vector_matches_python_logic() {
        let data = load("observer_transcript.json");
        let cases = data["cases"].as_array().expect("cases array");
        for case in cases {
            let name = case["name"].as_str().unwrap_or("<unnamed>");
            let mut obs = observer_from_case_config(&case["config"]);
            let (dir, mut w) = tmp_writer();
            let base = Instant::now();

            for step in case["steps"].as_array().expect("steps array") {
                let role = step["role"].as_str().expect("step.role");
                let text = step["text"].as_str().expect("step.text");
                let final_ = step["final"].as_bool().unwrap_or(true);
                let source = step["source"].as_str().expect("step.source");
                let at_mono_ms = step["at_mono_ms"].as_i64().unwrap_or(0).max(0) as u64;
                let segment_id = step.get("segment_id").and_then(|v| v.as_str());
                let now_mono = base + Duration::from_millis(at_mono_ms);
                obs.on_transcript(&mut w, role, text, final_, segment_id, source, now_mono, at_mono_ms as i64);
            }

            if let Some(expect_events) = case.get("expect_events").and_then(|v| v.as_array()) {
                for expected in expect_events {
                    let kind = expected["kind"].as_str().expect("expect_events[].kind");
                    let matches: Vec<_> = w.events().iter().filter(|e| e.get("kind").and_then(|k| k.as_str()) == Some(kind)).collect();
                    assert!(!matches.is_empty(), "case {name}: expected at least one {kind} event, found none");
                    if let Some(spec_contains) = expected.get("spec_contains").and_then(|v| v.as_object()) {
                        let found = matches.iter().any(|e| {
                            let spec = e.get("spec").and_then(|s| s.as_object());
                            spec_contains.iter().all(|(k, v)| spec.and_then(|s| s.get(k)) == Some(v))
                        });
                        assert!(found, "case {name}: no {kind} event matched spec_contains {spec_contains:?}");
                    }
                }
            }

            if let Some(counts) = case.get("expect_event_counts").and_then(|v| v.as_object()) {
                for (kind, expected_count) in counts {
                    let expected_count = expected_count.as_i64().expect("expect_event_counts[] value");
                    let actual = w.events().iter().filter(|e| e.get("kind").and_then(|k| k.as_str()) == Some(kind.as_str())).count() as i64;
                    assert_eq!(actual, expected_count, "case {name}: expected {expected_count} {kind} event(s), found {actual}");
                }
            }

            if let Some(expected_turn) = case.get("expect_final_turn").and_then(|v| v.as_i64()) {
                assert_eq!(obs.turn(), expected_turn, "case {name}: final turn mismatch");
            }

            let _ = fs::remove_dir_all(&dir);
        }
    }
}
