//! Decode parity: an AgentSessionMessage must decode correctly.

use agent_sim_proto::decode_agent_session_message;
use serde_json::json;

/// Hand-encode AgentSessionMessage { event: { agent_state_changed { old: 2, new: 3 } } }.
fn encode_agent_state(old: u64, new: u64) -> Vec<u8> {
    let mut inner = Vec::new();
    inner.push(0x08); // old_state field 1 varint
    inner.extend(varint_bytes(old));
    inner.push(0x10); // new_state field 2 varint
    inner.extend(varint_bytes(new));
    let mut evt = Vec::new();
    evt.push((10 << 3) | 2); // agent_state_changed field 10
    evt.extend(varint_bytes(inner.len() as u64));
    evt.extend(inner);
    let mut msg = Vec::new();
    msg.push((3 << 3) | 2); // event field 3
    msg.extend(varint_bytes(evt.len() as u64));
    msg.extend(evt);
    msg
}

fn varint_bytes(mut v: u64) -> Vec<u8> {
    let mut out = Vec::new();
    loop {
        let b = (v & 0x7f) as u8;
        v >>= 7;
        if v == 0 {
            out.push(b);
            return out;
        }
        out.push(b | 0x80);
    }
}

#[test]
fn decodes_agent_state_changed() {
    let bytes = encode_agent_state(2, 3);
    let out = decode_agent_session_message(&bytes).unwrap();
    let evt = out["event"].as_object().unwrap();
    let st = evt["agent_state_changed"].as_object().unwrap();
    assert_eq!(st["old_state"], json!("listening"));
    assert_eq!(st["new_state"], json!("thinking"));
}

#[test]
fn decodes_empty_message() {
    let out = decode_agent_session_message(&[]).unwrap();
    assert!(out.is_empty());
}

// --- tool events (fixture built by hand; wire numbers verified vs venv pb) ---

fn varint(v: u64) -> Vec<u8> {
    let mut out = Vec::new();
    let mut v = v;
    loop {
        let b = (v & 0x7f) as u8;
        v >>= 7;
        if v == 0 {
            out.push(b);
            break;
        }
        out.push(b | 0x80);
    }
    out
}

fn tag(field: u64, wire: u8) -> Vec<u8> {
    varint((field << 3) | wire as u64)
}

fn ld(field: u64, payload: &[u8]) -> Vec<u8> {
    let mut out = tag(field, 2);
    out.extend(varint(payload.len() as u64));
    out.extend_from_slice(payload);
    out
}

fn s(field: u64, text: &str) -> Vec<u8> {
    ld(field, text.as_bytes())
}

fn function_call(id: &str, call_id: &str, name: &str) -> Vec<u8> {
    let mut out = s(1, id);
    out.extend(s(2, call_id));
    out.extend(s(3, "{}"));
    out.extend(s(4, name));
    out
}

#[test]
fn decode_tool_execution_updated_started_and_ended() {
    // Started { function_call }
    let started_inner = ld(1, &function_call("c1", "call-1", "get_weather"));
    // ToolExecutionUpdated { started = 1 }
    let update = ld(1, &started_inner);
    // AgentSessionEvent { tool_execution_updated = 22 }
    let event = ld(22, &update);
    // AgentSessionMessage { event = 3 }
    let msg = ld(3, &event);

    let out = agent_sim_proto::decode_agent_session_message(&msg).unwrap();
    let call = &out["event"]["tool_execution_updated"]["started"]["function_call"];
    assert_eq!(call["id"], json!("c1"));
    assert_eq!(call["call_id"], json!("call-1"));
    assert_eq!(call["name"], json!("get_weather"));
    assert_eq!(call["arguments"], json!("{}"));

    // Ended { id=1, call_id=2, status=4 (TC_ERROR) }
    let mut ended = s(1, "c1");
    ended.extend(s(2, "call-1"));
    ended.extend(tag(4, 0));
    ended.extend(varint(2));
    let update = ld(4, &ended);
    let event = ld(22, &update);
    let msg = ld(3, &event);
    let out = agent_sim_proto::decode_agent_session_message(&msg).unwrap();
    let ended = &out["event"]["tool_execution_updated"]["ended"];
    assert_eq!(ended["call_id"], json!("call-1"));
    assert_eq!(ended["status"], json!("TC_ERROR"));
}

#[test]
fn decode_function_tools_started_and_handoff() {
    // FunctionToolsStarted { function_calls = [call] } (field 20)
    let started = ld(1, &function_call("c2", "call-2", "lookup"));
    let event = ld(20, &started);
    let msg = ld(3, &event);
    let out = agent_sim_proto::decode_agent_session_message(&msg).unwrap();
    let calls = &out["event"]["function_tools_started"]["function_calls"];
    assert_eq!(calls.as_array().unwrap().len(), 1);
    assert_eq!(calls[0]["name"], json!("lookup"));

    // ConversationItemAdded { item { agent_handoff { id, old, new } } } (field 12)
    let mut handoff = s(1, "h1");
    handoff.extend(s(2, "agent-a"));
    handoff.extend(s(3, "agent-b"));
    let chat_item = ld(4, &handoff); // ChatItem.agent_handoff = 4
    let added = ld(1, &chat_item); // ConversationItemAdded.item = 1
    let event = ld(12, &added);
    let msg = ld(3, &event);
    let out = agent_sim_proto::decode_agent_session_message(&msg).unwrap();
    let item = &out["event"]["conversation_item_added"]["item"];
    assert_eq!(item["type"], json!("agent_handoff"));
    assert_eq!(item["old_agent_id"], json!("agent-a"));
    assert_eq!(item["new_agent_id"], json!("agent-b"));
}
