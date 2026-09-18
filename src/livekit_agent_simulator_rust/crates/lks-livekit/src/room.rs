//! LiveKit room connection + event stream (port of `livekit/room.py` slice).
//!
//! P2 vertical slice: connect as the sim caller, watch for the agent's
//! published audio track, and expose the room event stream to the caller
//! bridge. Dispatch (AgentDispatchClient) lives in `dispatch.rs`.

use std::sync::Arc;
use std::time::Duration;

use livekit::prelude::*;
use livekit_data_stream::api::StreamReader;
use tokio::sync::broadcast;

use lks_core::errors::RunError;

/// A room event the sim layer cares about (normalized from RoomEvent).
#[derive(Debug, Clone)]
pub enum SimRoomEvent {
    TrackSubscribed {
        track_sid: String,
        participant_identity: String,
    },
    TrackPublished {
        track_sid: String,
        participant_identity: String,
    },
    ParticipantConnected {
        identity: String,
        name: String,
    },
    ParticipantDisconnected {
        identity: String,
    },
    Disconnected,
    ActiveSpeakersChanged {
        identities: Vec<String>,
    },
    DataReceived {
        topic: String,
        data: Vec<u8>,
        /// Sender identity (Python packet.participant.identity), if known.
        sender: Option<String>,
    },
    /// A fully-read `lk.transcription` text stream (port of observer.py's
    /// `_on_transcription_stream`/`_read_transcription`). Only emitted for
    /// the topic gated by `RoomObserveGate::lk_transcription`.
    TextStream {
        topic: String,
        participant_identity: String,
        text: String,
        final_: bool,
        segment_id: Option<String>,
    },
    /// A fully-read `lk.agent.session` byte stream (all chunks concatenated,
    /// one message per stream — port of agent_session_observer.py's framing
    /// note). Only emitted for the topic gated by
    /// `RoomObserveGate::lk_agent_session`.
    ByteStream {
        topic: String,
        participant_identity: String,
        data: Vec<u8>,
    },
    /// A text/byte stream failed to read or had no readable payload —
    /// port of observer.py's `observer.error {where: "lk.transcription", ...}`.
    StreamError {
        topic: String,
        error: String,
    },
}

/// Which SDK-standard telemetry streams `connect_room` should read and
/// surface as `SimRoomEvent`s. Mirrors `ObserveConfig.lk_transcription` /
/// `lk_agent_session` (Python `observer.py`/`agent_session_observer.py`
/// gate handler *registration* on these; the Rust SDK fires every stream-open
/// event regardless, so this is a post-hoc topic filter instead).
#[derive(Debug, Clone, Copy, Default)]
pub struct RoomObserveGate {
    pub lk_transcription: bool,
    pub lk_agent_session: bool,
}

const TOPIC_TRANSCRIPTION: &str = "lk.transcription";
const TOPIC_AGENT_SESSION: &str = "lk.agent.session";
const ATTR_TRANSCRIPTION_FINAL: &str = "lk.transcription_final";
const ATTR_SEGMENT_ID: &str = "lk.segment_id";

/// Hard ceiling on `Room::connect`. The SDK DOES have an internal
/// signal-connect timeout (`SIGNAL_CONNECT_TIMEOUT`, threaded through
/// `RoomOptions.connect_timeout`) plus `join_retries: 3` — but its
/// `livekit_runtime::spawn`ed session tasks and retry/backoff machinery can
/// still park a dead-endpoint attempt (e.g. `ws://localhost:7880` with no
/// server, as used by the MCP harness temp configs) well past those bounds.
/// This outer ceiling (incident: PR #109/#110 ubuntu/macos Rust CI,
/// 2026-09-18 — silent multi-minute stalls inside the MCP harness's
/// `execute_scenario`) bounds the whole attempt so a bad endpoint is a
/// fast, loud connect error instead of dead air.
///
/// HANG CLASS (proven over 15 CI attempts): `tokio::time::timeout` only
/// stops POLLING the inner future — it cannot abort work the SDK already
/// spawned onto the executor (`livekit_runtime::spawn` session/signal
/// tasks, native webrtc threads) or a native await that never resolves.
/// Those stragglers keep the RUNTIME alive: on a multi-thread runtime the
/// test's own timeout future eventually gets a worker and fires, but on a
/// single-thread/current_thread runtime the one thread is parked inside
/// the wedged await and the timeout itself never gets polled. Timeouts
/// bound well-behaved futures; they do NOT bound leaked native work.
/// The only hard guarantee is process exit (the CLI's shape) or never
/// starting the doomed attempt (preflight gate).
pub const ROOM_CONNECT_TIMEOUT: Duration = Duration::from_secs(15);

/// Connect to a LiveKit room and return (room handle, event receiver).
pub async fn connect_room(
    url: &str,
    token: &str,
    _room_name: &str,
    observe_gate: RoomObserveGate,
) -> Result<(Arc<Room>, broadcast::Receiver<SimRoomEvent>), RunError> {
    // NOTE: `Room::connect(...)` is evaluated EAGERLY here as the future
    // argument — by the time tokio::time::timeout wraps it, the SDK has
    // already started its internal connection machinery *outside* any
    // timeout. If that machinery parks (e.g. DNS/WS handshake against a
    // dead endpoint never resolving NOR erroring — PR #110 CI: room
    // connect + dispatch bounded yet execute_scenario still hung >60s),
    // the outer timeout fires but the leaked inner task keeps the runtime
    // (and the run) alive. `async { ... }` defers construction until first
    // poll — strictly INSIDE the timeout — so expiry drops the whole
    // attempt. Both shapes compile; only the lazy one actually bounds.
    let (room, mut events) = tokio::time::timeout(ROOM_CONNECT_TIMEOUT, async {
        Room::connect(url, token, RoomOptions::default()).await
    })
    .await
    .map_err(|_| {
        RunError(format!(
            "room connect timed out after {ROOM_CONNECT_TIMEOUT:?} to {url}"
        ))
    })?
    .map_err(|e| RunError(format!("room connect failed: {e}")))?;
    let room = Arc::new(room);

    let (tx, rx) = broadcast::channel(256);
    let room2 = room.clone();
    let tx2 = tx.clone();
    tokio::spawn(async move {
        while let Some(event) = events.recv().await {
            let sim = match event {
                RoomEvent::TrackSubscribed {
                    track: _,
                    publication,
                    participant,
                } => Some(SimRoomEvent::TrackSubscribed {
                    track_sid: publication.sid().to_string(),
                    participant_identity: participant.identity().to_string(),
                }),
                RoomEvent::TrackPublished {
                    publication,
                    participant,
                } => Some(SimRoomEvent::TrackPublished {
                    track_sid: publication.sid().to_string(),
                    participant_identity: participant.identity().to_string(),
                }),
                RoomEvent::ParticipantConnected(participant) => {
                    Some(SimRoomEvent::ParticipantConnected {
                        identity: participant.identity().to_string(),
                        name: participant.name().to_string(),
                    })
                }
                RoomEvent::ParticipantDisconnected(participant) => {
                    Some(SimRoomEvent::ParticipantDisconnected {
                        identity: participant.identity().to_string(),
                    })
                }
                RoomEvent::Disconnected { .. } => Some(SimRoomEvent::Disconnected),
                RoomEvent::ActiveSpeakersChanged { speakers } => {
                    Some(SimRoomEvent::ActiveSpeakersChanged {
                        identities: speakers.iter().map(|p| p.identity().to_string()).collect(),
                    })
                }
                RoomEvent::DataReceived {
                    payload,
                    topic,
                    participant,
                    ..
                } => {
                    let sender = participant.map(|p| p.identity().to_string());
                    Some(SimRoomEvent::DataReceived {
                        topic: topic.unwrap_or_default(),
                        data: payload.to_vec(),
                        sender,
                    })
                }
                RoomEvent::TextStreamOpened {
                    reader,
                    topic,
                    participant_identity,
                } => {
                    // Read must happen off the main loop (read_all() awaits
                    // full stream completion) — port of observer.py wrapping
                    // its handler in `asyncio.ensure_future(...)`.
                    if observe_gate.lk_transcription && topic == TOPIC_TRANSCRIPTION {
                        if let Some(reader) = reader.take() {
                            let tx3 = tx2.clone();
                            let participant_identity = participant_identity.to_string();
                            tokio::spawn(async move {
                                // Attributes MUST be read AFTER read_all()
                                // completes: the SDK's final flush carries
                                // lk.transcription_final=true on the CLOSE
                                // frame (room_io/_output.py _flush_task →
                                // writer.aclose(attributes=...)), so a
                                // snapshot at open time always says false.
                                // Matches Python observer.py _read_transcription
                                // (read_all() first, attrs second).
                                // Clone info BEFORE read_all (which takes
                                // self by value): TextStreamInfo's attribute
                                // map is Arc-shared with the stream manager,
                                // so the trailer's final=true (arriving with
                                // the close frame) is visible afterwards.
                                let info = reader.info().clone();
                                let sim = match reader.read_all().await {
                                    Ok(text) => {
                                        let attrs = info.attributes();
                                        let final_ = attrs
                                            .get(ATTR_TRANSCRIPTION_FINAL)
                                            .map(|v| v.eq_ignore_ascii_case("true"))
                                            .unwrap_or(false);
                                        let segment_id = attrs.get(ATTR_SEGMENT_ID).cloned();
                                        SimRoomEvent::TextStream {
                                            topic: TOPIC_TRANSCRIPTION.to_string(),
                                            participant_identity,
                                            text,
                                            final_,
                                            segment_id,
                                        }
                                    }
                                    Err(e) => SimRoomEvent::StreamError {
                                        topic: TOPIC_TRANSCRIPTION.to_string(),
                                        error: e.to_string(),
                                    },
                                };
                                let _ = tx3.send(sim);
                            });
                        }
                    }
                    None
                }
                RoomEvent::ByteStreamOpened {
                    reader,
                    topic,
                    participant_identity,
                } => {
                    if observe_gate.lk_agent_session && topic == TOPIC_AGENT_SESSION {
                        if let Some(reader) = reader.take() {
                            let tx3 = tx2.clone();
                            let participant_identity = participant_identity.to_string();
                            tokio::spawn(async move {
                                let sim = match reader.read_all().await {
                                    Ok(bytes) => SimRoomEvent::ByteStream {
                                        topic: TOPIC_AGENT_SESSION.to_string(),
                                        participant_identity,
                                        data: bytes.to_vec(),
                                    },
                                    Err(e) => SimRoomEvent::StreamError {
                                        topic: TOPIC_AGENT_SESSION.to_string(),
                                        error: e.to_string(),
                                    },
                                };
                                let _ = tx3.send(sim);
                            });
                        }
                    }
                    None
                }
                _ => None,
            };
            if let Some(sim) = sim {
                let _ = tx2.send(sim);
            }
        }
    });
    let _ = room2;

    Ok((room, rx))
}

/// Create a join token for the sim caller (room join grant only).
pub fn make_token(
    api_key: &str,
    api_secret: &str,
    identity: &str,
    room_name: &str,
) -> Result<String, RunError> {
    use livekit_api::access_token::{AccessToken, VideoGrants};

    let grants = VideoGrants {
        room_join: true,
        room: room_name.to_string(),
        ..Default::default()
    };

    let token = AccessToken::with_api_key(api_key, api_secret)
        .with_identity(identity)
        .with_grants(grants)
        .to_jwt()
        .map_err(|e| RunError(format!("token build failed: {e}")))?;
    Ok(token)
}
