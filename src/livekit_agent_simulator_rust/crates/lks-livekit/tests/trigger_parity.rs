//! Trigger-loop parity with the Python contract driver (bead v2-31).
//!
//! Two divergences lived in the LIVE trigger loop, so "parity means wiring
//! dead code" did not dispose of them:
//!
//! 1. GAP TOLERANCE. Python holds the continuity clock across a short VAD
//!    dropout and only disarms once the gap reaches TRIGGER_GAP_TOLERANCE_S
//!    (driver.py:1283-1288). Rust reset on a SINGLE inactive sample, so a
//!    trigger Python fires reliably could silently never fire under lksr.
//!
//! 2. WAIT BUDGET. Python bounds a trigger wait with TRIGGER_WAIT_BUDGET_S
//!    (driver.py:45) and turns expiry into BEHAVIOR_TIMEOUT. Rust had no
//!    wall-clock deadline at all: a never-satisfied trigger parked the step
//!    indefinitely. A hang leaves no report; a failed trigger leaves a red
//!    one.
//!
//! Timing, so the driver is the REAL one and the waits are real. Nothing here
//! is mocked, because a mocked clock would not have caught either divergence:
//! the original bug was a missing accumulator and a missing deadline, both of
//! which a mock happily satisfies.

use lks_core::logging::event::EventWriter;
use lks_livekit::script::{ScriptAction, ScriptObserverState, ScriptRuntime, TranscriptHistory};
use serde_json::json;
use std::sync::{Arc, Mutex as StdMutex};
use tokio::sync::{broadcast, Mutex as TokioMutex};

fn temp_writer() -> (tempfile::TempDir, Arc<TokioMutex<EventWriter>>) {
    let dir = tempfile::tempdir().unwrap();
    let writer = EventWriter::new("test-run", dir.path().to_path_buf(), "UTC", 2500).unwrap();
    (dir, Arc::new(TokioMutex::new(writer)))
}

type Fired = Arc<StdMutex<Vec<String>>>;

#[allow(clippy::too_many_arguments)]
fn spawn_runtime(
    steps: Vec<serde_json::Value>,
    writer: Arc<TokioMutex<EventWriter>>,
    state: Arc<TokioMutex<ScriptObserverState>>,
    first_speaker: &str,
) -> (
    broadcast::Sender<()>,
    broadcast::Receiver<()>,
    Fired,
    tokio::task::JoinHandle<Result<(), lks_core::errors::RunError>>,
) {
    let (end_tx, end_rx) = broadcast::channel::<()>(1);
    let fired: Fired = Arc::new(StdMutex::new(Vec::new()));
    let fired2 = fired.clone();
    let runtime = ScriptRuntime::new(
        steps,
        writer,
        state,
        end_tx.clone(),
        Box::new(move |action| {
            if let ScriptAction::Speak { text, .. } = action {
                fired2.lock().unwrap().push(text);
            }
            Ok(())
        }),
        "en".into(),
        "test-api-key".into(),
        "openai".into(),
        first_speaker.into(),
        Arc::new(TranscriptHistory::new()),
    );
    let rx = end_rx.resubscribe();
    let task = tokio::spawn(async move { runtime.run(rx).await });
    (end_tx, end_rx, fired, task)
}

/// A dropout shorter than the 1.2s tolerance must not disarm the trigger.
///
/// The clock is what distinguishes the two behaviours, so the assertion is
/// about WHEN, not merely whether it eventually fires. With the fix the
/// continuity clock starts at t=0 and survives the gap, so the cue fires as
/// soon as the agent is audible again (~700ms). Under the old hard reset the
/// clock restarted at t=700 and the cue could not fire before ~1200ms — so at
/// the 1000ms mark the two implementations differ observably.
#[tokio::test]
async fn a_short_dropout_does_not_disarm_the_trigger() {
    let (_d, writer) = temp_writer();
    let state = Arc::new(TokioMutex::new(ScriptObserverState::default()));
    let (_tx, _rx, fired, task) = spawn_runtime(
        vec![json!({
            "id": "open",
            "trigger": "agent_speaking",
            "min_agent_active_ms": 500,
            "delay_ms": 0,
            "say": "still armed",
            "action": "speak",
        })],
        writer,
        state.clone(),
        "agent",
    );

    state.lock().await.agent_is_active_speaker = true;
    // 300ms dropout — well inside the 1.2s tolerance.
    tokio::time::sleep(std::time::Duration::from_millis(400)).await;
    state.lock().await.agent_is_active_speaker = false;
    tokio::time::sleep(std::time::Duration::from_millis(300)).await;
    state.lock().await.agent_is_active_speaker = true;

    tokio::time::sleep(std::time::Duration::from_millis(300)).await;
    assert_eq!(
        fired.lock().unwrap().len(),
        1,
        "a sub-tolerance dropout must not reset the continuity clock"
    );
    task.abort();
}

/// The mirror image, and the reason the tolerance is not "never reset": a gap
/// LONGER than 1.2s does disarm the trigger. Without this the fix would be an
/// unbounded latch — a trigger that can never be disarmed is its own hang.
#[tokio::test]
async fn a_long_dropout_still_disarms_the_trigger() {
    let (_d, writer) = temp_writer();
    let state = Arc::new(TokioMutex::new(ScriptObserverState::default()));
    let (_tx, _rx, fired, task) = spawn_runtime(
        vec![json!({
            "id": "open",
            "trigger": "agent_speaking",
            "min_agent_active_ms": 300,
            "delay_ms": 0,
            "say": "should not fire",
            "action": "speak",
        })],
        writer,
        state.clone(),
        "agent",
    );

    state.lock().await.agent_is_active_speaker = true;
    tokio::time::sleep(std::time::Duration::from_millis(200)).await;
    // 1.5s gap — past the 1.2s tolerance.
    state.lock().await.agent_is_active_speaker = false;
    tokio::time::sleep(std::time::Duration::from_millis(1500)).await;
    state.lock().await.agent_is_active_speaker = true;

    tokio::time::sleep(std::time::Duration::from_millis(200)).await;
    assert_eq!(
        fired.lock().unwrap().len(),
        0,
        "a gap past the tolerance must reset the clock, not extend it"
    );
    task.abort();
}

/// A never-satisfied trigger must FAIL, not park. Python's bound is 30.0s
/// (driver.py:45), and 30 is the contract rather than an implementation
/// detail — so this asserts the window, not merely that some timeout exists.
///
/// Slow by design: the whole point is the wall-clock number.
#[tokio::test]
async fn a_never_satisfied_trigger_fails_at_the_python_budget() {
    let (_d, writer) = temp_writer();
    let state = Arc::new(TokioMutex::new(ScriptObserverState::default()));
    let (_tx, _rx, fired, task) = spawn_runtime(
        vec![json!({
            "id": "never",
            "trigger": "agent_speaking",
            "min_agent_active_ms": 3_600_000,
            "delay_ms": 0,
            "say": "unreachable",
            "action": "speak",
        })],
        writer,
        state.clone(),
        "agent",
    );
    // The agent NEVER becomes an active speaker.

    let started = std::time::Instant::now();
    let outcome = tokio::time::timeout(std::time::Duration::from_secs(45), task)
        .await
        .expect("run() must not hang past the budget")
        .expect("task must not panic");
    let elapsed = started.elapsed();

    assert!(
        outcome.is_err(),
        "a trigger that never fires must end the run, not return Ok"
    );
    let msg = outcome.unwrap_err().0;
    assert!(
        msg.contains("never fired"),
        "the error must say the trigger never fired, got: {msg}"
    );
    assert_eq!(
        fired.lock().unwrap().len(),
        0,
        "nothing may be published when the trigger never satisfies"
    );
    // Band, not equality: 30s is the contract, and a test that demands exactly
    // 30000ms would fail on a loaded machine for no good reason.
    assert!(
        elapsed >= std::time::Duration::from_secs(29)
            && elapsed <= std::time::Duration::from_secs(38),
        "expected the ~30s Python budget, took {elapsed:?}"
    );
}
