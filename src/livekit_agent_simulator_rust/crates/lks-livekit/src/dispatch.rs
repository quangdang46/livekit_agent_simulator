//! Agent dispatch — create an AgentDispatch on the target (port of
//! `livekit/dispatch.py` slice). Uses the LiveKit server API.

use livekit_api::services::agent_dispatch::AgentDispatchClient;
use livekit_protocol::CreateAgentDispatchRequest;
use lks_core::errors::RunError;

/// Create a dispatch so the configured agent joins the sim room.
pub async fn create_dispatch(
    api_url: &str,
    api_key: &str,
    api_secret: &str,
    room_name: &str,
    agent_name: &str,
    metadata: Option<&str>,
) -> Result<String, RunError> {
    let client = AgentDispatchClient::with_api_key(api_url, api_key, api_secret);

    let req = CreateAgentDispatchRequest {
        room: room_name.to_string(),
        agent_name: agent_name.to_string(),
        metadata: metadata.unwrap_or("").to_string(),
        ..Default::default()
    };

    let resp = client
        .create_dispatch(req)
        .await
        .map_err(|e| RunError(format!("create dispatch failed: {e}")))?;
    Ok(resp.id)
}
