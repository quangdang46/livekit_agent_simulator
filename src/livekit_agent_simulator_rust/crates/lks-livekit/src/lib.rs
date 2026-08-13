//! lks-livekit — media/network for livekit-agent-simulator (P2+).
//!
//! Room connect + dispatch + caller bridges + run orchestration. Adds the
//! `livekit` (libwebrtc) and `gemini-live` deps; pure logic stays in lks-core.

pub mod callers;
pub mod dispatch;
pub mod room;
pub mod run;
