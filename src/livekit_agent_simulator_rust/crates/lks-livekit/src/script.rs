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
    locale: String,
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
    ) -> Self {
        Self {
            steps,
            writer,
            state,
            end_tx,
            on_action,
            defer_state: parking_lot::Mutex::new(None),
            locale,
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
        let mut stop_rx = stop_rx;

        while arm_idx < self.steps.len() {
            tokio::select! {
                _ = stop_rx.recv() => return Ok(()),
                _ = tokio::time::sleep(Duration::from_millis(50)) => {}
            }
            // Clear mute_persona when the loop re-arms.
            crate::callers::openai::MUTE_PERSONA_ACTIVE.store(false, std::sync::atomic::Ordering::SeqCst);
            // Post-cue gap: after a speak step, wait for the agent to reply
            // (up to 8s) before arming the next step — mirrors the Python
            // _await_agent_reply window so steps don't fire over the agent.
            if let Some(since) = awaiting_reply_since {
                let state = self.state.lock().await;
                let replied = state.agent_replied_this_turn || !state.agent_is_active_speaker;
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
            let state = self.state.lock().await;
            let active = match trigger.as_str() {
                "time" => true,
                "silence" => {
                    if Self::step_bool(&step, "require_agent_spoke_first", true)
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

            let mut w = self.writer.lock().await;
            match action.as_str() {
                "hang_up" => {
                    // ── Hang-up deferral (port of runtime.py _hang_up_ready) ──
                    let require_reply = Self::step_bool(&step, "require_agent_reply_this_turn", true);
                    let defer_open = Self::step_bool(&step, "defer_on_open_question", true);
                    let budget_ms = { let raw = Self::step_i64(&step, "open_question_idle_ms"); if raw > 0 { raw } else { 20000 } };
                    let state_snapshot = self.state.lock().await;
                    let user_spoke = state_snapshot.user_has_spoken && !state_snapshot.agent_replied_this_turn;
                    let open_question = defer_open
                        && lks_core::script::hang_up_gate::agent_left_open_turn(Some(&state_snapshot.last_agent_final_text));
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
                                    ds_spec.insert("reason".into(), json!("defer_budget_exhausted"));
                                    ds_spec.insert("prior_reason".into(), json!(d.prior_reason));
                                    ds_spec.insert("deferred_ms".into(), json!(deferred_ms));
                                    ds_spec.insert("budget_ms".into(), json!(budget_ms));
                                    ds_spec.insert("last_agent_final".into(), json!(state_snapshot.last_agent_final_text[..state_snapshot.last_agent_final_text.len().min(240)]));
                                    drop(state_snapshot);
                                    w.emit("sim.script.hang_up_deferred", Some(&ds_spec), "sim.script", None, None, false, None);
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
                                ds_spec.insert("last_agent_final".into(), json!(state_snapshot.last_agent_final_text[..state_snapshot.last_agent_final_text.len().min(240)]));
                                drop(state_snapshot);
                                w.emit("sim.script.hang_up_deferred", Some(&ds_spec), "sim.script", None, None, false, None);
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
                    w.emit("sim.script.hang_up", Some(&spec), "sim.script", None, None, false, None);
                    w.emit("sim.hang_up", Some(&spec), "sim.script", None, None, false, None);
                    drop(w);
                    let _ = (self.on_action)(ScriptAction::HangUp { farewell, label });
                    let _ = self.end_tx.send(());
                    return Ok(());
                }
                "room_pcm" => {
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
                        crate::callers::openai::MUTE_PERSONA_ACTIVE.store(true, std::sync::atomic::Ordering::SeqCst);
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
                }
            }
            if once {
                fired.push(id);
            }
            arm_idx += 1;
        }
        Ok(())
    }
}

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
