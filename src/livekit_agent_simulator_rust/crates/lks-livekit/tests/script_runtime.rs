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

#[tokio::test]
async fn room_pcm_plays_into_source() {
    // Build a tiny 24 kHz mono WAV, read it, and play it into a shared source.
    let dir = tempfile::tempdir().unwrap();
    let wav = dir.path().join("cue.wav");
    let spec = hound::WavSpec {
        channels: 1,
        sample_rate: 24_000,
        bits_per_sample: 16,
        sample_format: hound::SampleFormat::Int,
    };
    {
        let mut w = hound::WavWriter::create(&wav, spec).unwrap();
        for i in 0..4800 {
            let v = ((i as f32 * 0.1).sin() * 1000.0) as i16;
            w.write_sample(v).unwrap();
        }
    }
    let mut reader = hound::WavReader::open(&wav).unwrap();
    let samples: Vec<i16> = reader.samples::<i16>().filter_map(Result::ok).collect();
    assert_eq!(samples.len(), 4800);
    assert_eq!(reader.spec().sample_rate, 24_000);

    let shared: lks_livekit::script::SharedMicSource = Arc::new(TokioMutex::new(None));
    // Without a published source → error (not crash).
    let err = lks_livekit::script::play_pcm_to_source(&shared, &samples, 24_000).await;
    assert!(err.is_err(), "no source → error");

    // With a real NativeAudioSource → frames pushed (capture_frame OK).
    use livekit::webrtc::audio_source::native::NativeAudioSource;
    use livekit::webrtc::prelude::*;
    let source = NativeAudioSource::new(AudioSourceOptions::default(), 24_000, 1, 1000);
    {
        let mut guard = shared.lock().await;
        *guard = Some(Arc::new(source));
    }
    let r = lks_livekit::script::play_pcm_to_source(&shared, &samples, 24_000).await;
    assert!(r.is_ok(), "playback succeeded: {r:?}");
}

#[test]
fn recorder_writes_stereo_wav() {
    let dir = tempfile::tempdir().unwrap();
    let mut rec = lks_livekit::audio::LocalConversationRecorder::new();
    rec.mark_start();
    // 24k mono sim frames + agent frames → resampled into 16k stereo.
    let sim: Vec<u8> = (0..4800i16).flat_map(|i| i.to_le_bytes()).collect();
    let agent: Vec<u8> = (0..4800i16)
        .map(|i| -i)
        .flat_map(|i| i.to_le_bytes())
        .collect();
    rec.push_sim(&sim, 24_000);
    rec.push_agent(&agent, 24_000);
    let path = dir.path().join("conversation.wav");
    let res = rec.save(&path).unwrap();
    assert!(res.duration_ms > 0);
    let mut reader = hound::WavReader::open(&path).unwrap();
    let spec = reader.spec();
    assert_eq!(spec.channels, 2, "stereo L=sim R=agent");
    assert_eq!(spec.sample_rate, 16_000);
    let frames: Vec<i16> = reader.samples::<i16>().filter_map(Result::ok).collect();
    assert!(frames.len() >= 2, "interleaved stereo samples");
}
