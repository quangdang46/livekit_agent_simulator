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
const FIELD_CONVERSATION_ITEM_ADDED: u64 = 12;
const FIELD_FUNCTION_TOOLS_EXECUTED: u64 = 14;
const FIELD_ERROR: u64 = 15;
const FIELD_OVERLAPPING_SPEECH: u64 = 16;
const FIELD_SESSION_USAGE_UPDATED: u64 = 17;
const FIELD_FUNCTION_TOOLS_STARTED: u64 = 20;
const FIELD_TOOL_EXECUTION_UPDATED: u64 = 22;

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
            FIELD_FUNCTION_TOOLS_STARTED if wire == 2 => {
                let len = varint(bytes, &mut pos).ok_or("len")? as usize;
                let calls = decode_repeated_function_calls(&bytes[pos..pos + len])?;
                pos += len;
                out.insert(
                    "function_tools_started".into(),
                    json!({"function_calls": calls}),
                );
            }
            FIELD_FUNCTION_TOOLS_EXECUTED if wire == 2 => {
                let len = varint(bytes, &mut pos).ok_or("len")? as usize;
                let (calls, outputs) = decode_function_tools_executed(&bytes[pos..pos + len])?;
                pos += len;
                out.insert(
                    "function_tools_executed".into(),
                    json!({"function_calls": calls, "function_call_outputs": outputs}),
                );
            }
            FIELD_TOOL_EXECUTION_UPDATED if wire == 2 => {
                let len = varint(bytes, &mut pos).ok_or("len")? as usize;
                let update = decode_tool_execution_updated(&bytes[pos..pos + len])?;
                pos += len;
                out.insert("tool_execution_updated".into(), Json::Object(update));
            }
            FIELD_CONVERSATION_ITEM_ADDED if wire == 2 => {
                let len = varint(bytes, &mut pos).ok_or("len")? as usize;
                let added = decode_conversation_item_added(&bytes[pos..pos + len])?;
                pos += len;
                out.insert("conversation_item_added".into(), Json::Object(added));
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

/// ToolCallStatus enum (livekit.agent.ToolCallStatus).
fn tool_status_name(v: u64) -> String {
    match v {
        0 => "TC_RUNNING".to_string(),
        1 => "TC_DONE".to_string(),
        2 => "TC_ERROR".to_string(),
        3 => "TC_CANCELLED".to_string(),
        _ => format!("{v}"),
    }
}

/// FunctionCall — Python _function_call_spec shape.
fn decode_function_call(bytes: &[u8]) -> Result<Json, String> {
    let mut pos = 0usize;
    let mut id = String::new();
    let mut call_id = String::new();
    let mut arguments = String::new();
    let mut name = String::new();
    while pos < bytes.len() {
        let tag = varint(bytes, &mut pos).ok_or("tag")?;
        let field = tag >> 3;
        let wire = (tag & 0x7) as u8;
        if wire == 2 {
            let len = varint(bytes, &mut pos).ok_or("len")? as usize;
            let s = String::from_utf8_lossy(&bytes[pos..pos + len]).to_string();
            match field {
                1 => id = s,
                2 => call_id = s,
                3 => arguments = s,
                4 => name = s,
                _ => {}
            }
            pos += len;
        } else {
            skip_field(bytes, &mut pos, wire).ok_or("skip")?;
        }
    }
    Ok(json!({
        "id": if id.is_empty() { Json::Null } else { json!(id) },
        "call_id": if call_id.is_empty() { Json::Null } else { json!(call_id) },
        "name": if name.is_empty() { Json::Null } else { json!(name) },
        "arguments": arguments,
    }))
}

/// FunctionCallOutput — Python _function_output_spec shape.
fn decode_function_call_output(bytes: &[u8]) -> Result<Json, String> {
    let mut pos = 0usize;
    let mut id = String::new();
    let mut name = String::new();
    let mut call_id = String::new();
    let mut output = String::new();
    let mut is_error = false;
    while pos < bytes.len() {
        let tag = varint(bytes, &mut pos).ok_or("tag")?;
        let field = tag >> 3;
        let wire = (tag & 0x7) as u8;
        match (field, wire) {
            (5, 0) => is_error = varint(bytes, &mut pos).ok_or("v")? != 0,
            _ if wire == 2 => {
                let len = varint(bytes, &mut pos).ok_or("len")? as usize;
                let s = String::from_utf8_lossy(&bytes[pos..pos + len]).to_string();
                match field {
                    1 => id = s,
                    2 => name = s,
                    3 => call_id = s,
                    4 => output = s,
                    _ => {}
                }
                pos += len;
            }
            _ => skip_field(bytes, &mut pos, wire).ok_or("skip")?,
        }
    }
    Ok(json!({
        "id": if id.is_empty() { Json::Null } else { json!(id) },
        "call_id": if call_id.is_empty() { Json::Null } else { json!(call_id) },
        "name": if name.is_empty() { Json::Null } else { json!(name) },
        "output": output,
        "is_error": is_error,
    }))
}

/// Repeated FunctionCall (FunctionToolsStarted.function_calls / field 1).
fn decode_repeated_function_calls(bytes: &[u8]) -> Result<Vec<Json>, String> {
    let mut pos = 0usize;
    let mut calls = Vec::new();
    while pos < bytes.len() {
        let tag = varint(bytes, &mut pos).ok_or("tag")?;
        let field = tag >> 3;
        let wire = (tag & 0x7) as u8;
        if field == 1 && wire == 2 {
            let len = varint(bytes, &mut pos).ok_or("len")? as usize;
            calls.push(decode_function_call(&bytes[pos..pos + len])?);
            pos += len;
        } else {
            skip_field(bytes, &mut pos, wire).ok_or("skip")?;
        }
    }
    Ok(calls)
}

/// FunctionToolsExecuted: function_calls (1) + function_call_outputs (2).
fn decode_function_tools_executed(bytes: &[u8]) -> Result<(Vec<Json>, Vec<Json>), String> {
    let mut pos = 0usize;
    let mut calls = Vec::new();
    let mut outputs = Vec::new();
    while pos < bytes.len() {
        let tag = varint(bytes, &mut pos).ok_or("tag")?;
        let field = tag >> 3;
        let wire = (tag & 0x7) as u8;
        if wire == 2 {
            let len = varint(bytes, &mut pos).ok_or("len")? as usize;
            match field {
                1 => calls.push(decode_function_call(&bytes[pos..pos + len])?),
                2 => outputs.push(decode_function_call_output(&bytes[pos..pos + len])?),
                _ => {}
            }
            pos += len;
        } else {
            skip_field(bytes, &mut pos, wire).ok_or("skip")?;
        }
    }
    Ok((calls, outputs))
}

/// ToolExecutionUpdated oneof: started(1) | call_updated(2) | reply_updated(3) | ended(4).
fn decode_tool_execution_updated(bytes: &[u8]) -> Result<Map<String, Json>, String> {
    let mut pos = 0usize;
    let mut out = Map::new();
    while pos < bytes.len() {
        let tag = varint(bytes, &mut pos).ok_or("tag")?;
        let field = tag >> 3;
        let wire = (tag & 0x7) as u8;
        if wire != 2 {
            skip_field(bytes, &mut pos, wire).ok_or("skip")?;
            continue;
        }
        let len = varint(bytes, &mut pos).ok_or("len")? as usize;
        let inner = &bytes[pos..pos + len];
        pos += len;
        match field {
            1 => {
                // Started { function_call = 1 }
                let mut pos2 = 0usize;
                let mut call = json!({});
                while pos2 < inner.len() {
                    let t2 = varint(inner, &mut pos2).ok_or("tag")?;
                    let f2 = t2 >> 3;
                    let w2 = (t2 & 0x7) as u8;
                    if f2 == 1 && w2 == 2 {
                        let l2 = varint(inner, &mut pos2).ok_or("len")? as usize;
                        call = decode_function_call(&inner[pos2..pos2 + l2])?;
                        pos2 += l2;
                    } else {
                        skip_field(inner, &mut pos2, w2).ok_or("skip")?;
                    }
                }
                out.insert("started".into(), json!({"function_call": call}));
            }
            4 => {
                // Ended { id=1, call_id=2, message=3, status=4 }
                let mut pos2 = 0usize;
                let mut id = String::new();
                let mut call_id = String::new();
                let mut message = String::new();
                let mut has_message = false;
                let mut status = String::new();
                while pos2 < inner.len() {
                    let t2 = varint(inner, &mut pos2).ok_or("tag")?;
                    let f2 = t2 >> 3;
                    let w2 = (t2 & 0x7) as u8;
                    match (f2, w2) {
                        (4, 0) => status = tool_status_name(varint(inner, &mut pos2).ok_or("v")?),
                        _ if w2 == 2 => {
                            let l2 = varint(inner, &mut pos2).ok_or("len")? as usize;
                            let s = String::from_utf8_lossy(&inner[pos2..pos2 + l2]).to_string();
                            match f2 {
                                1 => id = s,
                                2 => call_id = s,
                                3 => {
                                    message = s;
                                    has_message = true;
                                }
                                _ => {}
                            }
                            pos2 += l2;
                        }
                        _ => skip_field(inner, &mut pos2, w2).ok_or("skip")?,
                    }
                }
                out.insert(
                    "ended".into(),
                    json!({
                        "id": id,
                        "call_id": call_id,
                        "message": if has_message { json!(message) } else { Json::Null },
                        "status": status,
                    }),
                );
            }
            _ => {}
        }
    }
    Ok(out)
}

/// ConversationItemAdded { item = 1 (ChatContext.ChatItem oneof) } — surfaces
/// function_call chat items (tool.start fallback) and agent_handoff items.
fn decode_conversation_item_added(bytes: &[u8]) -> Result<Map<String, Json>, String> {
    let mut pos = 0usize;
    let mut item = Map::new();
    while pos < bytes.len() {
        let tag = varint(bytes, &mut pos).ok_or("tag")?;
        let field = tag >> 3;
        let wire = (tag & 0x7) as u8;
        if wire != 2 {
            skip_field(bytes, &mut pos, wire).ok_or("skip")?;
            continue;
        }
        let len = varint(bytes, &mut pos).ok_or("len")? as usize;
        let inner = &bytes[pos..pos + len];
        pos += len;
        if field != 1 {
            continue;
        }
        // ChatItem: message=1 | function_call=2 | function_call_output=3 |
        // agent_handoff=4 | agent_config_update=5
        let mut pos2 = 0usize;
        while pos2 < inner.len() {
            let t2 = varint(inner, &mut pos2).ok_or("tag")?;
            let f2 = t2 >> 3;
            let w2 = (t2 & 0x7) as u8;
            if w2 != 2 {
                skip_field(inner, &mut pos2, w2).ok_or("skip")?;
                continue;
            }
            let l2 = varint(inner, &mut pos2).ok_or("len")? as usize;
            let payload = &inner[pos2..pos2 + l2];
            pos2 += l2;
            match f2 {
                2 => {
                    let mut call = decode_function_call(payload)?;
                    call["type"] = json!("function_call");
                    item = call.as_object().cloned().unwrap_or_default();
                }
                3 => {
                    let mut out = decode_function_call_output(payload)?;
                    out["type"] = json!("function_call_output");
                    item = out.as_object().cloned().unwrap_or_default();
                }
                4 => {
                    // AgentHandoff { id=1, old_agent_id=2, new_agent_id=3 }
                    let mut pos3 = 0usize;
                    let mut id = String::new();
                    let mut old_agent_id = String::new();
                    let mut new_agent_id = String::new();
                    while pos3 < payload.len() {
                        let t3 = varint(payload, &mut pos3).ok_or("tag")?;
                        let f3 = t3 >> 3;
                        let w3 = (t3 & 0x7) as u8;
                        if w3 == 2 {
                            let l3 = varint(payload, &mut pos3).ok_or("len")? as usize;
                            let s = String::from_utf8_lossy(&payload[pos3..pos3 + l3]).to_string();
                            match f3 {
                                1 => id = s,
                                2 => old_agent_id = s,
                                3 => new_agent_id = s,
                                _ => {}
                            }
                            pos3 += l3;
                        } else {
                            skip_field(payload, &mut pos3, w3).ok_or("skip")?;
                        }
                    }
                    item.insert("type".into(), json!("agent_handoff"));
                    item.insert("id".into(), json!(id));
                    item.insert("old_agent_id".into(), json!(old_agent_id));
                    item.insert("new_agent_id".into(), json!(new_agent_id));
                }
                _ => {}
            }
        }
    }
    let mut out = Map::new();
    out.insert("item".into(), Json::Object(item));
    Ok(out)
}
