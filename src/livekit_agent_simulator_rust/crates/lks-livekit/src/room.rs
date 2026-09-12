//! LiveKit room connection + event stream (port of `livekit/room.py` slice).
//!
//! P2 vertical slice: connect as the sim caller, watch for the agent's
//! published audio track, and expose the room event stream to the caller
//! bridge. Dispatch (AgentDispatchClient) lives in `dispatch.rs`.

use std::sync::Arc;

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
    StreamError { topic: String, error: String },
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

/// Connect to a LiveKit room and return (room handle, event receiver).
pub async fn connect_room(
    url: &str,
    token: &str,
    _room_name: &str,
    observe_gate: RoomObserveGate,
) -> Result<(Arc<Room>, broadcast::Receiver<SimRoomEvent>), RunError> {
    let (room, mut events) = Room::connect(url, token, RoomOptions::default())
        .await
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
                                let attrs = reader.info().attributes();
                                let final_ = attrs
                                    .get(ATTR_TRANSCRIPTION_FINAL)
                                    .map(|v| v.eq_ignore_ascii_case("true"))
                                    .unwrap_or(false);
                                let segment_id = attrs.get(ATTR_SEGMENT_ID).cloned();
                                let sim = match reader.read_all().await {
                                    Ok(text) => SimRoomEvent::TextStream {
                                        topic: TOPIC_TRANSCRIPTION.to_string(),
                                        participant_identity,
                                        text,
                                        final_,
                                        segment_id,
                                    },
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
