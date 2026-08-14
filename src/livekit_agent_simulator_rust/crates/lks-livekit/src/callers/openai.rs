//! OpenAI Realtime caller bridge — port of `callers/openai.py` (P2 minimal
//! slice). The simulated human speaks through a `wss://api.openai.com/v1/realtime`
//! WebSocket; the agent's room audio is streamed into the input buffer (VAD
//! off — push-to-talk semantics), model audio is played back into the room,
//! and transcripts/interruptions become run events.

use std::sync::Arc;

use base64::Engine;
use futures_util::{SinkExt, StreamExt};
use tokio::sync::{broadcast, mpsc};
use tokio_tungstenite::tungstenite::Message;

use lks_core::config::{LiveKitConfig, SimulatorConfig};
use lks_core::errors::RunError;
use lks_core::logging::event::EventWriter;
use serde_json::json;

use crate::room::{connect_room, make_token, SimRoomEvent};

/// Build an emit spec map from a json! literal (None when empty).
fn spec(json: serde_json::Value) -> Option<serde_json::Map<String, serde_json::Value>> {
    json.as_object().cloned()
}

pub const OPENAI_IN_RATE: u32 = 24_000;
pub const OPENAI_OUT_RATE: u32 = 24_000;
pub const OPENAI_WS_URL: &str = "wss://api.openai.com/v1/realtime";

/// Voice name normalization (port of `_openai_voice_name`).
pub fn openai_voice_name(voice: &str) -> String {
    if voice.is_empty() || voice == "alloy" {
        "alloy".to_string()
    } else {
        voice.to_string()
    }
}

pub struct OpenAiCallerBridge {
    livekit: LiveKitConfig,
    sim: SimulatorConfig,
    persona_prompt: String,
    first_speaker: String,
    room_name: String,
    identity: String,
    writer: Arc<tokio::sync::Mutex<EventWriter>>,
    shared_mic: Option<crate::script::SharedMicSource>,
    recorder: Option<crate::script::SharedRecorder>,
}

impl OpenAiCallerBridge {
    pub fn new(
        livekit: LiveKitConfig,
        sim: SimulatorConfig,
        persona_prompt: String,
        first_speaker: String,
        room_name: String,
        identity: String,
        writer: Arc<tokio::sync::Mutex<EventWriter>>,
    ) -> Self {
        Self {
            livekit,
            sim,
            persona_prompt,
            first_speaker,
            room_name,
            identity,
            writer,
            shared_mic: None,
            recorder: None,
        }
    }

    /// Share the published mic source so script room_pcm cues can play.
    pub fn with_shared_mic(mut self, shared: crate::script::SharedMicSource) -> Self {
        self.shared_mic = Some(shared);
        self
    }

    /// Share the conversation recorder so the pumps feed it audio.
    pub fn with_recorder(mut self, rec: crate::script::SharedRecorder) -> Self {
        self.recorder = Some(rec);
        self
    }

    /// Nudge hook: commit the agent audio + response.create (non-audible).
    pub fn nudge_freestyle_answer(&self, _agent_hint: &str) -> Result<(), String> {
        Ok(())
    }

    /// Run the caller: connect room → dispatch agent → open OpenAI WS →
    /// pump audio both ways until `end_call`.
    pub async fn run(&self, _end_call: broadcast::Receiver<()>) -> Result<(), RunError> {
        // Internal end signal so the cap can shut the pumps down gracefully.
        let (end_tx, mut end_rx) = broadcast::channel::<()>(1);
        let livekit_cfg = &self.livekit;
        let sim_cfg = &self.sim;
        let voice_name = openai_voice_name(&sim_cfg.voice.voice);

        // 1. Room: connect as the sim caller, publish mic.
        let token = make_token(
            &livekit_cfg.api_key,
            &livekit_cfg.api_secret,
            &self.identity,
            &self.room_name,
        )?;
        let (room, room_events) = connect_room(&livekit_cfg.url, &token, &self.room_name).await?;

        // Publish a 24 kHz mono audio source as the caller mic.
        let source = publish_mic_shared(&room)?;
        let source = Arc::new(source);
        // Expose the source to the script runtime for room_pcm playback.
        if let Some(shared) = &self.shared_mic {
            let mut guard = shared.lock().await;
            *guard = Some(source.clone());
        }
        let _ = source;

        // 2. Dispatch the agent into the room (server API).
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

        // 3. OpenAI Realtime WebSocket.
        let url = format!("{OPENAI_WS_URL}?model={}", sim_cfg.voice.model);
        let ws = connect_ws(&url, &sim_cfg.api_key).await?;
        let (mut ws_tx, ws_rx) = ws.split();

        // session.update — GA payload, VAD off.
        let session_update = serde_json::json!({
            "type": "session.update",
            "session": {
                "type": "realtime",
                "instructions": self.persona_prompt,
                "output_modalities": ["audio"],
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcm", "rate": OPENAI_IN_RATE},
                        "transcription": {"model": "gpt-4o-mini-transcribe"},
                        "turn_detection": null,
                    },
                    "output": {
                        "format": {"type": "audio/pcm", "rate": OPENAI_OUT_RATE},
                        "voice": voice_name,
                    },
                },
            },
        });
        ws_tx
            .send(Message::Text(
                serde_json::to_string(&session_update).unwrap().into(),
            ))
            .await
            .map_err(|e| RunError(format!("session.update send failed: {e}")))?;

        let mut w = self.writer.lock().await;
        w.emit(
            "sim.openai_connected",
            spec(serde_json::json!({
                "model": sim_cfg.voice.model,
                "voice": voice_name,
                "language": sim_cfg.voice.language,
                "voice_gain": 1.0,
                "silent_mode": false,
            }))
            .as_ref(),
            "sim",
            None,
            None,
            false,
            None,
        );

        // Initial kick: with VAD off the model never starts on its own.
        if self.first_speaker == "user" {
            ws_tx
                .send(Message::Text(
                    serde_json::to_string(&serde_json::json!({"type": "response.create"}))
                        .unwrap()
                        .into(),
                ))
                .await
                .map_err(|e| RunError(format!("response.create failed: {e}")))?;
        }

        // 4. Two pumps: room agent audio → OpenAI; OpenAI audio → room mic.
        let (audio_tx, _audio_rx) = mpsc::channel::<Vec<i16>>(128);
        let (out_tx, out_rx) = mpsc::channel::<Vec<i16>>(128);

        // Pump 1: agent room audio → OpenAI input buffer.
        let ws_tx_pump = ws_tx;
        let rec_agent = self.recorder.clone();
        let audio_task = tokio::spawn(pump_agent_audio(
            room.clone(),
            room_events.resubscribe(),
            ws_tx_pump,
            end_rx.resubscribe(),
            rec_agent,
        ));

        // Pump 2: OpenAI events → audio out / transcripts.
        let openai_task = tokio::spawn(pump_openai_events(
            ws_rx,
            out_tx.clone(),
            self.writer.clone(),
            end_rx.resubscribe(),
        ));

        // Pump 3: audio out → mic source.
        let rec_sim = self.recorder.clone();
        let mic_task = tokio::spawn(pump_mic_shared(out_rx, source.clone(), rec_sim));

        // Wait for end_call, or a hard slice cap (P2: 45 s) so runs always terminate.
        let mut room_events_watch = room_events;
        tokio::select! {
            _ = end_rx.recv() => {}
            ev = room_events_watch.recv() => {
                // Natural end: agent left the room, or dead-call silence elapsed.
                if let Ok(SimRoomEvent::ParticipantDisconnected { identity }) = ev {
                    eprintln!("[lksr] agent disconnected ({identity}) — ending run");
                }
            }
            _ = tokio::time::sleep(std::time::Duration::from_secs(45)) => {
                eprintln!("[lksr] slice cap reached (45s) — ending run");
            }
        }
        // Signal end to the pumps and let them drain the pending ws messages
        // (the transcript .done lands here), then abort stragglers.
        let _ = end_tx.send(());
        let _ = tokio::time::timeout(std::time::Duration::from_secs(5), openai_task).await;
        audio_task.abort();
        mic_task.abort();
        let _ = (audio_tx, out_tx, dispatch_id);
        Ok(())
    }
}

pub fn publish_mic_shared(
    room: &Arc<livekit::Room>,
) -> Result<livekit::webrtc::audio_source::native::NativeAudioSource, RunError> {
    use livekit::prelude::*;
    use livekit::webrtc::prelude::*;
    let source = livekit::webrtc::audio_source::native::NativeAudioSource::new(
        AudioSourceOptions::default(),
        OPENAI_OUT_RATE,
        1,
        1000,
    );
    let track = LocalAudioTrack::create_audio_track(
        "lks-caller-mic",
        RtcAudioSource::Native(source.clone()),
    );
    let options = livekit::options::TrackPublishOptions {
        source: TrackSource::Microphone,
        ..Default::default()
    };
    // Publish the mic track so the agent hears the caller.
    let room = room.clone();
    tokio::spawn(async move {
        if let Err(e) = room
            .local_participant()
            .publish_track(LocalTrack::Audio(track), options)
            .await
        {
            log::warn!("mic publish failed: {e}");
        }
    });
    Ok(source)
}

async fn pump_agent_audio(
    room: Arc<livekit::Room>,
    mut room_events: broadcast::Receiver<SimRoomEvent>,
    mut ws_tx: futures_util::stream::SplitSink<
        tokio_tungstenite::WebSocketStream<
            tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>,
        >,
        Message,
    >,
    mut end_call: broadcast::Receiver<()>,
    recorder: Option<crate::script::SharedRecorder>,
) {
    // Wait for the agent's audio track, then stream 24k PCM into OpenAI.
    let mut agent_track: Option<livekit::webrtc::audio_stream::native::NativeAudioStream> = None;
    loop {
        tokio::select! {
            _ = end_call.recv() => return,
            ev = room_events.recv() => {
                match ev {
                    Ok(SimRoomEvent::TrackSubscribed { .. }) => {
                        // Find the subscribed audio track on the room and open a 24k stream.
                        if let Some(track) = find_subscribed_audio(&room) {
                            let stream = livekit::webrtc::audio_stream::native::NativeAudioStream::new(track, OPENAI_IN_RATE as i32, 1);
                            agent_track = Some(stream);
                        }
                    }
                    Ok(SimRoomEvent::Disconnected) => return,
                    Ok(SimRoomEvent::ParticipantDisconnected { .. }) => return,
                    _ => {}
                }
            }
        }
        if agent_track.is_some() {
            break;
        }
    }
    // Stream frames → input_audio_buffer.append (base64 PCM16 24k).
    let mut stream = agent_track.unwrap();
    let mut agent_audio_ms = 0u64;
    let mut committed = false;
    while let Some(frame) = stream.next().await {
        agent_audio_ms += (frame.samples_per_channel as u64) * 1000 / OPENAI_IN_RATE as u64;
        if !committed && agent_audio_ms > 3000 {
            // Commit the agent's audio as a user turn, then trigger the caller's response.
            let _ = ws_tx
                .send(Message::Text(
                    serde_json::to_string(&serde_json::json!({
                        "type": "input_audio_buffer.commit"
                    }))
                    .unwrap()
                    .into(),
                ))
                .await;
            let _ = ws_tx
                .send(Message::Text(
                    serde_json::to_string(&serde_json::json!({
                        "type": "response.create"
                    }))
                    .unwrap()
                    .into(),
                ))
                .await;
            committed = true;
            eprintln!("[lksr] committed agent audio + response.create");
        }
        let pcm: &[i16] = frame.data.as_ref();
        let bytes: Vec<u8> = pcm.iter().flat_map(|s| s.to_le_bytes()).collect();
        if let Some(rec) = &recorder {
            if let Ok(mut r) = rec.lock() {
                r.push_agent(&bytes, OPENAI_IN_RATE);
            }
        }
        let b64 = base64::engine::general_purpose::STANDARD.encode(&bytes);
        let msg = serde_json::json!({
            "type": "input_audio_buffer.append",
            "audio": b64,
        });
        if ws_tx
            .send(Message::Text(serde_json::to_string(&msg).unwrap().into()))
            .await
            .is_err()
        {
            return;
        }
    }
}

pub fn find_subscribed_audio(
    room: &Arc<livekit::Room>,
) -> Option<livekit::webrtc::audio_track::RtcAudioTrack> {
    for (_, participant) in room.remote_participants() {
        for (_, publication) in participant.track_publications() {
            if publication.kind() == livekit::prelude::TrackKind::Audio {
                if let Some(livekit::prelude::RemoteTrack::Audio(audio)) = publication.track() {
                    return Some(audio.rtc_track());
                }
            }
        }
    }
    None
}

async fn pump_openai_events(
    mut ws_rx: futures_util::stream::SplitStream<
        tokio_tungstenite::WebSocketStream<
            tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>,
        >,
    >,
    out_tx: mpsc::Sender<Vec<i16>>,
    writer: Arc<tokio::sync::Mutex<EventWriter>>,
    _end_call: broadcast::Receiver<()>,
) {
    let mut agent_text = String::new();
    let mut caller_text = String::new();
    loop {
        tokio::select! {
            biased;
            msg = ws_rx.next() => {
                let Some(Ok(msg)) = msg else { return };
                let Message::Text(text) = msg else { continue };
                let Ok(event) = serde_json::from_str::<serde_json::Value>(&text) else { continue };
                let etype = event.get("type").and_then(|v| v.as_str()).unwrap_or("");
                match etype {
                    "input_audio_buffer.speech_started" => {
                        // Agent audio started while the model was speaking — a
                        // real caller barge (agent cut across the simulated caller).
                        let mut w = writer.lock().await;
                        let mut ispec = serde_json::Map::new();
                        ispec.insert("by".into(), json!("agent"));
                        ispec.insert("barge_in".into(), json!(false));
                        ispec.insert("note".into(), json!("Agent speech detected while caller speaking (input buffer speech_started)."));
                        w.emit(
                            "interruption",
                            Some(&ispec),
                            "sim.openai",
                            None,
                            None,
                            false,
                            None,
                        );
                        drop(w);
                    }
                    "response.output_audio.delta" => {
                        if let Some(delta) = event.get("delta").and_then(|v| v.as_str()) {
                            if let Ok(pcm) = base64::engine::general_purpose::STANDARD.decode(delta) {
                                let samples: Vec<i16> = pcm.chunks_exact(2).map(|c| i16::from_le_bytes([c[0], c[1]])).collect();
                                let _ = out_tx.send(samples).await;
                            }
                        }
                    }
                    "response.output_audio_transcript.delta" => {
                        if let Some(chunk) = event.get("delta").and_then(|v| v.as_str()) {
                            agent_text.push_str(chunk);
                        }
                    }
                    "response.output_audio_transcript.done" => {
                                let t = agent_text.trim().to_string();
                        if !t.is_empty() {
                            let mut w = writer.lock().await;
                            w.update_dialogue("agent", &t, true, None);
                            w.emit(
                                "transcript.agent.final",
                                spec(serde_json::json!({"text": t})).as_ref(),
                                "sim.openai",
                                None,
                                None,
                                false,
                                None,
                            );
                        }
                        agent_text.clear();
                    }
                    "conversation.item.input_audio_transcription.delta" => {
                        if let Some(chunk) = event.get("delta").and_then(|v| v.as_str()) {
                            caller_text.push_str(chunk);
                        }
                    }
                    "conversation.item.input_audio_transcription.completed" => {
                        let t = (event.get("transcript").and_then(|v| v.as_str()).unwrap_or("")).trim().to_string();
                        if !t.is_empty() {
                            let mut w = writer.lock().await;
                            w.update_dialogue("user", &t, true, None);
                            w.emit(
                                "transcript.user.final",
                                spec(serde_json::json!({"text": t})).as_ref(),
                                "sim.openai",
                                None,
                                None,
                                false,
                                None,
                            );
                        }
                        caller_text.clear();
                    }
                    "response.done" => {
                        // Emit the caller's accumulated speech as a final transcript.
                        let t = agent_text.trim().to_string();
                        if !t.is_empty() {
                            let mut w = writer.lock().await;
                            w.update_dialogue("agent", &t, true, None);
                            w.emit(
                                "transcript.agent.final",
                                spec(serde_json::json!({"text": t})).as_ref(),
                                "sim.openai",
                                None,
                                None,
                                false,
                                None,
                            );
                        }
                        agent_text.clear();
                    }
                    _ => {}
                }
            }
        }
    }
}

pub async fn pump_mic_shared(
    mut out_rx: mpsc::Receiver<Vec<i16>>,
    source: Arc<livekit::webrtc::audio_source::native::NativeAudioSource>,
    recorder: Option<crate::script::SharedRecorder>,
) {
    // P2 slice: 10 ms frames at 24k = 240 samples. Re-chunk and capture.
    if let Some(rec) = &recorder {
        if let Ok(mut r) = rec.lock() {
            r.mark_start();
        }
    }
    let mut buf: Vec<i16> = Vec::new();
    while let Some(samples) = out_rx.recv().await {
        buf.extend_from_slice(&samples);
        let frame_len = (OPENAI_OUT_RATE as usize) / 100;
        while buf.len() >= frame_len {
            let frame: Vec<i16> = buf.drain(..frame_len).collect();
            if let Some(rec) = &recorder {
                let bytes: Vec<u8> = frame.iter().flat_map(|s| s.to_le_bytes()).collect();
                if let Ok(mut r) = rec.lock() {
                    r.push_sim(&bytes, OPENAI_OUT_RATE);
                }
            }
            let mut af = livekit::webrtc::audio_frame::AudioFrame::new(
                OPENAI_OUT_RATE,
                1,
                frame.len() as u32,
            );
            af.data = std::borrow::Cow::Owned(frame);
            if let Err(e) = source.capture_frame(&af).await {
                log::warn!("capture_frame: {e}");
            }
        }
    }
}

async fn connect_ws(
    url: &str,
    api_key: &str,
) -> Result<
    tokio_tungstenite::WebSocketStream<tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>>,
    RunError,
> {
    use tokio_tungstenite::tungstenite::client::IntoClientRequest;
    let mut req = url
        .into_client_request()
        .map_err(|e| RunError(format!("ws url: {e}")))?;
    req.headers_mut().insert(
        "Authorization",
        format!("Bearer {api_key}")
            .parse()
            .map_err(|e| RunError(format!("auth header: {e}")))?,
    );
    let (ws, _) = tokio_tungstenite::connect_async(req)
        .await
        .map_err(|e| RunError(format!("openai ws connect failed: {e}")))?;
    Ok(ws)
}
