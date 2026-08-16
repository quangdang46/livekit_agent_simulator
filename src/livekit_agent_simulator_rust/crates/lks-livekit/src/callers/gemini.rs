//! Gemini Live caller bridge (port of `callers/gemini.py` slice).
//!
//! Same structure as the OpenAI bridge: connect room → dispatch agent → open a
//! Gemini Live session (24 kHz PCM in/out, VAD off, input/output transcription
//! on) → pump agent room audio into the session, model audio into the mic.

use std::sync::Arc;

use futures_util::StreamExt;
use gemini_live::{
    transport::{Auth, TransportConfig},
    AudioTranscriptionConfig, AutomaticActivityDetection, GenerationConfig, Modality, Part,
    PrebuiltVoiceConfig, RealtimeInputConfig, ServerEvent, Session, SessionConfig, SetupConfig,
    SpeechConfig, VoiceConfig,
};
use tokio::sync::{broadcast, mpsc, Mutex};

use lks_core::config::{LiveKitConfig, SimulatorConfig};
use lks_core::errors::RunError;
use lks_core::logging::event::EventWriter;

use crate::room::{connect_room, make_token, SimRoomEvent};

pub const GEMINI_RATE: u32 = 24_000;

pub struct GeminiCallerBridge {
    livekit: LiveKitConfig,
    sim: SimulatorConfig,
    persona_prompt: String,
    room_name: String,
    identity: String,
    writer: Arc<Mutex<EventWriter>>,
}

impl GeminiCallerBridge {
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        livekit: LiveKitConfig,
        sim: SimulatorConfig,
        persona_prompt: String,
        room_name: String,
        identity: String,
        writer: Arc<Mutex<EventWriter>>,
    ) -> Self {
        Self {
            livekit,
            sim,
            persona_prompt,
            room_name,
            identity,
            writer,
        }
    }

    pub async fn run(&self, _end_call: broadcast::Receiver<()>) -> Result<(), RunError> {
        let livekit_cfg = &self.livekit;
        let sim_cfg = &self.sim;

        // 1. Room: connect + publish mic.
        let token = make_token(
            &livekit_cfg.api_key,
            &livekit_cfg.api_secret,
            &self.identity,
            &self.room_name,
        )?;
        let (room, room_events) = connect_room(&livekit_cfg.url, &token, &self.room_name).await?;
        let source = super::openai::publish_mic_shared(&room)?;
        let source = Arc::new(source);

        // 2. Dispatch the agent.
        let api_host = livekit_cfg
            .url
            .replace("wss://", "https://")
            .replace("ws://", "https://");
        let dispatch_id = crate::dispatch::create_dispatch(
            &api_host,
            &livekit_cfg.api_key,
            &livekit_cfg.api_secret,
            &self.room_name,
            &livekit_cfg.agent_name,
            None,
        )
        .await?;

        // 3. Gemini Live session.
        let mut setup = SetupConfig {
            model: format!("models/{}", sim_cfg.voice.model),
            generation_config: Some(GenerationConfig {
                response_modalities: Some(vec![Modality::Audio]),
                speech_config: Some(SpeechConfig {
                    voice_config: VoiceConfig {
                        prebuilt_voice_config: PrebuiltVoiceConfig {
                            voice_name: sim_cfg.voice.voice.clone(),
                        },
                    },
                    language_code: Some(sim_cfg.voice.language.clone()),
                }),
                ..Default::default()
            }),
            system_instruction: Some(gemini_live::Content {
                role: None,
                parts: vec![Part {
                    text: Some(self.persona_prompt.clone()),
                    inline_data: None,
                }],
            }),
            tools: Some(vec![]),
            realtime_input_config: Some(RealtimeInputConfig {
                automatic_activity_detection: Some(AutomaticActivityDetection {
                    disabled: Some(true),
                    ..Default::default()
                }),
                ..Default::default()
            }),
            input_audio_transcription: Some(AudioTranscriptionConfig {}),
            output_audio_transcription: Some(AudioTranscriptionConfig {}),
            ..Default::default()
        };

        // Transport needs the caller's API key (the gemini_live crate does NOT
        // read it from env — Auth::None is the default, which would connect
        // unauthenticated). Port of callers/gemini.py:479 (api_key from config).
        let transport = TransportConfig {
            auth: Auth::ApiKey(sim_cfg.api_key.clone()),
            ..Default::default()
        };
        let session = Session::connect(SessionConfig {
            transport,
            setup: std::mem::take(&mut setup),
            reconnect: Default::default(),
        })
        .await
        .map_err(|e| RunError(format!("gemini connect failed: {e}")))?;
        let session = Arc::new(Mutex::new(session));

        let mut w = self.writer.lock().await;
        w.emit(
            "sim.gemini_connected",
            Some(
                &serde_json::json!({
                    "model": sim_cfg.voice.model,
                    "voice": sim_cfg.voice.voice,
                    "language": sim_cfg.voice.language,
                    "voice_gain": 1.0,
                    "silent_mode": false,
                    "resume": false,
                })
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
        drop(w);

        // 4. Pumps.
        let (out_tx, out_rx) = mpsc::channel::<Vec<i16>>(128);
        let mic_task = tokio::spawn(super::openai::pump_mic_shared(out_rx, source.clone(), None));

        // Gemini events → mic + transcripts.
        let writer = self.writer.clone();
        let session_rx = session.clone();
        let gemini_task = tokio::spawn(async move {
            let mut agent_text = String::new();
            let mut caller_text = String::new();
            let mut session = session_rx.lock().await;
            while let Some(event) = session.next_event().await {
                match event {
                    ServerEvent::ModelAudio(pcm) => {
                        let samples: Vec<i16> = pcm
                            .chunks_exact(2)
                            .map(|c| i16::from_le_bytes([c[0], c[1]]))
                            .collect();
                        let _ = out_tx.send(samples).await;
                    }
                    ServerEvent::OutputTranscription(text) => {
                        agent_text.push_str(&text);
                    }
                    ServerEvent::TurnComplete => {
                        let t = agent_text.trim().to_string();
                        if !t.is_empty() {
                            let mut w = writer.lock().await;
                            w.update_dialogue("agent", &t, true, None);
                            w.emit(
                                "transcript.agent.final",
                                Some(
                                    &serde_json::json!({"text": t})
                                        .as_object()
                                        .cloned()
                                        .unwrap_or_default(),
                                ),
                                "sim.gemini",
                                None,
                                None,
                                false,
                                None,
                            );
                        }
                        agent_text.clear();
                    }
                    ServerEvent::InputTranscription(text) => {
                        caller_text.push_str(&text);
                    }
                    ServerEvent::Interrupted => {}
                    ServerEvent::Closed { .. } => break,
                    _ => {}
                }
                let _ = &caller_text;
            }
        });

        // Agent room audio → Gemini session (24k PCM). The room-events
        // broadcast is shared with the end loop below (resubscribe).
        let session_tx = session.clone();
        let agent_audio_task = tokio::spawn(pump_agent_audio_gemini(
            room.clone(),
            room_events.resubscribe(),
            session_tx,
        ));

        // Wait for end_call, the agent leaving, or a hard slice cap. Emits
        // room.active_speakers / room.disconnected (parity with observer.py
        // handlers + openai.rs). The cap is a single immutable timer so it
        // actually fires after 45s (a sleep recreated per iteration resets it).
        let (end_tx, end_rx) = broadcast::channel::<()>(16);
        let mut disconnect_rx = end_rx.resubscribe();
        let cap = tokio::time::sleep(std::time::Duration::from_secs(45));
        tokio::pin!(cap);
        let mut room_events_watch = room_events;
        let writer_obs = self.writer.clone();
        loop {
            tokio::select! {
                _ = disconnect_rx.recv() => break,
                ev = room_events_watch.recv() => {
                    match ev {
                        Ok(SimRoomEvent::ParticipantDisconnected { identity }) => {
                            eprintln!("[lksr] agent disconnected ({identity}) — ending run");
                            break;
                        }
                        Ok(SimRoomEvent::Disconnected) => {
                            // room.disconnected (port of observer.py _on_disconnected).
                            let mut w = writer_obs.lock().await;
                            w.emit("room.disconnected", None, "room", None, None, false, None);
                            drop(w);
                            break;
                        }
                        Ok(SimRoomEvent::ActiveSpeakersChanged { identities }) => {
                            // room.active_speakers (port of observer.py active_speakers_changed).
                            let mut w = writer_obs.lock().await;
                            let mut spec_m = serde_json::Map::new();
                            let ids: Vec<serde_json::Value> = identities
                                .iter()
                                .map(|i| serde_json::Value::String(i.clone()))
                                .collect();
                            spec_m.insert("identities".into(), serde_json::Value::Array(ids));
                            w.emit(
                                "room.active_speakers",
                                Some(&spec_m),
                                "room",
                                None,
                                None,
                                false,
                                None,
                            );
                            drop(w);
                        }
                        _ => {}
                    }
                }
                _ = &mut cap => {
                    eprintln!("[lksr] slice cap reached (45s) — ending run");
                    break;
                }
            }
        }
        // Signal end to the pumps and let the gemini task drain the pending
        // events (the transcript .done lands here), then abort stragglers.
        let _ = end_tx.send(());
        let _ = tokio::time::timeout(std::time::Duration::from_secs(5), gemini_task).await;
        mic_task.abort();
        agent_audio_task.abort();
        let _ = dispatch_id;
        Ok(())
    }
}

async fn pump_agent_audio_gemini(
    room: Arc<livekit::Room>,
    mut room_events: broadcast::Receiver<SimRoomEvent>,
    session: Arc<Mutex<Session>>,
) {
    // Wait for the agent's audio track.
    let track: Option<livekit::webrtc::audio_track::RtcAudioTrack> = loop {
        tokio::select! {
            _ = tokio::time::sleep(std::time::Duration::from_millis(100)) => {}
            ev = room_events.recv() => {
                match ev {
                    Ok(SimRoomEvent::TrackSubscribed { .. }) => {
                        if let Some((t, _)) = super::openai::find_subscribed_audio(&room) {
                            break Some(t);
                        }
                    }
                    Ok(SimRoomEvent::Disconnected) => return,
                    _ => {}
                }
            }
        }
    };
    let Some(track) = track else { return };
    let mut stream =
        livekit::webrtc::audio_stream::native::NativeAudioStream::new(track, GEMINI_RATE as i32, 1);
    while let Some(frame) = stream.next().await {
        let pcm: &[i16] = frame.data.as_ref();
        let bytes: Vec<u8> = pcm.iter().flat_map(|s| s.to_le_bytes()).collect();
        let s = session.lock().await;
        if s.send_audio(&bytes).await.is_err() {
            return;
        }
    }
}
