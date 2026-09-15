//! OpenAI Realtime caller bridge — port of `callers/openai.py` (P2 minimal
//! slice). The simulated human speaks through a `wss://api.openai.com/v1/realtime`
//! WebSocket; the agent's room audio is streamed into the input buffer (VAD
//! off — push-to-talk semantics), model audio is played back into the room,
//! and transcripts/interruptions become run events.

use std::sync::atomic::{AtomicBool, AtomicI64, Ordering};
use std::sync::Arc;

use base64::Engine;
use futures_util::{SinkExt, StreamExt};
use tokio::sync::{broadcast, mpsc};
use tokio_tungstenite::tungstenite::Message;

use lks_core::config::{LiveKitConfig, ObserveConfig, SimulatorConfig};
use lks_core::errors::RunError;
use lks_core::logging::event::EventWriter;
use serde_json::json;

use crate::callers::end_call;
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
    max_turns: i64,
    room_name: String,
    identity: String,
    writer: Arc<tokio::sync::Mutex<EventWriter>>,
    shared_mic: Option<crate::script::SharedMicSource>,
    /// Shared ScriptObserverState (see with_script_state) — run_plumbing()'s
    /// observation loop writes agent/user speech here; ScriptRuntime's
    /// trigger gates read it. Without this the contract path is deaf (run
    /// 004: only caller-step-0 fired, run ended on timeout).
    script_state: Option<Arc<tokio::sync::Mutex<crate::script::ScriptObserverState>>>,
    /// Contract path (caller_steps non-empty): the bridge is mic/mixer +
    /// observation plumbing ONLY — no OpenAI Realtime persona session, no
    /// freestyle generation. Port of run_orchestrator.py: the contract path
    /// "never opens a session — run() has no call sites (the bridge is used
    /// only for publish_mic); the contract AI adapter builds its prompt from
    /// the BehaviorContract instead". run() with this set skips straight to
    /// run_plumbing() after dispatch.agent_joined.
    contract_only: bool,
    recorder: Option<crate::script::SharedRecorder>,
    /// Scenario Dispatch.metadata || config default (None = empty string).
    dispatch_metadata: Option<String>,
    /// Persona.speech_conditions.silent_mode — mute injects (P1.B1 port).
    silent_mode: bool,
    /// Persona.speech_conditions map (effects resolution at run time).
    persona_speech_conditions: serde_json::Map<String, serde_json::Value>,
    /// Mid-call cue channel receiver (ScriptRuntime → bridge). Mutex-wrapped
    /// so `run(&self)` can take it once at loop start.
    cue_rx: parking_lot::Mutex<Option<crate::script::CueRx>>,
    /// observe.* knobs for data-topic/session observation (Python parity).
    observe: ObserveConfig,
    /// Fix (lksr hardcoded 45s slice cap): the hard per-run timer used to be
    /// a bare `Duration::from_secs(45)` regardless of scenario config.
    /// Sourced from `Scenario::run_spec().timeout_s` (Execute overrides
    /// Simulator, default 120s — see lks-core::scenario) by run.rs; falls
    /// back to the historical 45s when unset (`with_slice_cap_secs` not
    /// called), matching prior behavior for any caller that doesn't opt in.
    slice_cap_secs: u64,
}

impl OpenAiCallerBridge {
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        livekit: LiveKitConfig,
        sim: SimulatorConfig,
        persona_prompt: String,
        first_speaker: String,
        max_turns: i64,
        room_name: String,
        identity: String,
        writer: Arc<tokio::sync::Mutex<EventWriter>>,
    ) -> Self {
        Self {
            livekit,
            sim,
            persona_prompt,
            first_speaker,
            max_turns,
            room_name,
            identity,
            writer,
            shared_mic: None,
            script_state: None,
            contract_only: false,
            recorder: None,
            dispatch_metadata: None,
            silent_mode: false,
            persona_speech_conditions: Default::default(),
            cue_rx: parking_lot::Mutex::new(None),
            observe: ObserveConfig::default(),
            slice_cap_secs: 45,
        }
    }

    /// Builder: observe config for data-topic + lk.agent.session observation.
    pub fn with_observe(mut self, observe: ObserveConfig) -> Self {
        self.observe = observe;
        self
    }

    /// Builder: hard per-run slice cap in seconds (fix: was hardcoded 45s —
    /// see `slice_cap_secs` field doc). Non-positive values fall back to the
    /// 45s default rather than disabling the cap or panicking on Duration
    /// construction.
    pub fn with_slice_cap_secs(mut self, secs: i64) -> Self {
        self.slice_cap_secs = if secs > 0 { secs as u64 } else { 45 };
        self
    }

    /// Builder: opaque Dispatch.metadata passthrough (scenario > config).
    pub fn with_dispatch_metadata(mut self, metadata: Option<String>) -> Self {
        self.dispatch_metadata = metadata;
        self
    }

    /// Builder: Persona.speech_conditions.silent_mode.
    pub fn with_silent_mode(mut self, silent: bool) -> Self {
        self.silent_mode = silent;
        self
    }

    /// Builder: contract-only plumbing mode (caller_steps drives the run —
    /// no persona Realtime session). Set by run.rs from
    /// `!scenario.caller_actions.is_empty()`.
    pub fn with_contract_only(mut self, contract_only: bool) -> Self {
        self.contract_only = contract_only;
        self
    }

    /// Builder: shared ScriptObserverState handle so run_plumbing()'s
    /// observation loop can feed agent/user speech back to ScriptRuntime's
    /// trigger gates (silence/agent_speaking) and reply tracking. None =
    /// triggers never fire (same as the unwired legacy state). Wired by
    /// run.rs from the same Arc handed to ScriptRuntime::new.
    pub fn with_script_state(
        mut self,
        state: Arc<tokio::sync::Mutex<crate::script::ScriptObserverState>>,
    ) -> Self {
        self.script_state = Some(state);
        self
    }

    /// Builder: persona speech_conditions (for degradation effects).
    pub fn with_speech_conditions(
        mut self,
        sc: serde_json::Map<String, serde_json::Value>,
    ) -> Self {
        self.persona_speech_conditions = sc;
        self
    }

    /// Builder: mid-call cue channel (ScriptRuntime → bridge delivery).
    pub fn with_cue_rx(self, rx: crate::script::CueRx) -> Self {
        *self.cue_rx.lock() = Some(rx);
        self
    }

    /// Sim caller identity/name — Python adapter.py SIM_IDENTITY/SIM_NAME. The
    /// target agent (e.g. voice-ai-agent's session-event-handlers) matches the
    /// caller by identity, so the sim must use the stable Python name
    /// ("lks-caller"), not a per-run identity (that would break caller
    /// detection/hangup).
    pub const SIM_IDENTITY: &str = "lks-caller";
    pub const SIM_NAME: &str = "Agent Simulator Caller";

    /// Phase 3: caller finals (sim.openai) now converge onto the shared
    /// `lks-core::observer::Observer` chokepoint instead of a private
    /// begin_turn/turn_taking_ms tracker — matching Python, where
    /// `Observer.on_transcript` is the SAME entry point for provider-native
    /// deltas and `lk.transcription`, so cross-source dedup priority
    /// actually functions (see docs/plans/PLAN-20260813-rust-full-port.md
    /// Appendix F, `_accept_final`). `on_transcript` performs the dialogue
    /// update, turn framing/advance, and `transcript.user.final` emission
    /// that `begin_user_turn` + a manual emit previously duplicated.
    async fn emit_user_final(
        writer: &Arc<tokio::sync::Mutex<EventWriter>>,
        observer: &Arc<tokio::sync::Mutex<lks_core::observer::Observer>>,
        text: &str,
    ) {
        let now_wall_ms = jiff::Zoned::now().timestamp().as_millisecond();
        let mut w = writer.lock().await;
        observer.lock().await.on_transcript(
            &mut w,
            "user",
            text,
            true,
            None,
            "sim.openai",
            std::time::Instant::now(),
            now_wall_ms,
        );
    }

    /// Phase 3: same convergence for agent finals — `on_transcript` performs
    /// the dialogue update, turn framing (opens turn 1 if needed,
    /// `turn_taking_ms` when this is the first reply of the turn), and
    /// `transcript.agent.final` emission that `emit_agent_final`'s manual
    /// logic previously duplicated. The `sim.heard_agent` mirror + activity
    /// timestamp are openai-bridge-specific side effects, kept here.
    async fn emit_agent_final(
        writer: &Arc<tokio::sync::Mutex<EventWriter>>,
        observer: &Arc<tokio::sync::Mutex<lks_core::observer::Observer>>,
        text: &str,
    ) {
        let t = text.trim().to_string();
        if t.is_empty() {
            return;
        }
        // contract_do driver signal: bump seq + latch text BEFORE the
        // dialogue update below, so a `do:` turn already polling can never
        // observe the new seq without the matching text.
        *crate::callers::openai::AGENT_FINAL_TEXT.lock() = t.clone();
        crate::callers::openai::AGENT_FINAL_SEQ.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
        let now_wall_ms = jiff::Zoned::now().timestamp().as_millisecond();
        {
            let mut w = writer.lock().await;
            observer.lock().await.on_transcript(
                &mut w,
                "agent",
                &t,
                true,
                None,
                "sim.openai",
                std::time::Instant::now(),
                now_wall_ms,
            );
        }
        crate::callers::openai::LAST_ANY_ACTIVITY_MS.store(
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_millis() as i64)
                .unwrap_or(0),
            std::sync::atomic::Ordering::SeqCst,
        );
        // sim.heard_agent (port of openai.py:1202).
        let mut w = writer.lock().await;
        w.emit(
            "sim.heard_agent",
            Some(
                &serde_json::json!({"text": t})
                    .as_object()
                    .cloned()
                    .unwrap_or_default(),
            ),
            "sim.openai",
            None,
            None,
            false,
            None,
        );
    }
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

    /// Port of openai.py `_commit_and_respond`: commit the input buffer
    /// (agent audio), request the next model response, and clear the buffer
    /// (VAD off — the buffer is not auto-cleared; without `clear` the next
    /// agent turn's audio mixes with the committed turn's residual PCM). The
    /// central guard skips when a response is already in flight (OpenAI
    /// rejects response.create then).
    async fn commit_and_respond(ws_tx: &mpsc::Sender<Message>, response_in_flight: &AtomicBool) {
        if response_in_flight.load(Ordering::SeqCst) {
            return;
        }
        let commit = serde_json::json!({"type": "input_audio_buffer.commit"});
        let _ = ws_tx
            .send(Message::Text(
                serde_json::to_string(&commit).unwrap().into(),
            ))
            .await;
        let rc = serde_json::json!({"type": "response.create"});
        let _ = ws_tx
            .send(Message::Text(serde_json::to_string(&rc).unwrap().into()))
            .await;
        let clear = serde_json::json!({"type": "input_audio_buffer.clear"});
        let _ = ws_tx
            .send(Message::Text(serde_json::to_string(&clear).unwrap().into()))
            .await;
        response_in_flight.store(true, Ordering::SeqCst);
    }

    /// Run the caller: connect room → dispatch agent → open OpenAI WS →
    /// pump audio both ways until `end_call`.
    ///
    /// Contract-only mode (`contract_only`, set by run.rs when the scenario
    /// carries caller_actions): skip the persona Realtime session entirely —
    /// no WS, no pumps, no bootstrap. The bridge is mic/mixer + observation
    /// plumbing; ScriptRuntime drives speech via the TTS→mic path. Port of
    /// run_orchestrator.py: the contract path "never opens a session".
    pub async fn run(&self, _end_call: broadcast::Receiver<()>) -> Result<(), RunError> {
        if self.contract_only {
            return self.run_plumbing(_end_call).await;
        }
        // Internal end signal so the cap can shut the pumps down gracefully.
        let (end_tx, end_rx) = broadcast::channel::<()>(1);
        // True while the OpenAI server has an in-flight response (Python's
        // _response_in_flight). response.create is rejected while one is
        // active — the central guard skips the hand-off request then.
        let response_in_flight = Arc::new(AtomicBool::new(false));
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
        let observe_gate = crate::room::RoomObserveGate {
            lk_transcription: self.observe.lk_transcription,
            lk_agent_session: self.observe.lk_agent_session,
        };
        let (room, room_events) =
            connect_room(&livekit_cfg.url, &token, &self.room_name, observe_gate).await?;
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
        let source = publish_mic_shared(&room).await?;
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
            self.dispatch_metadata.as_deref(),
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
            spec_m.insert(
                "metadata_set".into(),
                serde_json::Value::Bool(self.dispatch_metadata.is_some()),
            );
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
        // Shared Observer (lks-core::observer) — the single chokepoint for
        // every transcript source (sim.openai deltas AND lk.transcription),
        // matching Python's Observer.on_transcript. Constructed here (agent
        // identity known) so it can be threaded into both pump_openai_events
        // (sim.openai turns) and the room-events select loop below
        // (lk.transcription turns).
        let observer_cfg = lks_core::observer::ObserverConfig {
            agent_identity: agent_identity.clone(),
            sim_identity: self.identity.clone(),
            first_speaker: self.first_speaker.clone(),
            transcript_dedupe_window_ms: self.observe.transcript_dedupe_window_ms,
            ..Default::default()
        };
        let observer = std::sync::Arc::new(tokio::sync::Mutex::new(
            lks_core::observer::Observer::new(observer_cfg),
        ));
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
            response_in_flight.store(true, Ordering::SeqCst);
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
            response_in_flight.clone(),
        ));

        // Pump 2: OpenAI events → audio out / transcripts.
        let openai_task = tokio::spawn(pump_openai_events(
            ws_rx,
            out_tx.clone(),
            ws_msg_tx.clone(),
            self.writer.clone(),
            observer.clone(),
            end_rx.resubscribe(),
            response_in_flight.clone(),
            end_tx.clone(),
            self.max_turns,
        ));

        // Pump 3: audio out → mic source (with persona degradation effects).
        let rec_sim = self.recorder.clone();
        let mic_effects = {
            let sc = &self.persona_speech_conditions;
            crate::degradation::resolve_audio_effects(sc.get("effects")).unwrap_or_default()
        };
        let mic_task = tokio::spawn(pump_mic_shared(
            out_rx,
            source.clone(),
            rec_sim,
            mic_effects,
        ));

        // Wait for end_call, the agent leaving, or a hard slice cap. Emits
        // room.active_speakers / room.disconnected as they happen (parity with
        // observer.py handlers).
        let mut room_events_watch = room_events;

        // lk.agent.session + data-topic observation (Python observer parity).
        let mut session_observer = crate::observe::SessionObserver::new();
        let mut data_router = crate::observe::DataRouter::new(self.observe.clone());
        let writer_obs = self.writer.clone();
        let mut disconnect_rx = end_rx.resubscribe();
        // Hard cap: single immutable timer so it actually fires after
        // `slice_cap_secs` (a sleep recreated per iteration resets it and the
        // cap never triggers). Fix: this was a bare `from_secs(45)` — now
        // sourced from Scenario::run_spec().timeout_s via with_slice_cap_secs
        // (run.rs), falling back to the historical 45s when unset.
        let cap = tokio::time::sleep(std::time::Duration::from_secs(self.slice_cap_secs));
        tokio::pin!(cap);
        // Cue consumer: ScriptRuntime Speak/Dtmf commands (port of
        // bridge.inject_cue).
        //
        // Contract-only mode: Speak = OpenAI TTS (mp3 → 24k PCM) played into
        // the SHARED mic (TTS→mic, the Python BridgePublishSink equivalent —
        // mic audio, never a Realtime text item; there is no persona session
        // to inject into). Freestyle mode (legacy): Speak = verbatim
        // user-turn text item + response.create into the persona session.
        // Dtmf = LiveKit publish_dtmf data packet in both modes.
        //
        // TTS voice follows the scenario's sim voice when it names an OpenAI
        // TTS voice, else the OpenAI default ("alloy"); contract do:/say:
        // text is what the agent TRANSCRIBES (STT-side), so voice identity
        // never affects assertions — parity with Python (sherpa/SAPI voice
        // is equally arbitrary there).
        let mut cue_rx: Option<crate::script::CueRx> = self.cue_rx.lock().take();
        let room_for_dtmf = room.clone();
        let ws_tx_cue = ws_tx.clone();
        let writer_cue = self.writer.clone();
        let contract_mode = self.contract_only;
        // TTS voice allowlist is the audio/speech enum ONLY (nova, shimmer,
        // echo, onyx, fable, alloy, ash, sage, coral) — Realtime voices
        // (marin, cedar, ballad, verse...) 400 here. Map unknown → alloy;
        // voice identity never affects assertions (agent transcribes STT-side).
        // (run verify-fresh: marin → HTTP 400 on all 3 Speak cues.)
        let tts_voice = {
            let v = sim_cfg.voice.voice.trim().to_lowercase();
            match v.as_str() {
                "alloy" | "ash" | "sage" | "coral" | "echo" | "shimmer" | "nova" | "onyx"
                | "fable" => v,
                _ => "alloy".to_string(),
            }
        };
        let tts_key = sim_cfg.api_key.clone();
        let _ = &source;
        let _ = contract_mode;
        loop {
            tokio::select! {
                _ = disconnect_rx.recv() => break,
                cue = async {
                    match cue_rx.as_mut() {
                        Some(rx) => rx.recv().await,
                        None => std::future::pending().await,
                    }
                } => {
                    match cue {
                        Some(crate::script::CueCommand::Speak { text, label }) => {
                            if self.silent_mode {
                                let mut w = writer_cue.lock().await;
                                w.emit(
                                    "sim.silent_mode_skip_inject",
                                    Some(&serde_json::json!({"label": label, "delivery": "openai_text", "text": text.chars().take(120).collect::<String>()}).as_object().cloned().unwrap_or_default()),
                                    "sim",
                                    None,
                                    None,
                                    false,
                                    None,
                                );
                                continue;
                            }
                            if contract_mode {
                                // TTS→mic (contract path): synthesize, then
                                // play PCM into the shared mic the agent hears.
                                // Speak steps are sized for live delivery: if
                                // the mic isn't published yet (agent hasn't
                                // joined / track not up), fail LOUDLY with a
                                // tts_error event instead of hanging forever
                                // inside play_pcm_to_source's mutex wait (run
                                // 006: TTS fired before mic publish → the cue
                                // future never resolved → run sat until the
                                // 300s slice cap with zero dialogue).
                                let tts_label = label.clone();
                                let tts_text = text.clone();
                                let Some(shared) = &self.shared_mic else {
                                    let mut w = writer_cue.lock().await;
                                    w.emit(
                                        "sim.script.tts_error",
                                        Some(&serde_json::json!({"label": tts_label, "error": "shared_mic not wired — TTS has no mic to play into"}).as_object().cloned().unwrap_or_default()),
                                        "sim.script",
                                        None,
                                        None,
                                        false,
                                        None,
                                    );
                                    continue;
                                };
                                let tts_result =
                                    synthesize_caller_speech(&tts_key, &tts_voice, &tts_text).await;
                                match tts_result {
                                    Ok(pcm) => {
                                        let frames = pcm.len() / 2;
                                        eprintln!("[lksr] TTS ok ({label}): {frames} frames, playing to mic");
                                        // Play into the SHARED mic handle (the
                                        // same Arc run.rs handed to both the
                                        // bridge and ScriptRuntime) — NOT a
                                        // throwaway wrapper (run 010: wrapper
                                        // clone pointed at a stale source the
                                        // agent never subscribed to).
                                        if let Err(e) =
                                            crate::script::play_pcm_to_source(
                                                shared, &pcm, OPENAI_OUT_RATE,
                                            )
                                            .await
                                        {
                                            let mut w = writer_cue.lock().await;
                                            w.emit(
                                                "sim.script.tts_error",
                                                Some(&serde_json::json!({"label": tts_label, "error": e}).as_object().cloned().unwrap_or_default()),
                                                "sim.script",
                                                None,
                                                None,
                                                false,
                                                None,
                                            );
                                            continue;
                                        }
                                        let mut w = writer_cue.lock().await;
                                        w.emit(
                                            "transcript.user.final",
                                            Some(&serde_json::json!({"text": tts_text, "final": true}).as_object().cloned().unwrap_or_default()),
                                            "sim.script",
                                            None,
                                            None,
                                            false,
                                            None,
                                        );
                                        w.emit(
                                            "sim.script_inject",
                                            Some(&serde_json::json!({"label": tts_label, "delivery": "tts_mic", "frames": frames}).as_object().cloned().unwrap_or_default()),
                                            "sim.script",
                                            None,
                                            None,
                                            false,
                                            None,
                                        );
                                    }
                                    Err(e) => {
                                        eprintln!("[lksr] TTS error ({label}): {e}");
                                        let mut w = writer_cue.lock().await;
                                        w.emit(
                                            "sim.script.tts_error",
                                            Some(&serde_json::json!({"label": tts_label, "error": e}).as_object().cloned().unwrap_or_default()),
                                            "sim.script",
                                            None,
                                            None,
                                            false,
                                            None,
                                        );
                                    }
                                }
                                continue;
                            }
                            let item = serde_json::json!({
                                "type": "conversation.item.create",
                                "item": {
                                    "type": "message",
                                    "role": "user",
                                    "content": [{"type": "input_text", "text": text}],
                                },
                            });
                            let _ = ws_tx_cue
                                .send(Message::Text(serde_json::to_string(&item).unwrap().into()))
                                .await;
                            let rc = serde_json::json!({"type": "response.create"});
                            let _ = ws_tx_cue
                                .send(Message::Text(serde_json::to_string(&rc).unwrap().into()))
                                .await;
                            let mut w = writer_cue.lock().await;
                            w.emit(
                                "sim.script_inject",
                                Some(&serde_json::json!({"label": label, "delivery": "openai_text"}).as_object().cloned().unwrap_or_default()),
                                "sim.script",
                                None,
                                None,
                                false,
                                None,
                            );
                        }
                        Some(crate::script::CueCommand::Dtmf { digits }) => {
                            const DMAP: &[(&str, u32)] = &[
                                ("0", 0), ("1", 1), ("2", 2), ("3", 3), ("4", 4),
                                ("5", 5), ("6", 6), ("7", 7), ("8", 8), ("9", 9),
                                ("*", 10), ("#", 11),
                            ];
                            let lp = room_for_dtmf.local_participant();
                            for ch in digits.chars() {
                                if ch == 'w' {
                                    tokio::time::sleep(std::time::Duration::from_millis(120)).await;
                                    continue;
                                }
                                let Some((_, code)) = DMAP.iter().find(|(d, _)| *d == ch.to_string()) else {
                                    let mut w = writer_cue.lock().await;
                                    w.emit(
                                        "sim.script.dtmf_error",
                                        Some(&serde_json::json!({"error": format!("unknown DTMF char {ch:?}")}).as_object().cloned().unwrap_or_default()),
                                        "sim.script",
                                        None,
                                        None,
                                        false,
                                        None,
                                    );
                                    break;
                                };
                                    let dtmf = livekit::SipDTMF {
                                    code: *code,
                                    digit: ch.to_string(),
                                    ..Default::default()
                                };
                                if let Err(e) = lp.publish_dtmf(dtmf).await {
                                    let mut w = writer_cue.lock().await;
                                    w.emit(
                                        "sim.script.dtmf_error",
                                        Some(&serde_json::json!({"error": format!("publish_dtmf: {e}")}).as_object().cloned().unwrap_or_default()),
                                        "sim.script",
                                        None,
                                        None,
                                        false,
                                        None,
                                    );
                                }
                                tokio::time::sleep(std::time::Duration::from_millis(150)).await;
                            }
                        }
                        None => {}
                    }
                }
                ev = room_events_watch.recv() => {
                    match ev {
                        Ok(SimRoomEvent::ParticipantConnected { identity, name }) => {
                            // room.participant_connected (port of observer.py _on_join).
                            let mut w = writer_obs.lock().await;
                            let mut spec_m = serde_json::Map::new();
                            spec_m.insert("identity".into(), serde_json::Value::String(identity.clone()));
                            spec_m.insert("name".into(), serde_json::Value::String(name));
                            spec_m.insert("kind".into(), serde_json::Value::String("Remote".into()));
                            w.emit("room.participant_connected", Some(&spec_m), "room", None, None, false, None);
                            drop(w);
                        }
                        Ok(SimRoomEvent::ParticipantDisconnected { identity }) => {
                            // room.participant_disconnected (port of observer.py _on_leave).
                            {
                                let mut w = writer_obs.lock().await;
                                let mut spec_m = serde_json::Map::new();
                                spec_m.insert("identity".into(), serde_json::Value::String(identity.clone()));
                                w.emit("room.participant_disconnected", Some(&spec_m), "room", None, None, false, None);
                            }
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
                            // Set the agent-active-speaker latch (parity with
                            // observer.py agent_is_active_speaker). The caller
                            // identity is the sim; any OTHER active speaker is
                            // the agent under test.
                            let is_agent = identities
                                .iter()
                                .any(|i| i != Self::SIM_IDENTITY && i != &self.identity);
                            AGENT_ACTIVE_SPEAKER.store(is_agent, Ordering::SeqCst);
                            // Feed ScriptRuntime's silence/agent_speaking gates
                            // (contract path was deaf — run 005: only
                            // caller-step-0 fired). Same latch the freestyle
                            // pump reads via AGENT_ACTIVE_SPEAKER.
                            if let Some(st) = &self.script_state {
                                let mut sc = st.lock().await;
                                sc.agent_is_active_speaker = is_agent;
                                if is_agent {
                                    sc.agent_has_spoken = true;
                                }
                            }
                        }
                        Ok(SimRoomEvent::TrackSubscribed { track_sid, participant_identity }) => {
                            // room.track_subscribed (port of observer.py _on_track).
                            let mut w = writer_obs.lock().await;
                            let mut spec_m = serde_json::Map::new();
                            spec_m.insert("identity".into(), serde_json::Value::String(participant_identity));
                            spec_m.insert("kind".into(), serde_json::Value::String("audio".into()));
                            spec_m.insert("sid".into(), serde_json::Value::String(track_sid));
                            w.emit("room.track_subscribed", Some(&spec_m), "room", None, None, false, None);
                            drop(w);
                        }
                        Ok(SimRoomEvent::DataReceived { topic, data, sender }) => {
                            if self.observe.lk_agent_session && topic == crate::observe::TOPIC_SESSION_MESSAGES {
                                // Agent SDK byte stream → tool/session events.
                                let mut w = writer_obs.lock().await;
                                crate::observe::handle_session_bytes(&mut session_observer, &data, &mut w);
                            } else {
                                // Data-topic transcript_turn → shared Observer
                                // (final=true, source=topic), Python parity with
                                // observer.py `_handle_data_topic`. Staged by
                                // the sync router, fed here where the async
                                // observer lock is available.
                                let staged = {
                                    let mut w = writer_obs.lock().await;
                                    data_router.handle_data(&topic, &data, sender.as_deref(), &mut w);
                                    data_router.take_transcript()
                                };
                                if let Some(t) = staged {
                                    if !t.text.trim().is_empty() {
                                        let mut w = writer_obs.lock().await;
                                        let now_wall_ms = jiff::Zoned::now().timestamp().as_millisecond();
                                        observer.lock().await.on_transcript(
                                            &mut w,
                                            &t.role,
                                            &t.text,
                                            true,
                                            None,
                                            &t.source,
                                            std::time::Instant::now(),
                                            now_wall_ms,
                                        );
                                    }
                                }
                            }
                        }
                        Ok(SimRoomEvent::TextStream { participant_identity, text, final_, segment_id, .. }) => {
                            // lk.transcription path through the shared Observer
                            // (dedup/backchannel/preamble ported from observer.py,
                            // see lks-core::observer). sim.openai-sourced turns
                            // converge onto the same instance (Phase 3), so
                            // cross-source dedup priority functions as designed.
                            //
                            // do-driver signal (same as the run_plumbing arm
                            // below): bump AGENT_FINAL_SEQ on agent finals so
                            // run_contract_do's satisfaction wait sees replies
                            // even when they arrive via lk.transcription
                            // rather than the persona WS transcription path.
                            if !text.trim().is_empty() && final_ && participant_identity == agent_identity {
                                *crate::callers::openai::AGENT_FINAL_TEXT.lock() =
                                    text.trim().to_string();
                                crate::callers::openai::AGENT_FINAL_SEQ.fetch_add(
                                    1,
                                    std::sync::atomic::Ordering::SeqCst,
                                );
                            }
                            if !text.trim().is_empty() {
                                let role = if participant_identity == agent_identity { "agent" } else { "user" };
                                let mut w = writer_obs.lock().await;
                                let now_wall_ms = jiff::Zoned::now().timestamp().as_millisecond();
                                observer.lock().await.on_transcript(
                                    &mut w,
                                    role,
                                    &text,
                                    final_,
                                    segment_id.as_deref(),
                                    "lk.transcription",
                                    std::time::Instant::now(),
                                    now_wall_ms,
                                );
                            }
                        }
                        Ok(SimRoomEvent::ByteStream { data, .. }) => {
                            // Pure wiring into the already-correct decoder/dispatcher
                            // (SessionObserver::handle_event via handle_session_bytes).
                            let mut w = writer_obs.lock().await;
                            crate::observe::handle_session_bytes(&mut session_observer, &data, &mut w);
                        }
                        Ok(SimRoomEvent::StreamError { topic, error }) => {
                            let mut w = writer_obs.lock().await;
                            let mut spec_m = serde_json::Map::new();
                            spec_m.insert("where".into(), json!(topic));
                            spec_m.insert("error".into(), json!(error));
                            w.emit("observer.error", Some(&spec_m), &topic, None, None, false, None);
                        }
                        _ => {}
                    }
                }
                _ = &mut cap => {
                    eprintln!("[lksr] slice cap reached ({}s) — ending run", self.slice_cap_secs);
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

    /// Contract-only plumbing run: room connect → mic publish → dispatch →
    /// agent-join wait → observation event loop with the cue consumer
    /// (TTS→mic Speak + DTMF). No persona Realtime session, no freestyle
    /// generation, no pumps. Port of run_orchestrator.py: the contract path
    /// "never opens a session — run() has no call sites (the bridge is used
    /// only for publish_mic)".
    ///
    /// Code is deliberately factored out of run() (not interleaved with
    /// `if contract_only` branches) so the legacy freestyle body stays
    /// byte-identical for review. The shared pieces (room connect, mic
    /// publish, dispatch, agent-join, observer config) mirror run()'s steps
    /// 1–2 verbatim; only the session/pumps half is replaced by the cue loop.
    async fn run_plumbing(&self, _end_call: broadcast::Receiver<()>) -> Result<(), RunError> {
        let (_end_tx, end_rx) = broadcast::channel::<()>(1);
        let livekit_cfg = &self.livekit;

        // 1. Room: connect as the sim caller, publish mic (same as run()).
        let token = make_token(
            &livekit_cfg.api_key,
            &livekit_cfg.api_secret,
            &self.identity,
            &self.room_name,
        )?;
        let observe_gate = crate::room::RoomObserveGate {
            lk_transcription: self.observe.lk_transcription,
            lk_agent_session: self.observe.lk_agent_session,
        };
        let (room, room_events) =
            connect_room(&livekit_cfg.url, &token, &self.room_name, observe_gate).await?;
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
        let source = publish_mic_shared(&room).await?;
        let source = Arc::new(source);
        if let Some(shared) = &self.shared_mic {
            let mut guard = shared.lock().await;
            *guard = Some(source.clone());
        }
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
        {
            let mut w = self.writer.lock().await;
            w.emit(
                "sim.contract_only",
                Some(
                    &serde_json::json!({"mode": "plumbing", "note": "no persona Realtime session; ScriptRuntime drives speech via TTS->mic"})
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
        }

        // 2. Dispatch the agent (server API) + wait for join (same as run()).
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
            self.dispatch_metadata.as_deref(),
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
            spec_m.insert(
                "metadata_set".into(),
                serde_json::Value::Bool(self.dispatch_metadata.is_some()),
            );
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
        let agent_identity =
            crate::dispatch::wait_for_agent_join(&api_host, livekit_cfg, &self.room_name).await?;
        let observer_cfg = lks_core::observer::ObserverConfig {
            agent_identity: agent_identity.clone(),
            sim_identity: self.identity.clone(),
            first_speaker: self.first_speaker.clone(),
            transcript_dedupe_window_ms: self.observe.transcript_dedupe_window_ms,
            ..Default::default()
        };
        let observer = std::sync::Arc::new(tokio::sync::Mutex::new(
            lks_core::observer::Observer::new(observer_cfg),
        ));
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

        // 3. Observation + cue loop (no persona WS). The room-events arms are
        // the same handlers as run()'s select loop (observer.py parity);
        // Speak cues go TTS→mic (see the contract_mode arm below).
        let mut room_events_watch = room_events;
        let mut session_observer = crate::observe::SessionObserver::new();
        let mut data_router = crate::observe::DataRouter::new(self.observe.clone());
        let writer_obs = self.writer.clone();
        let mut disconnect_rx = end_rx.resubscribe();
        let cap = tokio::time::sleep(std::time::Duration::from_secs(self.slice_cap_secs));
        tokio::pin!(cap);
        let mut cue_rx: Option<crate::script::CueRx> = self.cue_rx.lock().take();
        let room_for_dtmf = room.clone();
        let writer_cue = self.writer.clone();
        // Same audio/speech-only allowlist as the freestyle cue loop
        // above (Realtime voices 400 here) — see comment there.
        let tts_voice = {
            let v = self.sim.voice.voice.trim().to_lowercase();
            match v.as_str() {
                "alloy" | "ash" | "sage" | "coral" | "echo" | "shimmer" | "nova" | "onyx"
                | "fable" => v,
                _ => "alloy".to_string(),
            }
        };
        let tts_key = self.sim.api_key.clone();
        let _ = &source;
        loop {
            tokio::select! {
                _ = disconnect_rx.recv() => break,
                cue = async {
                    match cue_rx.as_mut() {
                        Some(rx) => rx.recv().await,
                        None => std::future::pending().await,
                    }
                } => {
                    match cue {
                        Some(crate::script::CueCommand::Speak { text, label }) => {
                            if self.silent_mode {
                                let mut w = writer_cue.lock().await;
                                w.emit(
                                    "sim.silent_mode_skip_inject",
                                    Some(&serde_json::json!({"label": label, "delivery": "tts_mic", "text": text.chars().take(120).collect::<String>()}).as_object().cloned().unwrap_or_default()),
                                    "sim",
                                    None,
                                    None,
                                    false,
                                    None,
                                );
                                continue;
                            }
                            eprintln!("[lksr] TTS speak ({label}): {text}");
                            match synthesize_caller_speech(&tts_key, &tts_voice, &text).await {
                                Ok(pcm) => {
                                    let frames = pcm.len() / 2;
                                    // Play into the SHARED mic handle (same fix
                                    // as the freestyle-path cue loop above —
                                    // a wrapper clone points at a stale source
                                    // the agent does not subscribe to).
                                    let Some(shared) = &self.shared_mic else {
                                        let mut w = writer_cue.lock().await;
                                        w.emit(
                                            "sim.script.tts_error",
                                            Some(&serde_json::json!({"label": label, "error": "shared_mic not wired — TTS has no mic to play into"}).as_object().cloned().unwrap_or_default()),
                                            "sim.script",
                                            None,
                                            None,
                                            false,
                                            None,
                                        );
                                        continue;
                                    };
                                    if let Err(e) = crate::script::play_pcm_to_source(
                                        shared, &pcm, OPENAI_OUT_RATE,
                                    )
                                    .await
                                    {
                                        let mut w = writer_cue.lock().await;
                                        w.emit(
                                            "sim.script.tts_error",
                                            Some(&serde_json::json!({"label": label, "error": e}).as_object().cloned().unwrap_or_default()),
                                            "sim.script",
                                            None,
                                            None,
                                            false,
                                            None,
                                        );
                                        continue;
                                    }
                                    // New caller turn: clear the replied latch so
                                    // the post-cue gap (script.rs) waits for
                                    // the agent's answer to THIS turn.
                                    if let Some(st) = &self.script_state {
                                        let mut s = st.lock().await;
                                        s.user_has_spoken = true;
                                        s.agent_replied_this_turn = false;
                                    }
                                    let mut w = writer_cue.lock().await;
                                    w.emit(
                                        "transcript.user.final",
                                        Some(&serde_json::json!({"text": text, "final": true}).as_object().cloned().unwrap_or_default()),
                                        "sim.script",
                                        None,
                                        None,
                                        false,
                                        None,
                                    );
                                    w.emit(
                                        "sim.script_inject",
                                        Some(&serde_json::json!({"label": label, "delivery": "tts_mic", "frames": frames}).as_object().cloned().unwrap_or_default()),
                                        "sim.script",
                                        None,
                                        None,
                                        false,
                                        None,
                                    );
                                }
                                Err(e) => {
                                    let mut w = writer_cue.lock().await;
                                    w.emit(
                                        "sim.script.tts_error",
                                        Some(&serde_json::json!({"label": label, "error": e}).as_object().cloned().unwrap_or_default()),
                                        "sim.script",
                                        None,
                                        None,
                                        false,
                                        None,
                                    );
                                }
                            }
                        }
                        Some(crate::script::CueCommand::Dtmf { digits }) => {
                            const DMAP: &[(&str, u32)] = &[
                                ("0", 0), ("1", 1), ("2", 2), ("3", 3), ("4", 4),
                                ("5", 5), ("6", 6), ("7", 7), ("8", 8), ("9", 9),
                                ("*", 10), ("#", 11),
                            ];
                            let lp = room_for_dtmf.local_participant();
                            for ch in digits.chars() {
                                if ch == 'w' {
                                    tokio::time::sleep(std::time::Duration::from_millis(120)).await;
                                    continue;
                                }
                                let Some((_, code)) = DMAP.iter().find(|(d, _)| *d == ch.to_string()) else {
                                    let mut w = writer_cue.lock().await;
                                    w.emit(
                                        "sim.script.dtmf_error",
                                        Some(&serde_json::json!({"error": format!("unknown DTMF char {ch:?}")}).as_object().cloned().unwrap_or_default()),
                                        "sim.script",
                                        None,
                                        None,
                                        false,
                                        None,
                                    );
                                    break;
                                };
                                let dtmf = livekit::SipDTMF {
                                    code: *code,
                                    digit: ch.to_string(),
                                    ..Default::default()
                                };
                                if let Err(e) = lp.publish_dtmf(dtmf).await {
                                    let mut w = writer_cue.lock().await;
                                    w.emit(
                                        "sim.script.dtmf_error",
                                        Some(&serde_json::json!({"error": format!("publish_dtmf: {e}")}).as_object().cloned().unwrap_or_default()),
                                        "sim.script",
                                        None,
                                        None,
                                        false,
                                        None,
                                    );
                                }
                                tokio::time::sleep(std::time::Duration::from_millis(150)).await;
                            }
                        }
                        None => {}
                    }
                }
                ev = room_events_watch.recv() => {
                    match ev {
                        Ok(SimRoomEvent::ParticipantConnected { identity, name }) => {
                            let mut w = writer_obs.lock().await;
                            let mut spec_m = serde_json::Map::new();
                            spec_m.insert("identity".into(), serde_json::Value::String(identity.clone()));
                            spec_m.insert("name".into(), serde_json::Value::String(name));
                            spec_m.insert("kind".into(), serde_json::Value::String("Remote".into()));
                            w.emit("room.participant_connected", Some(&spec_m), "room", None, None, false, None);
                            drop(w);
                        }
                        Ok(SimRoomEvent::ParticipantDisconnected { identity }) => {
                            {
                                let mut w = writer_obs.lock().await;
                                let mut spec_m = serde_json::Map::new();
                                spec_m.insert("identity".into(), serde_json::Value::String(identity.clone()));
                                w.emit("room.participant_disconnected", Some(&spec_m), "room", None, None, false, None);
                            }
                            eprintln!("[lksr] agent disconnected ({identity}) — ending run");
                            break;
                        }
                        Ok(SimRoomEvent::Disconnected) => {
                            let mut w = writer_obs.lock().await;
                            w.emit("room.disconnected", None, "room", None, None, false, None);
                            drop(w);
                            break;
                        }
                        Ok(SimRoomEvent::ActiveSpeakersChanged { identities }) => {
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
                            let is_agent = identities
                                .iter()
                                .any(|i| i != Self::SIM_IDENTITY && i != &self.identity);
                            AGENT_ACTIVE_SPEAKER.store(is_agent, Ordering::SeqCst);
                            // Feed ScriptRuntime's silence/agent_speaking gates
                            // (contract path was deaf — run 005: only
                            // caller-step-0 fired). Same latch the freestyle
                            // pump reads via AGENT_ACTIVE_SPEAKER.
                            if let Some(st) = &self.script_state {
                                let mut sc = st.lock().await;
                                sc.agent_is_active_speaker = is_agent;
                                if is_agent {
                                    sc.agent_has_spoken = true;
                                }
                            }
                        }
                        Ok(SimRoomEvent::TrackSubscribed { track_sid, participant_identity }) => {
                            let mut w = writer_obs.lock().await;
                            let mut spec_m = serde_json::Map::new();
                            spec_m.insert("identity".into(), serde_json::Value::String(participant_identity));
                            spec_m.insert("kind".into(), serde_json::Value::String("audio".into()));
                            spec_m.insert("sid".into(), serde_json::Value::String(track_sid));
                            w.emit("room.track_subscribed", Some(&spec_m), "room", None, None, false, None);
                            drop(w);
                        }
                        Ok(SimRoomEvent::DataReceived { topic, data, sender }) => {
                            if self.observe.lk_agent_session && topic == crate::observe::TOPIC_SESSION_MESSAGES {
                                let mut w = writer_obs.lock().await;
                                crate::observe::handle_session_bytes(&mut session_observer, &data, &mut w);
                            } else {
                                let staged = {
                                    let mut w = writer_obs.lock().await;
                                    data_router.handle_data(&topic, &data, sender.as_deref(), &mut w);
                                    data_router.take_transcript()
                                };
                                if let Some(t) = staged {
                                    if !t.text.trim().is_empty() {
                                        let mut w = writer_obs.lock().await;
                                        let now_wall_ms = jiff::Zoned::now().timestamp().as_millisecond();
                                        observer.lock().await.on_transcript(
                                            &mut w,
                                            &t.role,
                                            &t.text,
                                            true,
                                            None,
                                            &t.source,
                                            std::time::Instant::now(),
                                            now_wall_ms,
                                        );
                                    }
                                }
                            }
                        }
                        Ok(SimRoomEvent::TextStream { participant_identity, text, final_, segment_id, .. }) => {
                            if !text.trim().is_empty() {
                                let role = if participant_identity == agent_identity { "agent" } else { "user" };
                                // Feed ScriptRuntime's trigger gates (run 004:
                                // contract path was deaf — only caller-step-0
                                // fired, run timed out). user finals mark
                                // user_has_spoken; agent finals mark
                                // has_spoken + replied_this_turn + latch text.
                                //
                                // do-driver signal (run verify-voice): the
                                // contract_do satisfaction wait polls
                                // AGENT_FINAL_SEQ — bump it here too, since
                                // run_plumbing has no persona WS whose
                                // input_audio_transcription.completed would
                                // otherwise bump it via emit_agent_final.
                                if final_ {
                                    *crate::callers::openai::AGENT_FINAL_TEXT.lock() =
                                        text.trim().to_string();
                                    crate::callers::openai::AGENT_FINAL_SEQ.fetch_add(
                                        1,
                                        std::sync::atomic::Ordering::SeqCst,
                                    );
                                }
                                if let Some(st) = &self.script_state {
                                    let mut s = st.lock().await;
                                    if role == "agent" && final_ {
                                        s.agent_has_spoken = true;
                                        s.agent_replied_this_turn = true;
                                        s.last_agent_final_text = text.trim().to_string();
                                    } else if role == "user" && final_ {
                                        s.user_has_spoken = true;
                                        s.agent_replied_this_turn = false;
                                    }
                                }
                                let mut w = writer_obs.lock().await;
                                let now_wall_ms = jiff::Zoned::now().timestamp().as_millisecond();
                                observer.lock().await.on_transcript(
                                    &mut w,
                                    role,
                                    &text,
                                    final_,
                                    segment_id.as_deref(),
                                    "lk.transcription",
                                    std::time::Instant::now(),
                                    now_wall_ms,
                                );
                            }
                        }
                        Ok(SimRoomEvent::ByteStream { data, .. }) => {
                            let mut w = writer_obs.lock().await;
                            crate::observe::handle_session_bytes(&mut session_observer, &data, &mut w);
                        }
                        Ok(SimRoomEvent::StreamError { topic, error }) => {
                            let mut w = writer_obs.lock().await;
                            let mut spec_m = serde_json::Map::new();
                            spec_m.insert("where".into(), json!(topic));
                            spec_m.insert("error".into(), json!(error));
                            w.emit("observer.error", Some(&spec_m), &topic, None, None, false, None);
                        }
                        _ => {}
                    }
                }
                _ = &mut cap => {
                    eprintln!("[lksr] slice cap reached ({}s) — ending run", self.slice_cap_secs);
                    break;
                }
            }
        }
        let _ = dispatch_id;
        Ok(())
    }
}

/// Contract-path TTS: OpenAI audio/speech → mp3 bytes → 24 kHz mono PCM16.
///
/// Port of live_wiring.py `_synthesize` (contract path speaks through TTS,
/// never through a persona Realtime session). MP3 decode needs no new dep:
/// the `hound` crate (already a dependency) only writes WAV, so this parses
/// MP3 frames minimally — no. MP3 decode is non-trivial without a decoder
/// crate, so request `wav` output format from the API instead (supported:
/// mp3/opus/aac/flac/wav/pcm) and parse the 44-byte RIFF header directly.
/// Falls back to 24 kHz resample-free playback: requests 24 kHz explicitly
/// (`response_format=wav` has no rate param — OpenAI returns the model's
/// native rate, resampled below when it differs).
async fn synthesize_caller_speech(
    api_key: &str,
    voice: &str,
    text: &str,
) -> Result<Vec<i16>, String> {
    // reqwest with rustls (no default features) — mirrors the blocking-free
    // async style used by lks-core::contract_do::generate_do_candidate.
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(30))
        .build()
        .map_err(|e| format!("tts client: {e}"))?;
    let resp = client
        .post("https://api.openai.com/v1/audio/speech")
        .bearer_auth(api_key)
        .json(&serde_json::json!({
            "model": "tts-1",
            "input": text,
            "voice": voice,
            "response_format": "wav",
        }))
        .send()
        .await
        .map_err(|e| format!("tts request: {e}"))?;
    if !resp.status().is_success() {
        let status = resp.status();
        let body = resp.text().await.unwrap_or_default();
        let clipped: String = body.chars().take(200).collect();
        return Err(format!("tts HTTP {status}: {clipped}"));
    }
    let bytes = resp.bytes().await.map_err(|e| format!("tts body: {e}"))?;
    wav_bytes_to_pcm16_24k(&bytes)
}

/// Parse a WAV (44-byte RIFF header, 16-bit PCM mono) into samples,
/// resampling to 24 kHz when the file rate differs (linear interpolation —
/// TTS speech tolerates it; avoids pulling a resampling crate for one call).
fn wav_bytes_to_pcm16_24k(bytes: &[u8]) -> Result<Vec<i16>, String> {
    if bytes.len() < 44 {
        return Err(format!("tts wav too short: {} bytes", bytes.len()));
    }
    if &bytes[0..4] != b"RIFF" || &bytes[8..12] != b"WAVE" {
        return Err("tts response is not a WAV file".to_string());
    }
    // Walk chunks to find "fmt " and "data" (fmt may not be first).
    let mut pos = 12usize;
    let mut fmt_rate = 0u32;
    let mut fmt_channels = 0u16;
    let mut fmt_bits = 0u16;
    let mut data: &[u8] = &[];
    while pos + 8 <= bytes.len() {
        let id = &bytes[pos..pos + 4];
        let size = u32::from_le_bytes(
            bytes[pos + 4..pos + 8]
                .try_into()
                .map_err(|_| "wav truncated chunk header".to_string())?,
        ) as usize;
        let body_start = pos + 8;
        let body_end = (body_start + size).min(bytes.len());
        if id == b"fmt " {
            if size < 16 {
                return Err("tts wav fmt chunk too small".to_string());
            }
            let audio_fmt =
                u16::from_le_bytes(bytes[body_start..body_start + 2].try_into().unwrap());
            if audio_fmt != 1 {
                return Err(format!("tts wav not PCM (format {audio_fmt})"));
            }
            fmt_channels =
                u16::from_le_bytes(bytes[body_start + 2..body_start + 4].try_into().unwrap());
            fmt_rate =
                u32::from_le_bytes(bytes[body_start + 4..body_start + 8].try_into().unwrap());
            fmt_bits =
                u16::from_le_bytes(bytes[body_start + 14..body_start + 16].try_into().unwrap());
        } else if id == b"data" {
            data = &bytes[body_start..body_end];
        }
        pos = body_end + (size % 2);
    }
    if fmt_rate == 0 || data.is_empty() {
        return Err("tts wav missing fmt/data chunks".to_string());
    }
    if fmt_bits != 16 {
        return Err(format!("tts wav not 16-bit (bits {fmt_bits})"));
    }
    // Mono: pairs of LE bytes. Stereo (unexpected): take left channel.
    let stride = fmt_channels.max(1) as usize;
    let mut samples: Vec<i16> = data
        .chunks_exact(2 * stride)
        .map(|f| i16::from_le_bytes([f[0], f[1]]))
        .collect();
    if fmt_rate != OPENAI_OUT_RATE {
        // Linear resample to 24 kHz.
        let ratio = OPENAI_OUT_RATE as f64 / fmt_rate as f64;
        let out_len = ((samples.len() as f64) * ratio) as usize;
        let mut out = Vec::with_capacity(out_len);
        for i in 0..out_len {
            let src = i as f64 / ratio;
            let lo = src.floor() as usize;
            let hi = (lo + 1).min(samples.len() - 1);
            let frac = (src - lo as f64) as f32;
            let a = samples[lo.min(samples.len() - 1)] as f32;
            let b = samples[hi] as f32;
            out.push((a + (b - a) * frac) as i16);
        }
        samples = out;
    }
    Ok(samples)
}

pub async fn publish_mic_shared(
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
    //
    // AWAIT the publish (do not fire-and-forget): contract-path TTS cues
    // (and room_pcm beds) play into this source via the shared handle, and
    // a publish that silently fails/races leaves playback with nowhere to
    // go — the agent hears nothing and the run sits until the slice cap
    // (run 001-final-lksr: ambient bed fired, then 300s of silence). A loud
    // error here fails fast instead.
    let room = room.clone();
    room.local_participant()
        .publish_track(LocalTrack::Audio(track), options)
        .await
        .map_err(|e| RunError(format!("mic publish failed: {e}")))?;
    Ok(source)
}

async fn pump_agent_audio(
    room: Arc<livekit::Room>,
    mut room_events: broadcast::Receiver<SimRoomEvent>,
    ws_tx: mpsc::Sender<Message>,
    mut end_call: broadcast::Receiver<()>,
    recorder: Option<crate::script::SharedRecorder>,
    writer: Arc<tokio::sync::Mutex<EventWriter>>,
    response_in_flight: Arc<AtomicBool>,
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
    // Stream frames → input_audio_buffer.append (base64 PCM16 24k). Port of
    // openai.py _pump_agent_audio: track the agent's speech end via a
    // trailing-silence window (RMS-gated), and commit + request the caller's
    // response on that boundary (VAD off — this is the manual turn hand-off).
    let mut stream = agent_track.unwrap();
    let mut agent_audio_ms = 0u64;
    let mut last_speech: Option<std::time::Instant> = None;
    let mut was_speaking = false;
    let mut commit_pending = false;
    // LiveKit's active-speaker detection (parity with observer.py
    // agent_is_active_speaker). The room marks the agent as an active speaker
    // when it is actually producing audio — this catches speech the RMS gate
    // would miss (lower-energy or ASR-gated audio). Port of openai.py
    // `obs_speaking or energy_speaking`.
    while let Some(frame) = stream.next().await {
        agent_audio_ms += (frame.samples_per_channel as u64) * 1000 / OPENAI_IN_RATE as u64;
        let pcm: &[i16] = frame.data.as_ref();
        let rms = pcm_iter_rms(pcm.iter().map(|&s| s as f32));
        let obs_speaking = AGENT_ACTIVE_SPEAKER.load(Ordering::SeqCst);
        let speaking = obs_speaking || rms >= AGENT_SPEECH_RMS;
        let now = std::time::Instant::now();
        if speaking {
            // Rising edge → perceived agent-speech onset (port of
            // observer.py _on_agent_onset; ts_mono_ms = detection frame).
            if !was_speaking {
                was_speaking = true;
                let mut w = writer.lock().await;
                let mut spec_m = serde_json::Map::new();
                spec_m.insert(
                    "onset_frame_idx".into(),
                    serde_json::json!(agent_audio_ms as i64),
                );
                w.emit(
                    "sim.agent.audio_onset",
                    Some(&spec_m),
                    "sim",
                    None,
                    None,
                    false,
                    None,
                );
            }
            last_speech = Some(now);
            commit_pending = false;
            // Feed the hold-music-timeout watchdog (run.rs) + observer state.
            LAST_AGENT_ACTIVITY_MS.store(
                std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .map(|d| d.as_millis() as i64)
                    .unwrap_or(0),
                Ordering::SeqCst,
            );
            AGENT_HAS_SPOKEN.store(true, Ordering::SeqCst);
        } else {
            was_speaking = false;
            if let Some(ls) = last_speech {
                if now.duration_since(ls).as_millis() >= AGENT_STREAM_END_SILENCE_MS {
                    commit_pending = true;
                    last_speech = None;
                    // Agent stopped speaking — commit the buffered audio + request
                    // the caller response (port of _commit_and_respond).
                    OpenAiCallerBridge::commit_and_respond(&ws_tx, &response_in_flight).await;
                }
            }
        }
        if !speaking
            && ((frame.samples_per_channel as u64) * 1000 / OPENAI_IN_RATE as u64)
                >= AGENT_STREAM_END_SILENCE_MS as u64
        {
            // Long silence — skip to avoid flooding the buffer.
            continue;
        }
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
    let _ = &mut commit_pending;
    let _ = &mut agent_audio_ms;
}

/// RMS of a float PCM frame (port of Python `pcm16_mono_rms`).
fn pcm_iter_rms(mut it: impl Iterator<Item = f32>) -> f32 {
    let mut sum = 0.0f64;
    let mut n = 0u64;
    for s in it.by_ref() {
        let f = s / 32768.0;
        sum += (f * f) as f64;
        n += 1;
    }
    if n == 0 {
        return 0.0;
    }
    (sum / n as f64).sqrt() as f32
}

// Port of openai.py _AGENT_SPEECH_RMS_THRESHOLD / _AGENT_STREAM_END_SILENCE_MS.
const AGENT_SPEECH_RMS: f32 = 100.0;
const AGENT_STREAM_END_SILENCE_MS: u128 = 650;

/// Set by the room-active-speakers handler when the agent is an active speaker
/// (parity with observer.py `agent_is_active_speaker`). The agent-audio pump
/// treats the agent as speaking when this is true OR RMS is above threshold —
/// the active-speaker signal catches audio the RMS gate would miss.
pub static AGENT_ACTIVE_SPEAKER: AtomicBool = AtomicBool::new(false);
/// Last instant the agent produced audio (RMS or active-speaker) — read by
/// the hold-music-timeout watchdog in run.rs (port of
/// observer.last_agent_activity_mono).
pub static LAST_AGENT_ACTIVITY_MS: AtomicI64 = AtomicI64::new(0);
/// Last transcript event (user or agent final) — used by dead_call_silence
/// watchdog (port of observer.last_activity_mono fallback chain).
pub static LAST_ANY_ACTIVITY_MS: AtomicI64 = AtomicI64::new(0);
/// True once the agent produced any audio this run.
pub static AGENT_HAS_SPOKEN: AtomicBool = AtomicBool::new(false);
/// True while a script step with mute_persona=true is active — suppress freestyle audio.
pub static MUTE_PERSONA_ACTIVE: AtomicBool = AtomicBool::new(false);
/// Bumped every time `emit_agent_final` fires — read by the `contract_do`
/// driver (`crate::script`) to detect a NEW agent reply (vs. a stale one
/// from before it published) without polling the shared Observer directly.
pub static AGENT_FINAL_SEQ: AtomicI64 = AtomicI64::new(0);
/// Text of the most recent agent final (paired with `AGENT_FINAL_SEQ`).
pub static AGENT_FINAL_TEXT: parking_lot::Mutex<String> = parking_lot::Mutex::new(String::new());

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

#[allow(clippy::too_many_arguments)]
async fn pump_openai_events(
    mut ws_rx: futures_util::stream::SplitStream<
        tokio_tungstenite::WebSocketStream<
            tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>,
        >,
    >,
    out_tx: mpsc::Sender<Vec<i16>>,
    ws_tx: mpsc::Sender<Message>,
    writer: Arc<tokio::sync::Mutex<EventWriter>>,
    observer: Arc<tokio::sync::Mutex<lks_core::observer::Observer>>,
    _end_call: broadcast::Receiver<()>,
    response_in_flight: Arc<AtomicBool>,
    end_tx: broadcast::Sender<()>,
    max_turns: i64,
) {
    let mut agent_text = String::new();
    let mut caller_text = String::new();
    let end_tx = end_tx.clone();
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
                        if crate::callers::openai::MUTE_PERSONA_ACTIVE
                            .load(std::sync::atomic::Ordering::SeqCst)
                        {
                            // Persona muted during script cue — drop audio.
                        } else if let Some(delta) = event.get("delta").and_then(|v| v.as_str()) {
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
                            // Emit transcript.user.interim for real-time TUI updates.
                            let accumulated = caller_text.trim().to_string();
                            if !accumulated.is_empty() {
                                let mut w = writer.lock().await;
                                let mut spec_m = serde_json::Map::new();
                                spec_m.insert("text".into(), serde_json::Value::String(accumulated));
                                spec_m.insert("final".into(), serde_json::Value::Bool(false));
                                w.emit("transcript.user.interim", Some(&spec_m), "sim.openai", None, None, false, None);
                            }
                        }
                    }
                    "response.audio_transcript.done" => {
                        // Same as output_audio_transcript.done (older/newer
                        // event-name variants; Python openai.py listens on
                        // response.audio_transcript.done). The caller's final
                        // turn must land in the transcript too.
                        response_in_flight.store(false, Ordering::SeqCst);
                        let t = caller_text.trim().to_string();
                        let ended = end_call::contains_end_call_signal(&caller_text);
                        let farewell = end_call::contains_farewell_signal(&caller_text);
                        let clean = end_call::strip_end_call_signal(&t);
                        if !clean.is_empty() {
                            OpenAiCallerBridge::emit_user_final(&writer, &observer, &clean).await;
                            crate::callers::openai::LAST_ANY_ACTIVITY_MS.store(
                                std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_millis() as i64).unwrap_or(0),
                                std::sync::atomic::Ordering::SeqCst,
                            );
                            let mut w = writer.lock().await;
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
                            drop(w);
                        }
                        caller_text.clear();
                        if ended || farewell {
                            let mut w = writer.lock().await;
                            let mut ec = serde_json::Map::new();
                            ec.insert(
                                "text".into(),
                                json!(end_call::strip_farewell_signal(&clean)),
                            );
                            ec.insert(
                                "reason".into(),
                                json!(if ended { "end_call_token" } else { "farewell" }),
                            );
                            w.emit(
                                "sim.end_call_token",
                                Some(&ec),
                                "sim.openai",
                                None,
                                None,
                                false,
                                None,
                            );
                            drop(w);
                            let _ = end_tx.send(());
                            break;
                        }
                        // Caller finished — commit + request the next caller
                        // response (port of _commit_and_respond).
                        OpenAiCallerBridge::commit_and_respond(&ws_tx, &response_in_flight).await;
                    }
                    "response.output_audio_transcript.done" => {
                        response_in_flight.store(false, Ordering::SeqCst);
                        // transcript.user.final — the model's output is the CALLER
                        // (port of openai.py _on_output_transcript_done).
                        let t = caller_text.trim().to_string();
                        // End-call / farewell detection (port of openai.py
                        // _on_output_transcript_done → should_end_call_on_turn).
                        let ended = end_call::contains_end_call_signal(&caller_text);
                        let farewell = end_call::contains_farewell_signal(&caller_text);
                        // Clean transcript: strip [END_CALL] / spoken hang-up
                        // markers before logging (Python _on_output_transcript_done
                        // uses strip_end_call_signal when not script-pending).
                        let clean = end_call::strip_end_call_signal(&t);
                        if !clean.is_empty() {
                            OpenAiCallerBridge::emit_user_final(&writer, &observer, &clean).await;
                            crate::callers::openai::LAST_ANY_ACTIVITY_MS.store(
                                std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_millis() as i64).unwrap_or(0),
                                std::sync::atomic::Ordering::SeqCst,
                            );
                            // sim.caller.audio_source_start once per utterance.
                            let mut w = writer.lock().await;
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
                            drop(w);
                        }
                        caller_text.clear();
                        if ended || farewell {
                            // Caller said goodbye / end-call — end the run
                            // instead of committing + re-requesting (caller
                            // self-loop). Port of openai.py: emits
                            // sim.end_call_token, sets end_call, returns.
                            let mut w = writer.lock().await;
                            let mut ec = serde_json::Map::new();
                            ec.insert(
                                "text".into(),
                                json!(end_call::strip_farewell_signal(&clean)),
                            );
                            ec.insert(
                                "reason".into(),
                                json!(if ended { "end_call_token" } else { "farewell" }),
                            );
                            w.emit(
                                "sim.end_call_token",
                                Some(&ec),
                                "sim.openai",
                                None,
                                None,
                                false,
                                None,
                            );
                            drop(w);
                            let _ = end_tx.send(());
                            break;
                        }
                        // Caller finished — commit the agent audio and request the
                        // next caller response (port of _commit_and_respond).
                        OpenAiCallerBridge::commit_and_respond(&ws_tx, &response_in_flight).await;
                    }
                    "conversation.item.input_audio_transcription.delta" => {
                        // Agent speech (the agent's room audio fed into the model).
                        if let Some(chunk) = event.get("delta").and_then(|v| v.as_str()) {
                            agent_text.push_str(chunk);
                            // Emit transcript.agent.interim for real-time TUI updates.
                            let accumulated = agent_text.trim().to_string();
                            if !accumulated.is_empty() {
                                let mut w = writer.lock().await;
                                let mut spec_m = serde_json::Map::new();
                                spec_m.insert("text".into(), serde_json::Value::String(accumulated));
                                spec_m.insert("final".into(), serde_json::Value::Bool(false));
                                w.emit("transcript.agent.interim", Some(&spec_m), "sim.openai", None, None, false, None);
                            }
                        }
                    }
                    "conversation.item.input_audio_transcription.completed" => {
                        // transcript.agent.final — the AGENT's audio transcribed
                        // (port of openai.py _on_agent_transcript_done).
                        let t = (event.get("transcript").and_then(|v| v.as_str()).unwrap_or("")).trim().to_string();
                        if !t.is_empty() {
                            OpenAiCallerBridge::emit_agent_final(&writer, &observer, &t).await;
                            // max_turns reached after the agent replied — end the
                            // run (port of run_orchestrator.py:769 "max_turns").
                            if max_turns > 0 {
                                let turn = writer.lock().await.current_turn();
                                if turn >= max_turns {
                                    let _ = end_tx.send(());
                                    break;
                                }
                            }
                        }
                        agent_text.clear();
                        // Agent finished speaking — request the next caller
                        // response (port of openai.py _on_agent_transcript_done
                        // commit+respond). Without this the model never hears
                        // the agent's turn (VAD off) and dead air follows.
                        OpenAiCallerBridge::commit_and_respond(&ws_tx, &response_in_flight).await;
                    }
                    "response.created" => {
                        // A model response started (Python openai.py sets
                        // _response_in_flight on response.created).
                        response_in_flight.store(true, Ordering::SeqCst);
                    }
                    "response.cancelled" | "response.failed" => {
                        // Response aborted — clear the in-flight guard (Python
                        // openai.py clears on response.cancelled/failed).
                        response_in_flight.store(false, Ordering::SeqCst);
                    }
                    "response.done" => {
                        // The model response finished (port of openai.py
                        // _on_response_done — the in-flight flag drives the
                        // commit+respond guard).
                        response_in_flight.store(false, Ordering::SeqCst);
                        // Emit the agent's accumulated input-transcript as a
                        // final transcript (port of openai.py _on_response_done
                        // flush — without this a caller-final utterance whose
                        // `.done` never arrived would strand un-finalized).
                        //
                        // Buffer-ownership guard (live run
                        // verify-lksr-alive/002): `.completed` (line ~1383)
                        // above already emits the agent final AND clears this
                        // buffer — emitting unconditionally here re-logs the
                        // SAME text a second time. Worse, between clear() and
                        // this handler the deltas of the NEXT turn may already
                        // have accumulated, so the duplicate arrives under a
                        // new turn number and corrupts turn framing (the run
                        // showed the same agent line twice + ghost "Yeah,"/"Mm."
                        // fragments). Skip when the buffer is empty.
                        if !agent_text.trim().is_empty() {
                            OpenAiCallerBridge::emit_agent_final(&writer, &observer, &agent_text).await;
                        }
                        // max_turns reached after the agent replied — end the
                        // run (port of run_orchestrator.py:769 "max_turns").
                        if max_turns > 0 {
                            let turn = writer.lock().await.current_turn();
                            if turn >= max_turns {
                                let _ = end_tx.send(());
                                break;
                            }
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
    effects: crate::degradation::PcmEffectChain,
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
            // Degradation effects (Persona.speech_conditions.effects) — the
            // agent hears imperfect audio like a real caller (P3 port).
            let frame: Vec<i16> = if effects.is_empty() {
                frame
            } else {
                let bytes: Vec<u8> = frame.iter().flat_map(|s| s.to_le_bytes()).collect();
                crate::degradation::apply_effects(&effects, &bytes)
                    .chunks_exact(2)
                    .map(|b| i16::from_le_bytes([b[0], b[1]]))
                    .collect()
            };
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
