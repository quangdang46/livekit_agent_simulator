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
