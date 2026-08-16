//! Sim legs (P6 slice): inbound_sip — the simulated caller dials the agent's
//! inbound DID via a LiveKit SIP trunk (Cloud hairpin). Port of
//! `livekit/sim_leg/inbound.py` core flow.

use std::sync::Arc;

use livekit_api::services::sip::{CreateSIPParticipantOptions, SIPClient};
use lks_core::config::SimConfig;
use lks_core::errors::RunError;
use lks_core::logging::event::EventWriter;
use tokio::sync::Mutex;

use crate::room::{connect_room, make_token};

/// Run the inbound_sip leg: create the sim room, place the SIP call to the
/// agent's dial_in number, then connect the caller bridge in that room.
pub async fn run_inbound_sip(
    cfg: &SimConfig,
    scenario: &lks_core::scenario::Scenario,
    run_id: &str,
    persona_prompt: String,
    writer: Arc<Mutex<EventWriter>>,
    provider: &str,
) -> Result<(), RunError> {
    let tel = &scenario.telephony;
    let outbound_trunk = tel
        .as_ref()
        .and_then(|t| t.sip_trunk_id.clone())
        .filter(|s| !s.is_empty())
        .or_else(|| {
            cfg.telephony
                .outbound_trunk_id
                .clone()
                .filter(|s| !s.is_empty())
        });
    let Some(trunk) = outbound_trunk else {
        return Err(RunError(
            "inbound_sip requires telephony.outbound_trunk_id (config) or Telephony.sip_trunk_id (scenario) to place the call."
                .into(),
        ));
    };
    let dial_in = tel
        .as_ref()
        .and_then(|t| t.call_to.clone())
        .filter(|s| !s.is_empty())
        .or_else(|| cfg.telephony.dial_in.clone().filter(|s| !s.is_empty()));
    let Some(dial_in) = dial_in else {
        return Err(RunError(
            "inbound_sip requires Telephony.dial_in or config telephony.dial_in (agent-side inbound DID)."
                .into(),
        ));
    };

    let sim_room_name = format!("lks-sip-{run_id}");
    let identity = "lks-caller";

    // 1. Connect the sim caller to the sim room (Cloud creates it implicitly).
    let token = make_token(
        &cfg.livekit.api_key,
        &cfg.livekit.api_secret,
        identity,
        &sim_room_name,
    )?;
    let (room, _room_events) = connect_room(&cfg.livekit.url, &token, &sim_room_name).await?;

    let mut w = writer.lock().await;
    w.emit(
        "sim.connected",
        Some(
            &serde_json::json!({
                "identity": identity,
                "room": sim_room_name,
                "mode": "inbound_sip",
            })
            .as_object()
            .cloned()
            .unwrap_or_default(),
        ),
        "sim",
        None,
        None,
        false,
        None,
    );
    drop(w);

    // 2. Place the SIP call from the sim room to the agent's DID.
    let api_host = cfg
        .livekit
        .url
        .replace("wss://", "https://")
        .replace("ws://", "https://");
    let sip = SIPClient::with_api_key(&api_host, &cfg.livekit.api_key, &cfg.livekit.api_secret);
    let sip_identity = format!("sip-in-{}", &run_id[..12]);
    let wait = tel
        .as_ref()
        .and_then(|t| t.wait_until_answered)
        .unwrap_or(true);

    let mut w = writer.lock().await;
    w.emit(
        "inbound.dial_started",
        Some(
            &serde_json::json!({
                "dial_in": dial_in,
                "room": sim_room_name,
                "participant_identity": sip_identity,
                "wait_until_answered": wait,
            })
            .as_object()
            .cloned()
            .unwrap_or_default(),
        ),
        "sim",
        None,
        None,
        false,
        None,
    );
    drop(w);

    let options = CreateSIPParticipantOptions {
        participant_identity: sip_identity.clone(),
        participant_name: Some("Simulated Caller".into()),
        wait_until_answered: Some(wait),
        ..Default::default()
    };
    let sip_info = sip
        .create_sip_participant(
            trunk.clone(),
            dial_in.clone(),
            sim_room_name.clone(),
            options,
            None,
        )
        .await
        .map_err(|e| RunError(format!("inbound dial failed: {e}")))?;

    let mut w = writer.lock().await;
    w.emit(
        "inbound.answered",
        Some(
            &serde_json::json!({
                "dial_in": dial_in,
                "participant_identity": sip_info.participant_identity,
                "sip_call_id": sip_info.sip_call_id,
            })
            .as_object()
            .cloned()
            .unwrap_or_default(),
        ),
        "sim",
        None,
        None,
        false,
        None,
    );
    drop(w);

    // 3. Run the caller bridge in the sim room (the agent joined via SIP).
    let provider = provider.trim().to_lowercase();
    if provider == "google" {
        let bridge = crate::callers::GeminiCallerBridge::new(
            cfg.livekit.clone(),
            cfg.simulator.clone(),
            persona_prompt,
            sim_room_name.clone(),
            identity.to_string(),
            writer.clone(),
        );
        bridge
            .run(tokio::sync::broadcast::channel::<()>(1).1)
            .await?;
    } else {
        let bridge = crate::callers::OpenAiCallerBridge::new(
            cfg.livekit.clone(),
            cfg.simulator.clone(),
            persona_prompt,
            "agent".to_string(),
            0,
            sim_room_name.clone(),
            identity.to_string(),
            writer.clone(),
        );
        bridge
            .run(tokio::sync::broadcast::channel::<()>(1).1)
            .await?;
    }
    let _ = room;
    Ok(())
}

// ---------------------------------------------------------------------------
// outbound_sim_callee — agent-room dials call_to (sim DID); Gemini answers on sim-room.
// ---------------------------------------------------------------------------
pub async fn run_outbound_sim_callee(
    cfg: &SimConfig,
    scenario: &lks_core::scenario::Scenario,
    run_id: &str,
    persona_prompt: String,
    writer: Arc<Mutex<EventWriter>>,
    provider: &str,
) -> Result<(), RunError> {
    use livekit_api::services::sip::{CreateSIPParticipantOptions, SIPClient};

    let tel = &scenario.telephony;
    let trunk = tel
        .as_ref()
        .and_then(|t| t.sip_trunk_id.clone())
        .filter(|s| !s.is_empty())
        .or_else(|| cfg.telephony.outbound_trunk_id.clone().filter(|s| !s.is_empty()))
        .ok_or_else(|| RunError("outbound_sim_callee requires telephony.outbound_trunk_id (config) or Telephony.sip_trunk_id (scenario).".into()))?;
    let call_to = tel
        .as_ref()
        .and_then(|t| t.call_to.clone())
        .filter(|s| !s.is_empty())
        .or_else(|| cfg.telephony.sim_inbound_number.clone().filter(|s| !s.is_empty()))
        .ok_or_else(|| RunError("outbound_sim_callee requires Telephony.call_to or config telephony.sim_inbound_number (DID/number Gemini answers).".into()))?;

    let sim_room_name = format!("lks-sip-{run_id}");
    let agent_room_name = format!("lks-{run_id}");
    let identity = "lks-caller";

    // Gemini joins the sim-room first (ready when the SIP leg lands).
    let token = make_token(
        &cfg.livekit.api_key,
        &cfg.livekit.api_secret,
        identity,
        &sim_room_name,
    )?;
    let (sim_room, _) = connect_room(&cfg.livekit.url, &token, &sim_room_name).await?;
    let mut w = writer.lock().await;
    w.emit("sim.connected",
        Some(&serde_json::json!({"identity": identity, "room": sim_room_name, "mode": "outbound_sim_callee"}).as_object().cloned().unwrap_or_default()),
        "sim", None, None, false, None);
    drop(w);

    // Dispatch the agent into the agent-room.
    let api_host = cfg
        .livekit
        .url
        .replace("wss://", "https://")
        .replace("ws://", "https://");
    let dispatch_id = crate::dispatch::create_dispatch(
        &api_host,
        &cfg.livekit.api_key,
        &cfg.livekit.api_secret,
        &agent_room_name,
        &cfg.livekit.agent_name,
        None,
    )
    .await?;
    let mut w = writer.lock().await;
    w.emit("dispatch.agent_joined",
        Some(&serde_json::json!({"identity": "agent", "mode": "outbound_sim_callee", "dispatch_id": dispatch_id}).as_object().cloned().unwrap_or_default()),
        "sim", None, None, false, None);
    drop(w);

    // Join the agent-room as observer (capture agent audio from the start).
    let obs_token = make_token(
        &cfg.livekit.api_key,
        &cfg.livekit.api_secret,
        &format!("lks-obs-{}", &run_id[..8]),
        &agent_room_name,
    )?;
    let (agent_room, _) = connect_room(&cfg.livekit.url, &obs_token, &agent_room_name).await?;

    // Dial call_to from the agent-room (the agent's outbound call to the sim DID).
    let sip = SIPClient::with_api_key(&api_host, &cfg.livekit.api_key, &cfg.livekit.api_secret);
    let sip_identity = format!("sip-out-{}", &run_id[..12]);
    let wait = tel
        .as_ref()
        .and_then(|t| t.wait_until_answered)
        .unwrap_or(true);
    let mut w = writer.lock().await;
    w.emit("outbound.dial_started",
        Some(&serde_json::json!({"call_to": call_to, "room": agent_room_name, "participant_identity": sip_identity, "wait_until_answered": wait, "mode": "outbound_sim_callee"}).as_object().cloned().unwrap_or_default()),
        "sim", None, None, false, None);
    drop(w);
    let options = CreateSIPParticipantOptions {
        participant_identity: sip_identity.clone(),
        participant_name: Some("Simulated Callee".into()),
        wait_until_answered: Some(wait),
        ..Default::default()
    };
    let sip_info = sip
        .create_sip_participant(
            trunk.clone(),
            call_to.clone(),
            agent_room_name.clone(),
            options,
            None,
        )
        .await
        .map_err(|e| RunError(format!("outbound_sim_callee dial failed: {e}")))?;
    let mut w = writer.lock().await;
    w.emit("outbound.dial_answered",
        Some(&serde_json::json!({"participant_identity": sip_info.participant_identity, "mode": "outbound_sim_callee"}).as_object().cloned().unwrap_or_default()),
        "sim", None, None, false, None);
    drop(w);

    // Run the caller bridge in the sim-room (Gemini is the callee).
    let provider = provider.trim().to_lowercase();
    if provider == "google" {
        let bridge = crate::callers::GeminiCallerBridge::new(
            cfg.livekit.clone(),
            cfg.simulator.clone(),
            persona_prompt,
            sim_room_name.clone(),
            identity.to_string(),
            writer.clone(),
        );
        bridge
            .run(tokio::sync::broadcast::channel::<()>(1).1)
            .await?;
    } else {
        let bridge = crate::callers::OpenAiCallerBridge::new(
            cfg.livekit.clone(),
            cfg.simulator.clone(),
            persona_prompt,
            "agent".to_string(),
            0,
            sim_room_name.clone(),
            identity.to_string(),
            writer.clone(),
        );
        bridge
            .run(tokio::sync::broadcast::channel::<()>(1).1)
            .await?;
    }
    let _ = (sim_room, agent_room);
    Ok(())
}

// ---------------------------------------------------------------------------
// agent_dials — dispatch only; wait for the SIP participant the agent creates.
// ---------------------------------------------------------------------------
pub async fn run_agent_dials(
    cfg: &SimConfig,
    _scenario: &lks_core::scenario::Scenario,
    run_id: &str,
    persona_prompt: String,
    writer: Arc<Mutex<EventWriter>>,
    provider: &str,
) -> Result<(), RunError> {
    let sim_room_name = format!("lks-sip-{run_id}");
    let agent_room_name = format!("lks-{run_id}");
    let identity = "lks-caller";

    let token = make_token(
        &cfg.livekit.api_key,
        &cfg.livekit.api_secret,
        identity,
        &sim_room_name,
    )?;
    let (sim_room, _) = connect_room(&cfg.livekit.url, &token, &sim_room_name).await?;
    let mut w = writer.lock().await;
    w.emit("sim.connected",
        Some(&serde_json::json!({"identity": identity, "room": sim_room_name, "mode": "agent_dials"}).as_object().cloned().unwrap_or_default()),
        "sim", None, None, false, None);
    drop(w);

    let api_host = cfg
        .livekit
        .url
        .replace("wss://", "https://")
        .replace("ws://", "https://");
    let dispatch_id = crate::dispatch::create_dispatch(
        &api_host,
        &cfg.livekit.api_key,
        &cfg.livekit.api_secret,
        &agent_room_name,
        &cfg.livekit.agent_name,
        None,
    )
    .await?;
    let mut w = writer.lock().await;
    w.emit("outbound.wait_agent_dial",
        Some(&serde_json::json!({"room": agent_room_name, "note": "waiting for agent to create SIP participant", "dispatch_id": dispatch_id}).as_object().cloned().unwrap_or_default()),
        "sim", None, None, false, None);
    drop(w);

    // The agent creates the SIP participant; Gemini answers in the sim-room.
    let provider = provider.trim().to_lowercase();
    if provider == "google" {
        let bridge = crate::callers::GeminiCallerBridge::new(
            cfg.livekit.clone(),
            cfg.simulator.clone(),
            persona_prompt,
            sim_room_name.clone(),
            identity.to_string(),
            writer.clone(),
        );
        bridge
            .run(tokio::sync::broadcast::channel::<()>(1).1)
            .await?;
    } else {
        let bridge = crate::callers::OpenAiCallerBridge::new(
            cfg.livekit.clone(),
            cfg.simulator.clone(),
            persona_prompt,
            "agent".to_string(),
            0,
            sim_room_name.clone(),
            identity.to_string(),
            writer.clone(),
        );
        bridge
            .run(tokio::sync::broadcast::channel::<()>(1).1)
            .await?;
    }
    let _ = sim_room;
    Ok(())
}

// ---------------------------------------------------------------------------
// outbound_human_pickup — dial a human/PSTN into the agent-room; Gemini speaks there.
// ---------------------------------------------------------------------------
pub async fn run_outbound_human_pickup(
    cfg: &SimConfig,
    scenario: &lks_core::scenario::Scenario,
    run_id: &str,
    persona_prompt: String,
    writer: Arc<Mutex<EventWriter>>,
    provider: &str,
) -> Result<(), RunError> {
    use livekit_api::services::sip::{CreateSIPParticipantOptions, SIPClient};

    let tel = &scenario.telephony;
    let trunk = tel.as_ref().and_then(|t| t.sip_trunk_id.clone()).filter(|s| !s.is_empty())
        .or_else(|| cfg.telephony.outbound_trunk_id.clone().filter(|s| !s.is_empty()))
        .ok_or_else(|| RunError("outbound_human_pickup requires telephony.outbound_trunk_id (config) or Telephony.sip_trunk_id (scenario).".into()))?;
    let call_to = tel.as_ref().and_then(|t| t.call_to.clone()).filter(|s| !s.is_empty())
        .ok_or_else(|| RunError("outbound_human_pickup requires Telephony.call_to (human/PSTN number that will answer).".into()))?;

    let agent_room_name = format!("lks-{run_id}");
    let api_host = cfg
        .livekit
        .url
        .replace("wss://", "https://")
        .replace("ws://", "https://");
    let dispatch_id = crate::dispatch::create_dispatch(
        &api_host,
        &cfg.livekit.api_key,
        &cfg.livekit.api_secret,
        &agent_room_name,
        &cfg.livekit.agent_name,
        None,
    )
    .await?;
    let mut w = writer.lock().await;
    w.emit("dispatch.created",
        Some(&serde_json::json!({"room": agent_room_name, "agent_name": cfg.livekit.agent_name, "mode": "outbound_human_pickup", "dispatch_id": dispatch_id}).as_object().cloned().unwrap_or_default()),
        "sim", None, None, false, None);
    drop(w);

    // Dial the human number from the agent-room.
    let sip = SIPClient::with_api_key(&api_host, &cfg.livekit.api_key, &cfg.livekit.api_secret);
    let sip_identity = format!("sip-out-{}", &run_id[..12]);
    let wait = tel
        .as_ref()
        .and_then(|t| t.wait_until_answered)
        .unwrap_or(true);
    let options = CreateSIPParticipantOptions {
        participant_identity: sip_identity.clone(),
        participant_name: Some("Simulated Caller".into()),
        wait_until_answered: Some(wait),
        ..Default::default()
    };
    let sip_info = sip
        .create_sip_participant(
            trunk.clone(),
            call_to.clone(),
            agent_room_name.clone(),
            options,
            None,
        )
        .await
        .map_err(|e| RunError(format!("outbound dial failed: {e}")))?;
    let mut w = writer.lock().await;
    w.emit("outbound.dial_answered",
        Some(&serde_json::json!({"participant_identity": sip_info.participant_identity, "mode": "outbound_human_pickup"}).as_object().cloned().unwrap_or_default()),
        "sim", None, None, false, None);
    drop(w);

    // Gemini speaks in the agent-room as the caller.
    let identity = "lks-caller";
    let provider = provider.trim().to_lowercase();
    if provider == "google" {
        let bridge = crate::callers::GeminiCallerBridge::new(
            cfg.livekit.clone(),
            cfg.simulator.clone(),
            persona_prompt,
            agent_room_name.clone(),
            identity.to_string(),
            writer.clone(),
        );
        bridge
            .run(tokio::sync::broadcast::channel::<()>(1).1)
            .await?;
    } else {
        let bridge = crate::callers::OpenAiCallerBridge::new(
            cfg.livekit.clone(),
            cfg.simulator.clone(),
            persona_prompt,
            "agent".to_string(),
            0,
            agent_room_name.clone(),
            identity.to_string(),
            writer.clone(),
        );
        bridge
            .run(tokio::sync::broadcast::channel::<()>(1).1)
            .await?;
    }
    Ok(())
}
