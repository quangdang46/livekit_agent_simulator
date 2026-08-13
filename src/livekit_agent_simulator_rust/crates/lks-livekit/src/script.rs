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

use lks_core::errors::RunError;
use lks_core::logging::event::EventWriter;
use serde_json::json;
use tokio::sync::Mutex;

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
}

pub struct ScriptRuntime {
    steps: Vec<serde_json::Value>,
    writer: Arc<Mutex<EventWriter>>,
    state: Arc<Mutex<ScriptObserverState>>,
    /// Sender used to signal hang_up → end the run.
    end_tx: tokio::sync::broadcast::Sender<()>,
    /// Callback executed for each fired action (the bridge implements it).
    on_action: Box<dyn Fn(ScriptAction) -> Result<(), String> + Send + Sync>,
}

impl ScriptRuntime {
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        steps: Vec<serde_json::Value>,
        writer: Arc<Mutex<EventWriter>>,
        state: Arc<Mutex<ScriptObserverState>>,
        end_tx: tokio::sync::broadcast::Sender<()>,
        on_action: Box<dyn Fn(ScriptAction) -> Result<(), String> + Send + Sync>,
    ) -> Self {
        Self {
            steps,
            writer,
            state,
            end_tx,
            on_action,
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
                    drop(w);
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
