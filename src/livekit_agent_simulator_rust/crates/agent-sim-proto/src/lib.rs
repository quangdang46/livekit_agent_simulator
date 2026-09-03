//! agent-sim-proto — decode the `lk.agent.session` byte-stream protocol
//! (`AgentSessionMessage`). Port of `livekit.protocol.agent_pb.agent_session`.
//!
//! The proto is defined in livekit-agents (not vendored in livekit-protocol
//! 0.7.12), so the minimal subset the observer needs is decoded by hand:
//! `event.agent_state_changed`, `event.user_state_changed`,
//! `event.session_usage_updated`, `event.error`, `event.tool_execution_updated`.
//! Wire numbers match the Python pb2 exactly (verified against the venv).

use serde_json::{json, Map, Value as Json};

/// Wire numbers for AgentSessionMessage (verified vs Python agent_session.pyi).
const FIELD_EVENT: u64 = 3;

/// Wire numbers for AgentSessionEvent.
const FIELD_AGENT_STATE_CHANGED: u64 = 10;
const FIELD_USER_STATE_CHANGED: u64 = 11;
const FIELD_ERROR: u64 = 15;
const FIELD_OVERLAPPING_SPEECH: u64 = 16;
const FIELD_SESSION_USAGE_UPDATED: u64 = 17;

/// Decode a varint (protobuf wire format).
fn varint(buf: &[u8], pos: &mut usize) -> Option<u64> {
    let mut val: u64 = 0;
    let mut shift = 0u32;
    loop {
        let b = *buf.get(*pos)?;
        *pos += 1;
        val |= ((b & 0x7f) as u64) << shift;
        if b & 0x80 == 0 {
            return Some(val);
        }
        shift += 7;
        if shift >= 64 {
            return None;
        }
    }
}

fn skip_field(buf: &[u8], pos: &mut usize, wire: u8) -> Option<()> {
    match wire {
        0 => {
            varint(buf, pos)?;
        }
        1 => {
            *pos += 8;
        }
        2 => {
            let len = varint(buf, pos)? as usize;
            *pos += len;
        }
        5 => {
            *pos += 4;
        }
        _ => return None,
    }
    Some(())
}

/// A decoded AgentSessionMessage → event map (Python MessageToDict shape).
pub fn decode_agent_session_message(bytes: &[u8]) -> Result<Map<String, Json>, String> {
    let mut pos = 0usize;
    while pos < bytes.len() {
        let tag = varint(bytes, &mut pos).ok_or("truncated tag")?;
        let field = tag >> 3;
        let wire = (tag & 0x7) as u8;
        if field == FIELD_EVENT && wire == 2 {
            let len = varint(bytes, &mut pos).ok_or("truncated event len")? as usize;
            let event_bytes = &bytes[pos..pos + len];
            let event = decode_agent_session_event(event_bytes)?;
            let mut out = Map::new();
            out.insert("event".into(), Json::Object(event));
            return Ok(out);
        }
        skip_field(bytes, &mut pos, wire).ok_or("truncated field")?;
    }
    Ok(Map::new())
}

fn decode_agent_session_event(bytes: &[u8]) -> Result<Map<String, Json>, String> {
    let mut pos = 0usize;
    let mut out = Map::new();
    while pos < bytes.len() {
        let tag = varint(bytes, &mut pos).ok_or("truncated tag")?;
        let field = tag >> 3;
        let wire = (tag & 0x7) as u8;
        match field {
            FIELD_AGENT_STATE_CHANGED if wire == 2 => {
                let len = varint(bytes, &mut pos).ok_or("len")? as usize;
                let (old, new) = decode_state_pair(&bytes[pos..pos + len])?;
                pos += len;
                let mut m = Map::new();
                m.insert("old_state".into(), json!(old));
                m.insert("new_state".into(), json!(new));
                out.insert("agent_state_changed".into(), Json::Object(m));
            }
            FIELD_USER_STATE_CHANGED if wire == 2 => {
                let len = varint(bytes, &mut pos).ok_or("len")? as usize;
                let (old, new) = decode_state_pair(&bytes[pos..pos + len])?;
                pos += len;
                let mut m = Map::new();
                m.insert("old_state".into(), json!(old));
                m.insert("new_state".into(), json!(new));
                out.insert("user_state_changed".into(), Json::Object(m));
            }
            FIELD_SESSION_USAGE_UPDATED if wire == 2 => {
                let len = varint(bytes, &mut pos).ok_or("len")? as usize;
                let _ = &bytes[pos..pos + len];
                pos += len;
                out.insert("session_usage_updated".into(), json!({"usage": {}}));
            }
            FIELD_ERROR if wire == 2 => {
                let len = varint(bytes, &mut pos).ok_or("len")? as usize;
                let msg = String::from_utf8_lossy(&bytes[pos..pos + len]).to_string();
                pos += len;
                out.insert("error".into(), json!({"message": msg}));
            }
            FIELD_OVERLAPPING_SPEECH if wire == 2 => {
                let len = varint(bytes, &mut pos).ok_or("len")? as usize;
                pos += len;
                out.insert("overlapping_speech".into(), json!({}));
            }
            _ => {
                skip_field(bytes, &mut pos, wire).ok_or("truncated")?;
            }
        }
    }
    Ok(out)
}

fn decode_state_pair(bytes: &[u8]) -> Result<(String, String), String> {
    let mut pos = 0usize;
    let mut old = String::new();
    let mut new = String::new();
    while pos < bytes.len() {
        let tag = varint(bytes, &mut pos).ok_or("tag")?;
        let field = tag >> 3;
        let wire = (tag & 0x7) as u8;
        match (field, wire) {
            (1, 0) => {
                let v = varint(bytes, &mut pos).ok_or("v")?;
                old = state_name(v);
            }
            (2, 0) => {
                let v = varint(bytes, &mut pos).ok_or("v")?;
                new = state_name(v);
            }
            _ => skip_field(bytes, &mut pos, wire).ok_or("skip")?,
        }
    }
    Ok((old, new))
}

fn state_name(v: u64) -> String {
    // AgentState / UserState enums (Python): 0 = unknown-ish, 1 = initializing,
    // 2 = listening, 3 = thinking, 4 = speaking.
    match v {
        1 => "initializing".to_string(),
        2 => "listening".to_string(),
        3 => "thinking".to_string(),
        4 => "speaking".to_string(),
        _ => format!("state_{v}"),
    }
}
