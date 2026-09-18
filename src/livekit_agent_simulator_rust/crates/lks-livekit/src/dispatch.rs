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

    // Server-API HTTP calls have no built-in timeout: against a dead
    // endpoint (e.g. `ws://localhost:7880` → https in the MCP harness temp
    // configs, with no server listening) the `.await` below blocks
    // effectively forever. Same incident as ROOM_CONNECT_TIMEOUT
    // (PR #109 ubuntu/macos Rust CI, 2026-09-18): the 40-minute hang was
    // here, inside create_dispatch — not inside room connect. Bounded so a
    // dead endpoint is a fast, loud error instead of dead air.
    // Same lazy-future rule as connect_room (see its NOTE): the client call
    // must be constructed INSIDE the timeout's async block, not evaluated
    // eagerly as the timeout argument.
    let resp = tokio::time::timeout(std::time::Duration::from_secs(15), async {
        client.create_dispatch(req).await
    })
    .await
    .map_err(|_| RunError(format!("create dispatch timed out after 15s to {api_url}")))?
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
        // Same dead-endpoint rule as create_dispatch above: each HTTP poll
        // is bounded (15s) so an unreachable server API fails fast instead
        // of parking the whole run on a single .await (PR #110 CI:
        // harness execute_scenario hung >60s past room-connect + dispatch
        // timeouts, here in the list_participants loop with no server at
        // ws://localhost:7880).
        match tokio::time::timeout(
            std::time::Duration::from_secs(15),
            client.list_participants(room_name),
        )
        .await
        {
            Ok(Ok(resp)) => {
                for p in resp {
                    if p.identity.starts_with("agent-") {
                        return Ok(p.identity);
                    }
                }
            }
            Ok(Err(e)) => {
                return Err(RunError(format!("wait_for_agent list_participants: {e}")));
            }
            Err(_) => {
                return Err(RunError(format!(
                    "wait_for_agent list_participants timed out after 15s to {api_url}"
                )));
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
