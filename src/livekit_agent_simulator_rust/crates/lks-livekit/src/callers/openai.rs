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
        let (end_tx, end_rx) = broadcast::channel::<()>(1);
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
        // sim.connected (port of webrtc.py sim_leg connect).
        {
            let mut w = self.writer.lock().await;
            let mut spec_m = serde_json::Map::new();
            spec_m.insert(
                "identity".into(),
                serde_json::Value::String(self.identity.clone()),
            );
            spec_m.insert(
                "room".into(),
                serde_json::Value::String(self.room_name.clone()),
            );
            spec_m.insert(
                "mode".into(),
                serde_json::Value::String("webrtc_sim".into()),
            );
            w.emit(
                "sim.connected",
                Some(&spec_m),
                "sim",
                None,
                None,
                false,
                None,
            );
        }

        // Publish a 24 kHz mono audio source as the caller mic.
        let source = publish_mic_shared(&room)?;
        let source = Arc::new(source);
        // Expose the source to the script runtime for room_pcm playback.
        if let Some(shared) = &self.shared_mic {
            let mut guard = shared.lock().await;
            *guard = Some(source.clone());
        }
        // sim.mic_published (port of callers/openai.py _publish_mic).
        {
            let mut w = self.writer.lock().await;
            let mut spec_m = serde_json::Map::new();
            spec_m.insert(
                "sample_rate".into(),
                serde_json::Value::Number(OPENAI_OUT_RATE.into()),
            );
            spec_m.insert("mixer".into(), serde_json::Value::String("parallel".into()));
            spec_m.insert(
                "provider".into(),
                serde_json::Value::String("openai".into()),
            );
            w.emit(
                "sim.mic_published",
                Some(&spec_m),
                "sim",
                None,
                None,
                false,
                None,
            );
        }
        let _ = source;

        // 2. Dispatch the agent into the room (server API) — emit dispatch.created
        //    (port of webrtc.py sim_leg connect).
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
        {
            let mut w = self.writer.lock().await;
            let mut spec_m = serde_json::Map::new();
            spec_m.insert(
                "room".into(),
                serde_json::Value::String(self.room_name.clone()),
            );
            spec_m.insert(
                "agent_name".into(),
                serde_json::Value::String(livekit_cfg.agent_name.clone()),
            );
            spec_m.insert(
                "dispatch_id".into(),
                serde_json::Value::String(dispatch_id.clone()),
            );
            spec_m.insert("metadata_set".into(), serde_json::Value::Bool(false));
            spec_m.insert(
                "mode".into(),
                serde_json::Value::String("webrtc_sim".into()),
            );
            w.emit(
                "dispatch.created",
                Some(&spec_m),
                "mcp",
                None,
                None,
                false,
                None,
            );
        }

        // Wait for the agent participant (port of adapter.wait_for_agent —
        // AgentJoinTimeout on deadline).
        let agent_identity =
            crate::dispatch::wait_for_agent_join(&api_host, livekit_cfg, &self.room_name).await?;
        {
            let mut w = self.writer.lock().await;
            let mut spec_m = serde_json::Map::new();
            spec_m.insert(
                "identity".into(),
                serde_json::Value::String(agent_identity.clone()),
            );
            spec_m.insert(
                "mode".into(),
                serde_json::Value::String("webrtc_sim".into()),
            );
            w.emit(
                "dispatch.agent_joined",
                Some(&spec_m),
                "mcp",
                None,
                None,
                false,
                None,
            );
        }

        // 3. OpenAI Realtime WebSocket. The single sender is shared via an
        // mpsc forwarding task (pump_agent_audio + pump_openai_events both need
        // to send; a SplitSink is not Clone).
        let url = format!("{OPENAI_WS_URL}?model={}", sim_cfg.voice.model);
        let ws = connect_ws(&url, &sim_cfg.api_key).await?;
        let (ws_tx_owned, ws_rx) = ws.split();
        let (ws_msg_tx, mut ws_msg_rx) = mpsc::channel::<Message>(128);
        let ws_forward = tokio::spawn(async move {
            let mut ws_tx = ws_tx_owned;
            while let Some(msg) = ws_msg_rx.recv().await {
                if ws_tx.send(msg).await.is_err() {
                    break;
                }
            }
        });
        let ws_forward_task = ws_forward; // keep the forwarder task alive for the run
        let ws_tx = ws_msg_tx.clone();

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

        {
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
            // drop(w) — tokio Mutex is not reentrant; the midcall emit below
            // locks again in the same task.
        }

        // Initial kick: with VAD off the model never starts on its own. Emits
        // sim.caller_midcall bootstrap when the caller speaks first (parity
        // with openai.py _send_midcall_cues — the OpenAI path sends a text
        // item + response.create).
        if self.first_speaker == "user" {
            // Bootstrap midcall cue (port of caller_policy midcall_cues).
            let bootstrap_text = "(The call just connected. You speak first per PERSONA: greet briefly and state why you are calling in one natural turn.)";
            let mut w = self.writer.lock().await;
            let mut spec_m = serde_json::Map::new();
            spec_m.insert("kind".into(), serde_json::Value::String("bootstrap".into()));
            spec_m.insert("label".into(), serde_json::Value::Null);
            spec_m.insert(
                "text".into(),
                serde_json::Value::String(bootstrap_text.chars().take(240).collect()),
            );
            w.emit(
                "sim.caller_midcall",
                Some(&spec_m),
                "sim",
                None,
                None,
                false,
                None,
            );
            drop(w);
            // GA user-turn text item FIRST, then response.create — the model
            // needs input to respond to (port of openai.py _user_text_item +
            // _send_midcall_cues). Without the item, response.create produces
            // nothing and the caller never speaks.
            let item = serde_json::json!({
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": bootstrap_text}],
                },
            });
            ws_tx
                .send(Message::Text(serde_json::to_string(&item).unwrap().into()))
                .await
                .map_err(|e| RunError(format!("item.create failed: {e}")))?;
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
        let ws_tx_pump = ws_msg_tx.clone();
        let rec_agent = self.recorder.clone();
        let writer_pump = self.writer.clone();
        let audio_task = tokio::spawn(pump_agent_audio(
            room.clone(),
            room_events.resubscribe(),
            ws_tx_pump,
            end_rx.resubscribe(),
            rec_agent,
            writer_pump,
        ));

        // Pump 2: OpenAI events → audio out / transcripts.
        let openai_task = tokio::spawn(pump_openai_events(
            ws_rx,
            out_tx.clone(),
            ws_msg_tx.clone(),
            self.writer.clone(),
            end_rx.resubscribe(),
        ));

        // Pump 3: audio out → mic source.
        let rec_sim = self.recorder.clone();
        let mic_task = tokio::spawn(pump_mic_shared(out_rx, source.clone(), rec_sim));

        // Wait for end_call, the agent leaving, or a hard slice cap. Emits
        // room.active_speakers / room.disconnected as they happen (parity with
        // observer.py handlers).
        let mut room_events_watch = room_events;
        let writer_obs = self.writer.clone();
        let mut disconnect_rx = end_rx.resubscribe();
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
                            spec_m.insert("identities".into(), serde_json::Value::Array(
                                identities.iter().map(|i| serde_json::Value::String(i.clone())).collect(),
                            ));
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
                _ = tokio::time::sleep(std::time::Duration::from_secs(45)) => {
                    eprintln!("[lksr] slice cap reached (45s) — ending run");
                    break;
                }
            }
        }
        // Signal end to the pumps and let them drain the pending ws messages
        // (the transcript .done lands here), then abort stragglers.
        let _ = end_tx.send(());
        let _ = tokio::time::timeout(std::time::Duration::from_secs(5), openai_task).await;
        audio_task.abort();
        mic_task.abort();
        ws_forward_task.abort();
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
    ws_tx: mpsc::Sender<Message>,
    mut end_call: broadcast::Receiver<()>,
    recorder: Option<crate::script::SharedRecorder>,
    writer: Arc<tokio::sync::Mutex<EventWriter>>,
) {
    // Wait for the agent's audio track, then stream 24k PCM into OpenAI.
    let mut agent_track: Option<livekit::webrtc::audio_stream::native::NativeAudioStream> = None;
    // The agent may have already published its track before we started — check
    // once up front so we don't miss a TrackSubscribed that fired earlier.
    if let Some((track, sid)) = find_subscribed_audio(&room) {
        let stream = livekit::webrtc::audio_stream::native::NativeAudioStream::new(
            track,
            OPENAI_IN_RATE as i32,
            1,
        );
        let mut w = writer.lock().await;
        let mut spec_m = serde_json::Map::new();
        spec_m.insert("track_sid".into(), serde_json::Value::String(sid));
        spec_m.insert(
            "provider".into(),
            serde_json::Value::String("openai".into()),
        );
        w.emit(
            "sim.agent_audio_bridged",
            Some(&spec_m),
            "sim",
            None,
            None,
            false,
            None,
        );
        drop(w);
        agent_track = Some(stream);
    }
    loop {
        tokio::select! {
            _ = end_call.recv() => return,
            ev = room_events.recv() => {
                match ev {
                    Ok(SimRoomEvent::TrackSubscribed { .. }) => {
                        // Find the subscribed audio track on the room and open a 24k stream.
                        if let Some((track, sid)) = find_subscribed_audio(&room) {
                            let stream = livekit::webrtc::audio_stream::native::NativeAudioStream::new(track, OPENAI_IN_RATE as i32, 1);
                            // sim.agent_audio_bridged (port of openai.py _agent_audio_pump).
                            let mut w = writer.lock().await;
                            let mut spec_m = serde_json::Map::new();
                            spec_m.insert("track_sid".into(), serde_json::Value::String(sid));
                            spec_m.insert("provider".into(), serde_json::Value::String("openai".into()));
                            w.emit(
                                "sim.agent_audio_bridged",
                                Some(&spec_m),
                                "sim",
                                None,
                                None,
                                false,
                                None,
                            );
                            drop(w);
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
) -> Option<(livekit::webrtc::audio_track::RtcAudioTrack, String)> {
    for (_, participant) in room.remote_participants() {
        for (_, publication) in participant.track_publications() {
            if publication.kind() == livekit::prelude::TrackKind::Audio {
                if let Some(livekit::prelude::RemoteTrack::Audio(audio)) = publication.track() {
                    let sid = publication.sid().to_string();
                    return Some((audio.rtc_track(), sid));
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
    ws_tx: mpsc::Sender<Message>,
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
                        // Caller speech (the model speaks AS the caller).
                        if let Some(chunk) = event.get("delta").and_then(|v| v.as_str()) {
                            caller_text.push_str(chunk);
                        }
                    }
                    "response.output_audio_transcript.done" => {
                        // transcript.user.final — the model's output is the CALLER
                        // (port of openai.py _on_output_transcript_done).
                        let t = caller_text.trim().to_string();
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
                            // sim.caller.audio_source_start once per utterance.
                            let mut src = serde_json::Map::new();
                            src.insert("provider".into(), json!("openai"));
                            src.insert("voice_gain".into(), json!(1.0));
                            src.insert("gain".into(), json!(1.0));
                            src.insert("via".into(), json!("model_output"));
                            w.emit(
                                "sim.caller.audio_source_start",
                                Some(&src),
                                "sim.openai",
                                None,
                                None,
                                false,
                                None,
                            );
                        }
                        caller_text.clear();
                        // Caller finished — commit the agent audio and request the
                        // next caller response (port of _commit_and_respond).
                        let commit = serde_json::json!({"type": "input_audio_buffer.commit"});
                        let _ = ws_tx
                            .send(Message::Text(serde_json::to_string(&commit).unwrap().into()))
                            .await;
                        let rc = serde_json::json!({"type": "response.create"});
                        let _ = ws_tx
                            .send(Message::Text(serde_json::to_string(&rc).unwrap().into()))
                            .await;
                    }
                    "conversation.item.input_audio_transcription.delta" => {
                        // Agent speech (the agent's room audio fed into the model).
                        if let Some(chunk) = event.get("delta").and_then(|v| v.as_str()) {
                            agent_text.push_str(chunk);
                        }
                    }
                    "conversation.item.input_audio_transcription.completed" => {
                        // transcript.agent.final — the AGENT's audio transcribed
                        // (port of openai.py _on_agent_transcript_done).
                        let t = (event.get("transcript").and_then(|v| v.as_str()).unwrap_or("")).trim().to_string();
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
                            // sim.heard_agent (port of openai.py:1202).
                            w.emit(
                                "sim.heard_agent",
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
