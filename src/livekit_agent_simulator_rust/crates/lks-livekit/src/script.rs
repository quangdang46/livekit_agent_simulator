//! Script runtime (port of `script/runtime.py` core slice).
//!
//! Walks the scenario's Script steps in order with their triggers:
//! - `time`: fire after delay_ms
//! - `silence`: fire when the agent is NOT the active speaker (with the
//!   require_agent_spoke_first gate)
//! - `agent_speaking`: fire after min_agent_active_ms + delay_ms of agent speech
//!
//! Actions: `speak` (text injected into the caller's voice via the OpenAI
//! bridge), `wait` (hold silence), `hang_up` (end the run with a farewell),
//! `dtmf` (publish keypad tones — wired at the bridge level).

use std::sync::Arc;
use std::time::{Duration, Instant};

use livekit::webrtc::audio_source::native::NativeAudioSource;

use lks_core::errors::RunError;
use lks_core::logging::event::EventWriter;
use serde_json::json;
use tokio::sync::{mpsc, Mutex};

/// A mid-call cue the Script runtime (or interrupt-rate runner) hands to the
/// caller bridge for real delivery (port of `bridge.inject_cue`).
#[derive(Debug, Clone)]
pub enum CueCommand {
    /// Verbatim caller speech — OpenAI: conversation.item.create + response.
    Speak { text: String, label: String },
    /// Keypad tones via LiveKit SIP DTMF data packet (publish_dtmf).
    Dtmf { digits: String },
}

/// Channel the run wires between the ScriptRuntime and the caller bridge.
pub type CueTx = mpsc::UnboundedSender<CueCommand>;
pub type CueRx = mpsc::UnboundedReceiver<CueCommand>;

/// Minimal observer state the runtime reads (fed by the bridge).
#[derive(Debug, Default, Clone)]
pub struct ScriptObserverState {
    pub agent_is_active_speaker: bool,
    pub agent_has_spoken: bool,
    pub user_has_spoken: bool,
    pub agent_replied_this_turn: bool,
    pub last_agent_final_text: String,
}

/// One settled caller/agent line (mirrors `caller_contract.Turn` /
/// `contract_do.DoTurn`). The history replay below rebuilds the
/// `recent_turns` context Python passes as `log` — the exact
/// pre-`do:` dialogue including verbatim `say:` lines and real agent
/// replies — instead of leaving `run_contract_do` with only its own
/// intra-`do:` lines. Locks are never held when waking waiters; the
/// history handle itself is sync (parking_lot) so TranscriptHistory::push
/// is callable from the async bridge emit sites without awaiting.
#[derive(Debug, Default, Clone)]
pub struct TurnHistoryEntry {
    pub speaker: String,
    pub text: String,
}

#[derive(Debug, Default)]
pub struct TranscriptHistory {
    inner: parking_lot::Mutex<Vec<TurnHistoryEntry>>,
}

impl TranscriptHistory {
    pub fn new() -> Self {
        Self {
            inner: parking_lot::Mutex::new(Vec::new()),
        }
    }

    pub fn push(&self, speaker: &str, text: &str) {
        let text = text.trim();
        if text.is_empty() {
            return;
        }
        let mut guard = self.inner.lock();
        // Adjacent-duplicate guard: the same line often arrives from BOTH
        // the bridge emit (sim.script) and the lk.transcription re-final of
        // our own TTS'd speech seconds later — keep one copy so the
        // generator prompt does not see doubled caller lines.
        if let Some(last) = guard.last() {
            if last.speaker == speaker && last.text.trim() == text {
                return;
            }
        }
        guard.push(TurnHistoryEntry {
            speaker: speaker.to_string(),
            text: text.to_string(),
        });
    }

    pub fn snapshot(&self) -> Vec<TurnHistoryEntry> {
        self.inner.lock().clone()
    }
}

pub type SharedTranscriptHistory = std::sync::Arc<TranscriptHistory>;

/// What the runtime asks the bridge to do.
pub enum ScriptAction {
    Speak {
        text: String,
        label: String,
        barge_in: bool,
        interrupt_class: Option<String>,
        delivery: String,
    },
    HangUp {
        farewell: String,
        label: String,
    },
    Wait,
    Dtmf {
        digits: String,
    },
    RoomPcm {
        asset: String,
        gain: f64,
        r#loop: bool,
        label: String,
    },
}

/// Hang-up deferral bookkeeping (port of script/runtime.py _hang_up_ready).
#[derive(Debug, Clone)]
#[allow(dead_code)]
struct DeferState {
    start: std::time::Instant,
    prior_reason: String,
    budget_ms: i64,
}

pub struct ScriptRuntime {
    steps: Vec<serde_json::Value>,
    writer: Arc<Mutex<EventWriter>>,
    state: Arc<Mutex<ScriptObserverState>>,
    /// Sender used to signal hang_up → end the run.
    end_tx: tokio::sync::broadcast::Sender<()>,
    /// Callback executed for each fired action (the bridge implements it).
    on_action: Box<dyn Fn(ScriptAction) -> Result<(), String> + Send + Sync>,
    /// Active hang-up deferral (None = not deferring).
    defer_state: parking_lot::Mutex<Option<DeferState>>,
    /// Locale for the default hang-up farewell text (from config).
    #[allow(dead_code)]
    locale: String,
    /// API key for the `do:` text backend (same key the caller bridge
    /// uses — `cfg.simulator.api_key`; port of
    /// `live_wiring.py::_build_text_backend`, no separate credential).
    do_api_key: String,
    /// `do:` text-backend provider ("openai"|"google" — mirrors
    /// `cfg.simulator.provider`; port of `_build_text_backend` provider
    /// selection in `live_wiring.py`).
    do_provider: String,
    /// run_spec.first_speaker ("agent"|"user") — gates the legacy
    /// require_agent_spoke_first silence assumption (see trigger gate).
    first_speaker: String,
    /// Full pre-`do:` dialogue replay (verbatim `say:` lines + real agent
    /// replies), shared with the bridge emit sites — see TranscriptHistory.
    history: SharedTranscriptHistory,
}

impl ScriptRuntime {
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        steps: Vec<serde_json::Value>,
        writer: Arc<Mutex<EventWriter>>,
        state: Arc<Mutex<ScriptObserverState>>,
        end_tx: tokio::sync::broadcast::Sender<()>,
        on_action: Box<dyn Fn(ScriptAction) -> Result<(), String> + Send + Sync>,
        locale: String,
        do_api_key: String,
        do_provider: String,
        first_speaker: String,
        history: SharedTranscriptHistory,
    ) -> Self {
        Self {
            steps,
            writer,
            state,
            end_tx,
            on_action,
            defer_state: parking_lot::Mutex::new(None),
            locale,
            do_api_key,
            do_provider,
            first_speaker,
            history,
        }
    }

    fn step_str(step: &serde_json::Value, key: &str, default: &str) -> String {
        step.get(key)
            .and_then(|v| v.as_str())
            .unwrap_or(default)
            .to_string()
    }

    fn step_i64(step: &serde_json::Value, key: &str) -> i64 {
        step.get(key).and_then(|v| v.as_i64()).unwrap_or(0)
    }

    fn step_bool(step: &serde_json::Value, key: &str, default: bool) -> bool {
        step.get(key).and_then(|v| v.as_bool()).unwrap_or(default)
    }

    /// Run the script loop until all steps fire or hang_up signals end.
    pub async fn run(&self, stop_rx: tokio::sync::broadcast::Receiver<()>) -> Result<(), RunError> {
        if self.steps.is_empty() {
            return Ok(());
        }
        let mut fired: Vec<String> = Vec::new();
        let mut arm_idx: usize = 0;
        let mut trigger_since: Vec<Option<Instant>> = vec![None; self.steps.len()];
        let mut awaiting_reply_since: Option<Instant> = None;
        // Local "have we fired our own opener yet" flag — see BUG FIX note
        // below on why this must NOT be `state.user_has_spoken`.
        let mut own_turn_fired = false;
        let mut stop_rx = stop_rx;

        while arm_idx < self.steps.len() {
            tokio::select! {
                _ = stop_rx.recv() => return Ok(()),
                _ = tokio::time::sleep(Duration::from_millis(50)) => {}
            }
            // Clear mute_persona when the loop re-arms.
            crate::callers::openai::MUTE_PERSONA_ACTIVE
                .store(false, std::sync::atomic::Ordering::SeqCst);
            // Post-cue gap: after a speak step, wait for the agent to reply
            // (up to 8s) before arming the next step — mirrors the Python
            // _await_agent_reply window so steps don't fire over the agent.
            //
            // first_speaker=user opener exception (run 002-final-lksr2): the
            // FIRST caller turn has no agent reply to wait for yet — the
            // agent's greeting comes AFTER our opener by construction. Waiting
            // here deadlocks step 2 behind an 8s gate on every iteration
            // while the agent (hearing nothing yet — TTS for step 1 may still
            // be synthesizing) stays silent. Skip the gap until the caller
            // has spoken at least once (user_has_spoken).
            //
            // BUG FIX (run 006-dealer-live-full, take 1): `|| !state.agent_is_active_speaker`
            // used to trivially satisfy this gate — the agent isn't the
            // active speaker BEFORE it starts talking either (TTS/response
            // latency), so every speak step fired the very next 50ms tick
            // instead of waiting for a real agent final.
            //
            // BUG FIX (run 006-dealer-live-full, take 2): swapping in
            // `!state.user_has_spoken` still burst-fired steps 2-5 — that
            // flag only flips once the ROOM's transcription pipeline has
            // transcribed OUR OWN TTS'd speech back as a "user final" (see
            // callers/openai.rs role=="user" arm), which lags several
            // seconds behind us firing the cue. A LOCAL flag (own_turn_fired,
            // set the instant we fire our own step, not derived from async
            // STT) is the only thing that actually reflects "have we spoken
            // yet" at gate-check time. `agent_replied_this_turn` (set true
            // only on a genuine agent final) remains the real gate.
            if let Some(since) = awaiting_reply_since {
                let state = self.state.lock().await;
                let replied = state.agent_replied_this_turn || !own_turn_fired;
                drop(state);
                if replied || since.elapsed() >= Duration::from_secs(8) {
                    awaiting_reply_since = None;
                } else {
                    continue;
                }
            }

            let step = self.steps[arm_idx].clone();
            let id = Self::step_str(&step, "id", &format!("step-{arm_idx}"));
            if fired.contains(&id) {
                arm_idx += 1;
                continue;
            }
            let once = Self::step_bool(&step, "once", true);
            let trigger = Self::step_str(&step, "trigger", "agent_speaking");
            let action = Self::step_str(&step, "action", "speak");
            let delay_ms = Self::step_i64(&step, "delay_ms");
            let min_agent_active_ms = Self::step_i64(&step, "min_agent_active_ms");

            // Trigger gate.
            //
            // first_speaker=user contract runs (e.g. dealer-live-full):
            // the caller opens the call, so silence-gated steps must NOT
            // wait for the agent to have spoken first — the agent hasn't
            // said anything yet by construction (its greeting comes AFTER
            // our opener). Port of run_orchestrator first_speaker=user
            // semantics: caller starts immediately; only first_speaker=agent
            // runs wait for the greeting first. The stale
            // require_agent_spoke_first=true default (a legacy-script
            // assumption) deadlocked every silence step here — run 009 fired
            // only caller-step-0 then sat until the slice cap.
            let first_speaker_is_agent = self.first_speaker == "agent";
            let state = self.state.lock().await;
            let active = match trigger.as_str() {
                "time" => true,
                "silence" => {
                    if first_speaker_is_agent
                        && Self::step_bool(&step, "require_agent_spoke_first", true)
                        && !state.agent_has_spoken
                    {
                        false
                    } else {
                        !state.agent_is_active_speaker
                    }
                }
                _ => state.agent_is_active_speaker,
            };
            let need = if trigger == "agent_speaking" {
                min_agent_active_ms + delay_ms
            } else {
                delay_ms
            };
            drop(state);

            if !active {
                trigger_since[arm_idx] = None;
                continue;
            }
            let started = *trigger_since[arm_idx].get_or_insert_with(Instant::now);
            let elapsed_ms = started.elapsed().as_millis() as i64;
            if elapsed_ms < need {
                continue;
            }

            // Fire the step.
            trigger_since[arm_idx] = None;
            let waited_ms = elapsed_ms;
            let label = Self::step_str(&step, "label", &id);
            let say = Self::step_str(&step, "say", "");
            let barge_in = Self::step_bool(&step, "barge_in", false);
            let icls = step
                .get("interrupt_class")
                .or_else(|| step.get("class"))
                .and_then(|v| v.as_str())
                .map(String::from);
            let delivery = Self::step_str(&step, "delivery", "gemini_text");

            if action == "contract_do" {
                // Never hold the EventWriter lock across the do-driver's
                // network calls / agent-reply wait — acquire it fresh for
                // each emit inside run_contract_do instead.
                if once {
                    fired.push(id.clone());
                }
                let label = Self::step_str(&step, "label", &id);
                self.run_contract_do(&step, &id, &label).await?;
                arm_idx += 1;
                continue;
            }

            let mut w = self.writer.lock().await;
            match action.as_str() {
                "hang_up" => {
                    // ── Hang-up deferral (port of runtime.py _hang_up_ready) ──
                    let require_reply =
                        Self::step_bool(&step, "require_agent_reply_this_turn", true);
                    let defer_open = Self::step_bool(&step, "defer_on_open_question", true);
                    let budget_ms = {
                        let raw = Self::step_i64(&step, "open_question_idle_ms");
                        if raw > 0 {
                            raw
                        } else {
                            20000
                        }
                    };
                    let state_snapshot = self.state.lock().await;
                    let user_spoke =
                        state_snapshot.user_has_spoken && !state_snapshot.agent_replied_this_turn;
                    let open_question = defer_open
                        && lks_core::script::hang_up_gate::agent_left_open_turn(Some(
                            &state_snapshot.last_agent_final_text,
                        ));
                    let should_defer = (require_reply && user_spoke) || open_question;
                    let reason = if require_reply && user_spoke {
                        Some("awaiting_agent_reply")
                    } else if open_question {
                        Some("open_agent_question")
                    } else {
                        None
                    };
                    if should_defer {
                        let mut ds = self.defer_state.lock();
                        match &*ds {
                            Some(d) => {
                                if d.start.elapsed() >= Duration::from_millis(budget_ms as u64) {
                                    let deferred_ms = d.start.elapsed().as_millis() as i64;
                                    let mut ds_spec = serde_json::Map::new();
                                    ds_spec.insert("step_id".into(), json!(id));
                                    ds_spec.insert("label".into(), json!(label));
                                    ds_spec
                                        .insert("reason".into(), json!("defer_budget_exhausted"));
                                    ds_spec.insert("prior_reason".into(), json!(d.prior_reason));
                                    ds_spec.insert("deferred_ms".into(), json!(deferred_ms));
                                    ds_spec.insert("budget_ms".into(), json!(budget_ms));
                                    ds_spec.insert(
                                        "last_agent_final".into(),
                                        json!(
                                            state_snapshot.last_agent_final_text[..state_snapshot
                                                .last_agent_final_text
                                                .len()
                                                .min(240)]
                                        ),
                                    );
                                    drop(state_snapshot);
                                    w.emit(
                                        "sim.script.hang_up_deferred",
                                        Some(&ds_spec),
                                        "sim.script",
                                        None,
                                        None,
                                        false,
                                        None,
                                    );
                                    *ds = None;
                                    drop(ds);
                                } else {
                                    drop(state_snapshot);
                                    drop(ds);
                                    drop(w);
                                    continue;
                                }
                            }
                            None => {
                                let prior = reason.unwrap_or("open_agent_question").to_string();
                                let mut ds_spec = serde_json::Map::new();
                                ds_spec.insert("step_id".into(), json!(id));
                                ds_spec.insert("label".into(), json!(label));
                                ds_spec.insert("reason".into(), json!(prior));
                                ds_spec.insert("deferred_ms".into(), json!(0));
                                ds_spec.insert("budget_ms".into(), json!(budget_ms));
                                ds_spec.insert(
                                    "last_agent_final".into(),
                                    json!(
                                        state_snapshot.last_agent_final_text
                                            [..state_snapshot.last_agent_final_text.len().min(240)]
                                    ),
                                );
                                drop(state_snapshot);
                                w.emit(
                                    "sim.script.hang_up_deferred",
                                    Some(&ds_spec),
                                    "sim.script",
                                    None,
                                    None,
                                    false,
                                    None,
                                );
                                *ds = Some(DeferState {
                                    start: std::time::Instant::now(),
                                    prior_reason: prior,
                                    budget_ms,
                                });
                                drop(ds);
                                drop(w);
                                continue;
                            }
                        }
                    } else {
                        *self.defer_state.lock() = None;
                        drop(state_snapshot);
                    }

                    let farewell = if say.is_empty() {
                        "Thanks, that's all for now. Bye.".to_string()
                    } else {
                        say.clone()
                    };
                    let mut spec = serde_json::Map::new();
                    spec.insert("step_id".into(), json!(id));
                    spec.insert("label".into(), json!(label));
                    spec.insert("say".into(), json!(farewell));
                    spec.insert("trigger".into(), json!(trigger));
                    spec.insert("action".into(), json!("hang_up"));
                    spec.insert("barge_in".into(), json!(barge_in));
                    spec.insert("waited_ms".into(), json!(waited_ms));
                    w.emit(
                        "sim.script.hang_up",
                        Some(&spec),
                        "sim.script",
                        None,
                        None,
                        false,
                        None,
                    );
                    w.emit(
                        "sim.hang_up",
                        Some(&spec),
                        "sim.script",
                        None,
                        None,
                        false,
                        None,
                    );
                    drop(w);
                    let _ = (self.on_action)(ScriptAction::HangUp { farewell, label });
                    let _ = self.end_tx.send(());
                    return Ok(());
                }
                "room_pcm" => {
                    eprintln!("[lksr] script fire room_pcm ({label}): asset check");
                    let asset = Self::step_str(&step, "asset", "");
                    let gain = step.get("gain").and_then(|v| v.as_f64()).unwrap_or(1.0);
                    let rloop = Self::step_bool(&step, "loop", false);
                    let mut spec = serde_json::Map::new();
                    spec.insert("step_id".into(), json!(id));
                    spec.insert("label".into(), json!(label));
                    spec.insert("say".into(), json!(say));
                    spec.insert("asset".into(), json!(asset));
                    spec.insert("gain".into(), json!(gain));
                    w.emit(
                        "sim.script.cue",
                        Some(&spec),
                        "sim.script",
                        None,
                        None,
                        false,
                        None,
                    );
                    drop(w);
                    let _ = (self.on_action)(ScriptAction::RoomPcm {
                        asset,
                        gain,
                        r#loop: rloop,
                        label,
                    });
                }
                "dtmf" => {
                    let digits = Self::step_str(&step, "digits", "");
                    let mut spec = serde_json::Map::new();
                    spec.insert("step_id".into(), json!(id));
                    spec.insert("digits".into(), json!(digits));
                    w.emit(
                        "sim.script.dtmf",
                        Some(&spec),
                        "sim.script",
                        None,
                        None,
                        false,
                        None,
                    );
                    drop(w);
                    let _ = (self.on_action)(ScriptAction::Dtmf { digits });
                }
                "wait" => {
                    let mut spec = serde_json::Map::new();
                    spec.insert("step_id".into(), json!(id));
                    spec.insert("waited_ms".into(), json!(waited_ms));
                    w.emit(
                        "sim.script.wait",
                        Some(&spec),
                        "sim.script",
                        None,
                        None,
                        false,
                        None,
                    );
                    drop(w);
                    let _ = (self.on_action)(ScriptAction::Wait);
                }
                _ => {
                    // speak
                    if delivery == "room_pcm" {
                        let asset = Self::step_str(&step, "asset", "");
                        let gain = step.get("gain").and_then(|v| v.as_f64()).unwrap_or(1.0);
                        let rloop = Self::step_bool(&step, "loop", false);
                        let mut spec = serde_json::Map::new();
                        spec.insert("step_id".into(), json!(id));
                        spec.insert("label".into(), json!(label));
                        spec.insert("say".into(), json!(say));
                        spec.insert("asset".into(), json!(asset));
                        spec.insert("gain".into(), json!(gain));
                        spec.insert("delivery".into(), json!("room_pcm"));
                        w.emit(
                            "sim.script.cue",
                            Some(&spec),
                            "sim.script",
                            None,
                            None,
                            false,
                            None,
                        );
                        drop(w);
                        let _ = (self.on_action)(ScriptAction::RoomPcm {
                            asset,
                            gain,
                            r#loop: rloop,
                            label,
                        });
                        if once {
                            fired.push(id);
                        }
                        arm_idx += 1;
                        continue;
                    }
                    if barge_in {
                        // Typed interruption: the caller cut across the agent.
                        let mut ispec = serde_json::Map::new();
                        ispec.insert("by".into(), json!("sim"));
                        ispec.insert("barge_in".into(), json!(true));
                        ispec.insert(
                            "class".into(),
                            match &icls {
                                Some(c) => json!(c),
                                None => json!("correction"),
                            },
                        );
                        ispec.insert("step_id".into(), json!(id));
                        ispec.insert("label".into(), json!(label));
                        ispec.insert("say".into(), json!(say));
                        ispec.insert(
                            "note".into(),
                            json!("Script barge while agent was speaking (typed interruption)."),
                        );
                        w.emit(
                            "interruption",
                            Some(&ispec),
                            "sim.script",
                            None,
                            None,
                            false,
                            None,
                        );
                    }
                    let mut spec = serde_json::Map::new();
                    spec.insert("step_id".into(), json!(id));
                    spec.insert("label".into(), json!(label));
                    spec.insert("say".into(), json!(say));
                    spec.insert("trigger".into(), json!(trigger));
                    spec.insert("action".into(), json!("speak"));
                    spec.insert("barge_in".into(), json!(barge_in));
                    spec.insert(
                        "class".into(),
                        match &icls {
                            Some(c) => json!(c),
                            None => json!(null),
                        },
                    );
                    spec.insert("waited_ms".into(), json!(waited_ms));
                    w.emit(
                        "sim.script.cue",
                        Some(&spec),
                        "sim.script",
                        None,
                        None,
                        false,
                        None,
                    );
                    let step_mute = Self::step_bool(&step, "mute_persona", false);
                    drop(w);
                    if step_mute {
                        crate::callers::openai::MUTE_PERSONA_ACTIVE
                            .store(true, std::sync::atomic::Ordering::SeqCst);
                    }
                    // PERMANENT instrument (not temp-debug): the cue send
                    // is fire-and-forget (let _ =) — if the bridge never
                    // receives, the run dies silent until the slice cap with
                    // zero evidence. This event names the step that fired.
                    {
                        let mut w = self.writer.lock().await;
                        w.emit(
                            "sim.script.fired",
                            Some(
                                &serde_json::json!({"step_id": id, "label": label, "action": "speak"})
                                    .as_object()
                                    .cloned()
                                    .unwrap_or_default(),
                            ),
                            "sim.script",
                            None,
                            None,
                            false,
                            None,
                        );
                    }
                    let _ = (self.on_action)(ScriptAction::Speak {
                        text: say,
                        label,
                        barge_in,
                        interrupt_class: icls,
                        delivery,
                    });
                    // Speak steps open a reply window before the next step.
                    awaiting_reply_since = Some(Instant::now());
                    own_turn_fired = true;
                }
            }
            if once {
                fired.push(id);
            }
            arm_idx += 1;
        }
        Ok(())
    }

    /// Port of `driver.py::_run_behavior` (+ `caller_contract/validator.py`
    /// generate/validate retry, `text_backends.py` HTTP call): drive one
    /// `do:` step to SATISFIED or the single canonical failure exit
    /// (BEHAVIOR_TIMEOUT / CALLER_BEHAVIOR_VIOLATION / AGENT_TIMEOUT — all
    /// end the run here, mirroring Python's one-owner budget gate).
    async fn run_contract_do(
        &self,
        step: &serde_json::Value,
        id: &str,
        label: &str,
    ) -> Result<(), RunError> {
        let behavior = Self::step_str(step, "behavior", "");
        if behavior.trim().is_empty() {
            return Err(RunError(format!(
                "contract_do step {id:?}: missing behavior"
            )));
        }
        let target = step
            .get("target")
            .and_then(|v| v.as_str())
            .map(String::from);
        let constraints_raw = step
            .get("constraints")
            .and_then(|v| v.as_object())
            .cloned()
            .unwrap_or_default();
        // TODO(port): interaction.interruption_rate/interval_ms/seed (seeded
        // backchannel policy, interaction_planner.py) — parsed by caller_dsl
        // at scenario load, ignored here; not load-bearing for satisfaction.
        // TODO(port): forbidden-intent semantic check (needs
        // DEFAULT_INTENT_KEYWORDS + intent taxonomy) — the lexical check in
        // ContractValidator::validate already runs; the semantic-verifier
        // pass is skipped by passing `None` below (tier-1 rule lexicon only,
        // same as the lks Python side's live-run selection).
        let contract = lks_core::caller_contract::BehaviorContract {
            behavior: behavior.clone(),
            target: target.clone(),
            constraints: lks_core::contract_do::parse_do_constraints(Some(&constraints_raw)),
        };

        let mut validator = lks_core::caller_contract::ContractValidator::new(None);
        let mut orchestrator = lks_core::caller_contract::OrchestratorState::new();
        orchestrator.start_behavior();
        // Seed `log` with the full pre-`do:` dialogue replay (mirrors
        // Python passing the live `log` into `_run_behavior`): verbatim
        // `say:` lines + genuine agent replies the bridge pushed into the
        // shared TranscriptHistory. Without this the generator only ever
        // saw `agent_latest` (ONE line) and its own intra-`do:` turns —
        // no test-drive topic, no booking context, no name ("Alex" was
        // never in the prompt) — so `do:` candidates drifted off-topic
        // and burned the whole retry budget on LOW_CONFIDENCE (runs
        // 001-005 on the main-repo path). Capped at
        // DEFAULT_RECENT_TURNS_CAP exactly like build_context caps.
        let mut log: Vec<lks_core::contract_do::DoTurn> = self
            .history
            .snapshot()
            .into_iter()
            .rev()
            .take(lks_core::contract_do::DEFAULT_RECENT_TURNS_CAP)
            .rev()
            .map(|e| lks_core::contract_do::DoTurn {
                speaker: e.speaker,
                text: e.text,
            })
            .collect();
        let mut agent_latest: Option<String> = None;

        loop {
            if matches!(
                orchestrator.check_max_turns(contract.constraints.max_turns),
                lks_core::caller_contract::BehaviorOutcome::FailedMaxTurns
            ) {
                let reason = format!("behavior {behavior:?} unsatisfied after max_turns");
                self.fail_contract_do(id, label, &reason).await;
                return Err(RunError(reason));
            }
            let turn = log.len() as i64;
            let context = lks_core::contract_do::build_context(
                &contract,
                turn,
                agent_latest.as_deref(),
                &[],
                &log,
                lks_core::contract_do::DEFAULT_RECENT_TURNS_CAP,
            );

            // Bounded retry: generate -> validate (validator verdicts only
            // — a transport failure does not consume this budget in
            // Python, but this slice keeps it simple and counts it too,
            // since DEFAULT_MAX_RETRIES+1 attempts already bounds runtime).
            let mut candidate = None;
            let mut last_reason = String::from("no attempts");
            for attempt in 0..=lks_core::contract_do::DEFAULT_MAX_RETRIES {
                let identity = orchestrator.new_generation();
                // Provider-aware text backend (mirrors Python
                // `live_wiring.py::_build_text_backend`): google/Gemini via
                // generateContent, everything else via OpenAI chat-completions.
                let attempt_result = if self.do_provider.trim().to_lowercase() == "google" {
                    lks_core::contract_do::generate_do_candidate_gemini(
                        &self.do_api_key,
                        lks_core::contract_do::DEFAULT_GEMINI_BASE_URL,
                        lks_core::contract_do::DEFAULT_GEMINI_MODEL,
                        lks_core::contract_do::DEFAULT_TEMPERATURE,
                        lks_core::contract_do::DEFAULT_TIMEOUT_S,
                        &context,
                        identity,
                    )
                    .await
                } else {
                    lks_core::contract_do::generate_do_candidate(
                        &self.do_api_key,
                        "https://api.openai.com/v1",
                        lks_core::contract_do::DEFAULT_MODEL,
                        lks_core::contract_do::DEFAULT_TEMPERATURE,
                        lks_core::contract_do::DEFAULT_TIMEOUT_S,
                        &context,
                        identity,
                    )
                    .await
                };
                let cand = match attempt_result {
                    Ok(c) => c,
                    Err(e) => {
                        last_reason = format!("LANGUAGE_GENERATION_ERROR: {e}");
                        continue;
                    }
                };
                let verdict = validator.validate(&cand, &contract);
                {
                    let mut w = self.writer.lock().await;
                    // Include the attempted utterance (truncated): run 009
                    // proved a verdict-only trail is not diagnosable — the
                    // same behavior can VALID once then MISMATCH 3×, and
                    // without the text there is no way to know what the
                    // model generated on the failing attempts (generator
                    // drift vs lexicon gap — opposite fixes).
                    let attempted: String = cand.utterance.chars().take(160).collect();
                    w.emit(
                        "contract.attempt_verdict",
                        Some(
                            &json!({
                                "behavior": behavior.clone(),
                                "verdict": verdict.verdict.as_str(),
                                "reason": verdict.reason.clone(),
                                "attempt": attempt,
                                "utterance": attempted,
                            })
                            .as_object()
                            .cloned()
                            .unwrap_or_default(),
                        ),
                        "sim.script",
                        None,
                        None,
                        false,
                        None,
                    );
                }
                if verdict.is_valid() {
                    candidate = Some(cand);
                    break;
                }
                last_reason = verdict.reason.unwrap_or_else(|| "INVALID".to_string());
            }
            let Some(candidate) = candidate else {
                let reason = last_reason;
                {
                    let mut w = self.writer.lock().await;
                    w.emit(
                        "contract.behavior_violation",
                        Some(
                            &json!({"behavior": behavior.clone(), "reason": reason.clone()})
                                .as_object()
                                .cloned()
                                .unwrap_or_default(),
                        ),
                        "sim.script",
                        None,
                        None,
                        false,
                        None,
                    );
                }
                self.fail_contract_do(id, label, &reason).await;
                return Err(RunError(format!("do: {behavior:?}: {reason}")));
            };

            // Publish exactly the validated string via the existing Speak
            // cue path (never a planner-reshaped variant) — reuses
            // `on_action`/cue_tx, no new delivery machinery.
            {
                let mut w = self.writer.lock().await;
                w.emit(
                    "contract.turn_published",
                    Some(
                        &json!({
                            "behavior": behavior.clone(),
                            "turn": turn,
                            "text": candidate.utterance.clone(),
                        })
                        .as_object()
                        .cloned()
                        .unwrap_or_default(),
                    ),
                    "sim.script",
                    None,
                    None,
                    false,
                    None,
                );
            }
            let text = candidate.utterance.clone();
            let _ = (self.on_action)(ScriptAction::Speak {
                text: text.clone(),
                label: format!("do:{behavior}"),
                barge_in: false,
                interrupt_class: None,
                delivery: "gemini_text".to_string(),
            });
            log.push(lks_core::contract_do::DoTurn {
                speaker: "caller".to_string(),
                text: text.clone(),
            });
            orchestrator.advance_behavior_turn();

            // Wait for the agent's NEXT final (AGENT_FINAL_SEQ must advance
            // past the baseline captured before publish, so a stale final
            // from before this turn is never mistaken for the reply) —
            // bounded, mirrors `_wait_agent_turn_with_policy(timeout_s=30.0)`.
            //
            // BUG FIX (run 006-dealer-live-full): the agent's transcription
            // stream emits multiple `final_=true` TextStream segments per
            // spoken turn (sentence/clause fragments), each bumping
            // AGENT_FINAL_SEQ — see callers/openai.rs TextStream arms. Taking
            // only the FIRST fragment ("Sure. We're available from about")
            // meant `evaluate_behavior` never saw the actual answer (dates)
            // that arrived in a later fragment of the SAME turn, so the
            // do-driver looped through 5 near-identical re-asks and the
            // agent, hearing itself interrupted/re-asked mid-sentence every
            // time, hung up thinking the connection was broken. Concatenate
            // every fragment that lands until AGENT_FINAL_SEQ goes quiet for
            // `quiet_window` — a lightweight port of the observer's
            // transcript_dedupe_window_ms merge, scoped to this wait only.
            let quiet_window = Duration::from_millis(900);
            let baseline =
                crate::callers::openai::AGENT_FINAL_SEQ.load(std::sync::atomic::Ordering::SeqCst);
            let mut last_seq = baseline;
            let mut collected = String::new();
            let mut quiet_deadline: Option<Instant> = None;
            let deadline = Instant::now() + Duration::from_secs(30);
            // Port of driver.py's post-publish agent-gone gate (runs
            // 054/055) + agent_wait.py's wait-entry fast-path (run 083):
            // when the agent already left, skip the wait AND the stale
            // pre-drain final entirely (None) so the caller below takes
            // the agent-ended path instead of recycling a dead reply.
            // Otherwise poll the wait; a mid-wait disconnect also ends it
            // early with whatever fragments arrived (None if none).
            let agent_text: Option<String> = if crate::callers::openai::is_agent_gone() {
                None
            } else {
                loop {
                    // Agent left mid-call (end_call tool → disconnect):
                    // stop polling a dead room — the caller maps None to
                    // the agent-ended path, never a timeout + budget burn.
                    if crate::callers::openai::is_agent_gone() {
                        break if collected.is_empty() {
                            None
                        } else {
                            Some(collected)
                        };
                    }
                    let seq = crate::callers::openai::AGENT_FINAL_SEQ
                        .load(std::sync::atomic::Ordering::SeqCst);
                    if seq > last_seq {
                        last_seq = seq;
                        let frag = crate::callers::openai::AGENT_FINAL_TEXT.lock().clone();
                        let frag = frag.trim();
                        if !frag.is_empty() {
                            if !collected.is_empty() {
                                collected.push(' ');
                            }
                            collected.push_str(frag);
                        }
                        quiet_deadline = Some(Instant::now() + quiet_window);
                    }
                    if let Some(qd) = quiet_deadline {
                        if Instant::now() >= qd {
                            break Some(collected);
                        }
                    }
                    if Instant::now() >= deadline {
                        break if collected.is_empty() {
                            None
                        } else {
                            Some(collected)
                        };
                    }
                    tokio::time::sleep(Duration::from_millis(50)).await;
                }
            };
            let Some(agent_text) = agent_text else {
                // Agent gone mid-call (end_call tool → disconnect): the call
                // is over by the AGENT's hand, not a caller violation and
                // not a timeout to retry — end cleanly as agent-ended (runs
                // 054/055 class burned the whole behavior budget polling a
                // dead room into FAILED_MAX_TURNS instead). Only fall
                // through to AGENT_TIMEOUT when the agent is NOT gone
                // (still just slow).
                if crate::callers::openai::is_agent_gone() {
                    {
                        let mut w = self.writer.lock().await;
                        w.emit(
                            "contract.agent_ended",
                            Some(
                                &json!({"behavior": behavior, "turn": turn})
                                    .as_object()
                                    .cloned()
                                    .unwrap_or_default(),
                            ),
                            "sim.script",
                            None,
                            None,
                            false,
                            None,
                        );
                    }
                    let _ = self.end_tx.send(());
                    return Ok(());
                }
                let reason = format!("agent did not reply to behavior {behavior:?} within timeout");
                {
                    let mut w = self.writer.lock().await;
                    w.emit(
                        "contract.agent_timeout",
                        Some(
                            &json!({"behavior": behavior, "turn": turn})
                                .as_object()
                                .cloned()
                                .unwrap_or_default(),
                        ),
                        "sim.script",
                        None,
                        None,
                        false,
                        None,
                    );
                }
                self.fail_contract_do(id, label, &reason).await;
                return Err(RunError(reason));
            };
            log.push(lks_core::contract_do::DoTurn {
                speaker: "agent".to_string(),
                text: agent_text.clone(),
            });
            agent_latest = Some(agent_text.clone());

            let verdict = lks_core::caller_contract::evaluate_behavior(
                &behavior,
                target.as_deref(),
                &agent_text,
            );
            if matches!(
                verdict,
                lks_core::caller_contract::EvaluatorVerdict::Satisfied
            ) {
                return Ok(());
            }
            // End-behavior escape (port of driver.py run-061 gate): the
            // call is already socially over — the agent answered the end
            // turn with a CLOSING turn (goodbye, you're-welcome,
            // thank-you, glad-it's-sorted) instead of new content. A
            // closing agent reply satisfies the end behavior; non-closing
            // replies (questions, new info, forward offers) still loop
            // the budget as before.
            if lks_core::caller_contract::is_end_behavior_closing_reply(&behavior, &agent_text) {
                {
                    let mut w = self.writer.lock().await;
                    w.emit(
                        "contract.agent_ended",
                        Some(
                            &json!({"behavior": behavior, "turn": turn})
                                .as_object()
                                .cloned()
                                .unwrap_or_default(),
                        ),
                        "sim.script",
                        None,
                        None,
                        false,
                        None,
                    );
                }
                return Ok(());
            }
            // Not satisfied: loop back to check_max_turns at the top — the
            // ONLY exit for budget exhaustion (mirrors driver.py's single
            // canonical BEHAVIOR_TIMEOUT funnel, no inline range bound).
        }
    }

    /// Emit the failure event + end the run (port of driver.py's single
    /// canonical BEHAVIOR_TIMEOUT/CALLER_BEHAVIOR_VIOLATION/AGENT_TIMEOUT
    /// exit funnel). Sets `CONTRACT_DO_FAILED` so `run.rs` can flip a
    /// "done" status to "failed" even though the script task's JoinHandle
    /// result is otherwise discarded on abort.
    async fn fail_contract_do(&self, id: &str, label: &str, reason: &str) {
        CONTRACT_DO_FAILED.store(true, std::sync::atomic::Ordering::SeqCst);
        *CONTRACT_DO_FAILURE_REASON.lock() = reason.to_string();
        {
            let mut w = self.writer.lock().await;
            w.emit(
                "sim.script.contract_do_failed",
                Some(
                    &json!({"step_id": id, "label": label, "reason": reason})
                        .as_object()
                        .cloned()
                        .unwrap_or_default(),
                ),
                "sim.script",
                None,
                None,
                false,
                None,
            );
        }
        let _ = self.end_tx.send(());
    }
}

/// Set by `ScriptRuntime::fail_contract_do` on any `do:` failure exit
/// (BEHAVIOR_TIMEOUT / CALLER_BEHAVIOR_VIOLATION / AGENT_TIMEOUT). The
/// script task's `JoinHandle` result is discarded on abort in `run.rs`, so
/// this pair of statics is the signal `run.rs` checks to flip a "done"
/// status to "failed" after the bridge exits.
pub static CONTRACT_DO_FAILED: std::sync::atomic::AtomicBool =
    std::sync::atomic::AtomicBool::new(false);
pub static CONTRACT_DO_FAILURE_REASON: parking_lot::Mutex<String> =
    parking_lot::Mutex::new(String::new());

/// Shared mic-source handle so the script runtime can play room_pcm cues.
pub type SharedMicSource = Arc<tokio::sync::Mutex<Option<Arc<NativeAudioSource>>>>;

/// Shared conversation recorder (sim+agent audio → conversation.wav).
pub type SharedRecorder = Arc<std::sync::Mutex<crate::audio::LocalConversationRecorder>>;

/// Play raw PCM16 samples into the mic source in ~10 ms frames (24 kHz mono).
pub async fn play_pcm_to_source(
    source: &SharedMicSource,
    samples: &[i16],
    sample_rate: u32,
) -> Result<(), String> {
    let guard = source.lock().await;
    let Some(src) = guard.as_ref() else {
        return Err("sim mic not published yet — cannot play room_pcm cue".into());
    };
    let src: &NativeAudioSource = src;
    if sample_rate != src.sample_rate() {
        return Err(format!(
            "room_pcm asset rate {sample_rate} != sim mic {} (resample cue WAV)",
            src.sample_rate()
        ));
    }
    let frame_len = (sample_rate as usize) / 100;
    for chunk in samples.chunks(frame_len) {
        let mut af =
            livekit::webrtc::audio_frame::AudioFrame::new(sample_rate, 1, chunk.len() as u32);
        af.data = std::borrow::Cow::Owned(chunk.to_vec());
        src.capture_frame(&af)
            .await
            .map_err(|e| format!("capture_frame: {e}"))?;
    }
    Ok(())
}
