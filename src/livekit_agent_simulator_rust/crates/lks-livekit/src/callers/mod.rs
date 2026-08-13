//! Caller bridges — the simulated human speaking with the agent (P2 slice).
//!
//! `OpenAiCallerBridge`: hand-rolled OpenAI Realtime WebSocket bridge
//! (port of `callers/openai.py` minimal slice): session.update with VAD off,
//! streams the agent's room audio into the input buffer, plays model audio
//! back into the room, emits transcript + connection events.

pub mod openai;

pub use openai::OpenAiCallerBridge;
