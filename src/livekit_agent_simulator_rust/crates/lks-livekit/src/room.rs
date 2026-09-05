//! LiveKit room connection + event stream (port of `livekit/room.py` slice).
//!
//! P2 vertical slice: connect as the sim caller, watch for the agent's
//! published audio track, and expose the room event stream to the caller
//! bridge. Dispatch (AgentDispatchClient) lives in `dispatch.rs`.

use std::sync::Arc;

use livekit::prelude::*;
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
}

/// Connect to a LiveKit room and return (room handle, event receiver).
pub async fn connect_room(
    url: &str,
    token: &str,
    _room_name: &str,
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
