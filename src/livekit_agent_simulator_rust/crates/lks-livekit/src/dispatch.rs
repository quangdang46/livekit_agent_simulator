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

/// Poll room participants until the agent joins (port of
/// `adapter.wait_for_agent` — AgentJoinTimeout on deadline). The agent is any
/// participant whose identity starts with "agent-" (LiveKit convention).
pub async fn wait_for_agent_join(
    api_url: &str,
    cfg: &lks_core::config::LiveKitConfig,
    room_name: &str,
) -> Result<String, RunError> {
    use livekit_api::services::room::RoomClient;
    let client = RoomClient::with_api_key(api_url, &cfg.api_key, &cfg.api_secret);
    let timeout_ms = cfg.agent_join_timeout_ms.max(1);
    let deadline = std::time::Instant::now() + std::time::Duration::from_millis(timeout_ms as u64);
    loop {
        match client.list_participants(room_name).await {
            Ok(resp) => {
                for p in resp {
                    if p.identity.starts_with("agent-") {
                        return Ok(p.identity);
                    }
                }
            }
            Err(e) => {
                return Err(RunError(format!("wait_for_agent list_participants: {e}")));
            }
        }
        if std::time::Instant::now() > deadline {
            return Err(RunError(format!(
                "Agent `{}` did not join room `{room_name}` within {timeout_ms}ms. \
                 Is the agent process running and registered with that exact agent_name?",
                cfg.agent_name
            )));
        }
        tokio::time::sleep(std::time::Duration::from_millis(500)).await;
    }
}
