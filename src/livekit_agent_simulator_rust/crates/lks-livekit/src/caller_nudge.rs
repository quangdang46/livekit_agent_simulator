//! Caller nudge (port of `caller_nudge.py`) — when first_speaker is agent,
//! persona-only runs stall without a text bootstrap. This activates the caller
//! via response.create (the nudge text is NEVER spoken into the room).

use std::sync::Arc;
use tokio::sync::{broadcast, Mutex};

use lks_core::logging::event::EventWriter;

pub const AGENT_GREETED_NUDGE: &str =
    "(The agent has finished greeting you. Respond now in the language of your persona.)";

/// Nudge the caller after the agent's greeting (non-audible activation).
#[allow(clippy::too_many_arguments)]
pub async fn nudge_caller_after_agent_greeting(
    writer: Arc<Mutex<EventWriter>>,
    end_call: broadcast::Receiver<()>,
    on_nudge: impl Fn(&str) -> Result<(), String>,
    agent_has_spoken: impl Fn() -> bool + Send + 'static,
    user_has_spoken: impl Fn() -> bool + Send + 'static,
    first_speaker: &str,
    silent_mode: bool,
    debounce_s: f64,
    poll_s: f64,
) {
    if first_speaker != "agent" {
        return;
    }
    if silent_mode {
        let mut w = writer.lock().await;
        w.emit(
            "sim.agent_greeted_nudge_skipped",
            Some(
                &serde_json::json!({"reason": "silent_mode"})
                    .as_object()
                    .cloned()
                    .unwrap_or_default(),
            ),
            "sim",
            None,
            None,
            false,
            None,
        );
        return;
    }
    let mut end_call = end_call;
    let mut nudged = false;
    while !nudged && end_call.try_recv().is_err() {
        if agent_has_spoken() && !user_has_spoken() {
            tokio::time::sleep(std::time::Duration::from_secs_f64(debounce_s)).await;
            if end_call.try_recv().is_ok() || user_has_spoken() {
                return;
            }
            match on_nudge(AGENT_GREETED_NUDGE) {
                Ok(()) => {
                    let mut w = writer.lock().await;
                    w.emit(
                        "sim.agent_greeted_nudge",
                        Some(
                            &serde_json::json!({"debounce_s": debounce_s, "audible": false})
                                .as_object()
                                .cloned()
                                .unwrap_or_default(),
                        ),
                        "sim",
                        None,
                        None,
                        false,
                        None,
                    );
                    nudged = true;
                }
                Err(_) => {
                    tokio::time::sleep(std::time::Duration::from_secs_f64(poll_s)).await;
                }
            }
        } else {
            tokio::time::sleep(std::time::Duration::from_secs_f64(poll_s)).await;
        }
    }
}
