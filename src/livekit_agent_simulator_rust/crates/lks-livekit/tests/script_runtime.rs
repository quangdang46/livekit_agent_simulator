//! Script runtime tests (offline) — trigger gating + action dispatch.
//! The action callback is SYNC (runs inside the runtime loop), so it uses a
//! std::sync::Mutex for captured state — never tokio locks.

use lks_core::logging::event::EventWriter;
use lks_livekit::script::{ScriptAction, ScriptObserverState, ScriptRuntime};
use serde_json::json;
use std::sync::{Arc, Mutex as StdMutex};
use tokio::sync::{broadcast, Mutex as TokioMutex};

fn temp_writer() -> (tempfile::TempDir, Arc<TokioMutex<EventWriter>>) {
    let dir = tempfile::tempdir().unwrap();
    let writer = EventWriter::new("test-run", dir.path().to_path_buf(), "UTC", 2500).unwrap();
    (dir, Arc::new(TokioMutex::new(writer)))
}

#[tokio::test]
async fn time_trigger_fires_speak_after_delay() {
    let (_d, writer) = temp_writer();
    let state = Arc::new(TokioMutex::new(ScriptObserverState::default()));
    let (end_tx, end_rx) = broadcast::channel::<()>(1);
    let fired: Arc<StdMutex<Vec<String>>> = Arc::new(StdMutex::new(Vec::new()));
    let fired2 = fired.clone();
    let steps = vec![json!({
        "id": "open",
        "trigger": "time",
        "delay_ms": 200,
        "say": "Hello there",
        "action": "speak",
    })];
    let runtime = ScriptRuntime::new(
        steps,
        writer,
        state,
        end_tx,
        Box::new(move |action| {
            if let ScriptAction::Speak { text, .. } = action {
                fired2.lock().unwrap().push(text);
            }
            Ok(())
        }),
    );
    let started = std::time::Instant::now();
    runtime.run(end_rx).await.unwrap();
    assert!(started.elapsed().as_millis() >= 150, "delay honored");
    assert_eq!(*fired.lock().unwrap(), vec!["Hello there".to_string()]);
}

#[tokio::test]
async fn silence_trigger_waits_for_agent_to_speak_first() {
    let (_d, writer) = temp_writer();
    let state = Arc::new(TokioMutex::new(ScriptObserverState::default()));
    let (end_tx, end_rx) = broadcast::channel::<()>(1);
    let fired: Arc<StdMutex<usize>> = Arc::new(StdMutex::new(0));
    let fired2 = fired.clone();
    let steps = vec![json!({
        "id": "open",
        "trigger": "silence",
        "delay_ms": 100,
        "say": "hi",
        "require_agent_spoke_first": true,
    })];
    let runtime = ScriptRuntime::new(
        steps,
        writer,
        state.clone(),
        end_tx,
        Box::new(move |action| {
            if let ScriptAction::Speak { .. } = action {
                *fired2.lock().unwrap() += 1;
            }
            Ok(())
        }),
    );
    // Spawn the runtime; agent hasn't spoken → silence trigger must not fire.
    let rx = end_rx.resubscribe();
    let task = tokio::spawn(async move { runtime.run(rx).await });
    tokio::time::sleep(std::time::Duration::from_millis(300)).await;
    assert_eq!(
        *fired.lock().unwrap(),
        0,
        "must not fire before agent speaks"
    );
    // Now mark the agent as having spoken + not active → fires.
    {
        let mut s = state.lock().await;
        s.agent_has_spoken = true;
        s.agent_is_active_speaker = false;
    }
    tokio::time::sleep(std::time::Duration::from_millis(300)).await;
    assert_eq!(
        *fired.lock().unwrap(),
        1,
        "fires once agent spoke and is silent"
    );
    task.abort();
}

#[tokio::test]
async fn hang_up_ends_run() {
    let (_d, writer) = temp_writer();
    let state = Arc::new(TokioMutex::new(ScriptObserverState::default()));
    let (end_tx, end_rx) = broadcast::channel::<()>(1);
    let ended: Arc<StdMutex<bool>> = Arc::new(StdMutex::new(false));
    let ended2 = ended.clone();
    let steps = vec![json!({
        "id": "bye",
        "trigger": "time",
        "delay_ms": 100,
        "action": "hang_up",
        "say": "Bye now",
    })];
    let runtime = ScriptRuntime::new(
        steps,
        writer,
        state,
        end_tx.clone(),
        Box::new(move |action| {
            if let ScriptAction::HangUp { farewell, .. } = action {
                assert_eq!(farewell, "Bye now");
                *ended2.lock().unwrap() = true;
            }
            Ok(())
        }),
    );
    let _ = end_tx;
    runtime.run(end_rx).await.unwrap();
    assert!(*ended.lock().unwrap(), "hang_up action fired");
}
