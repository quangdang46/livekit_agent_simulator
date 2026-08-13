//! Smoke test: AgentSessionMessage round-trips through prost encode/decode.
//! Verifies the vendored protoc generated the expected wire types.

use agent_sim_proto::{
    agent_session_event, AgentSessionEvent, AgentSessionMessage, FunctionCall, FunctionCallOutput,
    SessionRequest, SessionResponse,
};
use prost::Message;

#[test]
fn agent_session_message_encode_decode() {
    // Build an AgentSessionMessage with an event: function_tools_started.
    let event = AgentSessionEvent {
        created_at: None,
        event: Some(agent_session_event::Event::FunctionToolsStarted(
            agent_session_event::FunctionToolsStarted {
                function_calls: vec![FunctionCall {
                    id: "fc-1".into(),
                    call_id: "call-1".into(),
                    arguments: r#"{"n":1}"#.into(),
                    name: "tool-a".into(),
                    created_at: None,
                }],
            },
        )),
    };
    let msg = AgentSessionMessage {
        message: Some(agent_sim_proto::agent_session_message::Message::Event(
            event,
        )),
    };
    let bytes = msg.encode_to_vec();
    assert!(!bytes.is_empty());

    // Decode round-trip.
    let decoded = AgentSessionMessage::decode(&bytes[..]).expect("decode");
    let AgentSessionMessage {
        message: Some(agent_sim_proto::agent_session_message::Message::Event(ev)),
        ..
    } = decoded
    else {
        panic!("expected event message");
    };
    let Some(agent_session_event::Event::FunctionToolsStarted(started)) = ev.event else {
        panic!("expected function_tools_started");
    };
    assert_eq!(started.function_calls.len(), 1);
    assert_eq!(started.function_calls[0].name, "tool-a");
    assert_eq!(started.function_calls[0].call_id, "call-1");
}

#[test]
fn session_request_response_roundtrip() {
    // A request with get_chat_history, and a matching response.
    let req = SessionRequest {
        request_id: "req-1".into(),
        request: Some(agent_sim_proto::session_request::Request::GetChatHistory(
            agent_sim_proto::session_request::GetChatHistory {},
        )),
    };
    let resp = SessionResponse {
        request_id: "req-1".into(),
        error: None,
        response: Some(agent_sim_proto::session_response::Response::GetChatHistory(
            agent_sim_proto::session_response::GetChatHistoryResponse { items: vec![] },
        )),
    };
    let req_bytes = req.encode_to_vec();
    let resp_bytes = resp.encode_to_vec();
    let req_back = SessionRequest::decode(&req_bytes[..]).expect("decode req");
    let resp_back = SessionResponse::decode(&resp_bytes[..]).expect("decode resp");
    assert_eq!(req_back.request_id, "req-1");
    assert_eq!(resp_back.request_id, "req-1");
    assert!(
        matches!(
            resp_back.response,
            Some(agent_sim_proto::session_response::Response::GetChatHistory(
                _
            ))
        ),
        "get_chat_history response"
    );
}

#[test]
fn function_call_output_fields() {
    let out = FunctionCallOutput {
        id: "fo-1".into(),
        name: "tool-a".into(),
        call_id: "call-1".into(),
        output: "{\"ok\":true}".into(),
        is_error: false,
        created_at: None,
    };
    let bytes = out.encode_to_vec();
    let back = FunctionCallOutput::decode(&bytes[..]).expect("decode");
    assert_eq!(back.call_id, "call-1");
    assert_eq!(back.output, "{\"ok\":true}");
    assert!(!back.is_error);
}
