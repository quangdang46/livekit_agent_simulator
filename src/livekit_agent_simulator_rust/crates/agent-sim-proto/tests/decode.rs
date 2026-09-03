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
