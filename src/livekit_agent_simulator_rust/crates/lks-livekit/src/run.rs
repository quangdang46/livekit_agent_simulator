//! Run orchestration (P2 minimal slice — port of `run_orchestrator.py` core):
//! load config + scenario, create the report dir, connect the caller bridge,
//! run until a timeout, and finalize summary.json + events.jsonl.

use std::path::Path;
use std::sync::Arc;
use tokio::sync::Mutex;

use tokio::sync::broadcast;

use lks_core::config::load_config;
use lks_core::errors::RunError;
use lks_core::logging::event::EventWriter;
use lks_core::scenario_ops::find_scenario;

use crate::callers::OpenAiCallerBridge;

/// Run options for execute_scenario (repeat/pass@k/agent/optimized/profile).
#[derive(Debug, Clone, Default)]
pub struct ExecuteOptions {
    pub run_name: Option<String>,
    pub repeat: i64,
    pub pass_at_k: Option<i64>,
    pub agent_name: Option<String>,
    pub optimized: Option<String>,
    pub profile: Option<String>,
    pub environment: Option<String>,
}

impl ExecuteOptions {
    /// Single-shot options (repeat=1) — the default for CLI/MCP callers.
    pub fn single() -> Self {
        Self {
            repeat: 1,
            ..Default::default()
        }
    }
}

/// Full execute_scenario op — validate then run with repeat/pass@k flake
/// control, agent_name override, optimized artifact, and profile selection
/// (port of `ops.execute_scenario`). Returns the executed result envelope.
pub async fn execute_scenario(
    project_root: &Path,
    scenario_id: &str,
    opts: &ExecuteOptions,
) -> Result<serde_json::Map<String, serde_json::Value>, RunError> {
    let repeat = opts.repeat;
    if repeat < 1 {
        return Err(RunError("repeat must be >= 1".to_string()));
    }
    let k = opts.pass_at_k.unwrap_or(repeat);
    if k > repeat {
        return Err(RunError(format!(
            "pass_at_k ({k}) cannot exceed repeat ({repeat})"
        )));
    }

    // Validation first (matches Python: invalid → executed=false + validation).
    let cfg = load_config(
        project_root.to_path_buf(),
        opts.profile.as_deref(),
        opts.environment.as_deref(),
    )
    .map_err(|e| RunError(e.0))?;
    let scenario = match find_scenario(&cfg.scenarios_dir(), scenario_id) {
        Ok(s) => s,
        Err(e) => {
            // Missing/invalid scenario → executed=false envelope (Python
            // ops.validate_scenario shape), NOT an exception.
            let mut validation = serde_json::Map::new();
            validation.insert("valid".into(), serde_json::Value::Bool(false));
            validation.insert("error".into(), serde_json::Value::String(e.to_string()));
            let mut m = serde_json::Map::new();
            m.insert("executed".into(), serde_json::Value::Bool(false));
            m.insert("validation".into(), serde_json::Value::Object(validation));
            return Ok(m);
        }
    };
    let mut validation = serde_json::Map::new();
    validation.insert("valid".into(), serde_json::Value::Bool(true));
    validation.insert(
        "id".into(),
        serde_json::Value::String(scenario_id.to_string()),
    );

    let mut hard_passes = 0i64;
    let mut iterations: Vec<serde_json::Value> = Vec::new();

    for i in 0..repeat {
        let mut result = match execute_scenario_parsed(project_root, &scenario, opts).await {
            Ok(r) => r,
            Err(e) => {
                let mut m = serde_json::Map::new();
                m.insert("executed".into(), serde_json::Value::Bool(true));
                m.insert("run_id".into(), serde_json::Value::Null);
                m.insert("status".into(), serde_json::Value::String("failed".into()));
                m.insert("error".into(), serde_json::Value::String(e.to_string()));
                m
            }
        };
        result.insert("executed".into(), serde_json::Value::Bool(true));
        result.insert(
            "validation".into(),
            serde_json::Value::Object(validation.clone()),
        );

        // Gemini Live transport drop (1006 abnormal closure) is retryable
        // flakiness — re-run once per iteration (port of ops.py:519-530).
        if is_transport_drop(&result) {
            let retried = match execute_scenario_parsed(project_root, &scenario, opts).await {
                Ok(mut r) => {
                    r.insert("executed".into(), serde_json::Value::Bool(true));
                    r.insert(
                        "validation".into(),
                        serde_json::Value::Object(validation.clone()),
                    );
                    r.insert("retried_from_drop".into(), serde_json::Value::Bool(true));
                    r
                }
                Err(e) => {
                    let mut m = serde_json::Map::new();
                    m.insert("executed".into(), serde_json::Value::Bool(true));
                    m.insert("run_id".into(), serde_json::Value::Null);
                    m.insert("status".into(), serde_json::Value::String("failed".into()));
                    m.insert("error".into(), serde_json::Value::String(e.to_string()));
                    m
                }
            };
            result = retried;
        }

        let gate = lks_core::suite::evaluate_run_result(&result, false);
        let summary = result
            .get("summary")
            .and_then(|v| v.as_object())
            .cloned()
            .unwrap_or_default();
        let mdig =
            lks_core::ops::metrics_digest(summary.get("metrics").and_then(|v| v.as_object()));
        let mut it = serde_json::Map::new();
        it.insert("i".into(), serde_json::Value::Number((i + 1).into()));
        it.insert(
            "run_id".into(),
            result
                .get("run_id")
                .cloned()
                .or_else(|| summary.get("run_id").cloned())
                .unwrap_or(serde_json::Value::Null),
        );
        it.insert(
            "status".into(),
            result
                .get("status")
                .cloned()
                .unwrap_or(serde_json::Value::Null),
        );
        it.insert(
            "gate".into(),
            gate.get("gate").cloned().unwrap_or(serde_json::Value::Null),
        );
        it.insert(
            "ok".into(),
            gate.get("ok")
                .cloned()
                .unwrap_or(serde_json::Value::Bool(false)),
        );
        it.insert(
            "hard_reasons".into(),
            gate.get("hard_reasons")
                .cloned()
                .unwrap_or(serde_json::Value::Array(Vec::new())),
        );
        it.insert(
            "ttfw_ms".into(),
            mdig.get("ttfw_ms")
                .cloned()
                .unwrap_or(serde_json::Value::Null),
        );
        it.insert(
            "turn_p50_ms".into(),
            mdig.get("turn_p50_ms")
                .cloned()
                .unwrap_or(serde_json::Value::Null),
        );
        it.insert(
            "turn_p95_ms".into(),
            mdig.get("turn_p95_ms")
                .cloned()
                .unwrap_or(serde_json::Value::Null),
        );
        iterations.push(serde_json::Value::Object(it));
        if gate.get("ok").and_then(|v| v.as_bool()).unwrap_or(false) {
            hard_passes += 1;
        }
    }

    let ok = hard_passes >= k;
    let mut out = serde_json::Map::new();
    out.insert("executed".into(), serde_json::Value::Bool(true));
    out.insert("validation".into(), serde_json::Value::Object(validation));
    out.insert("repeat".into(), serde_json::Value::Number(repeat.into()));
    out.insert("pass_at_k".into(), serde_json::Value::Number(k.into()));
    out.insert(
        "hard_passes".into(),
        serde_json::Value::Number(hard_passes.into()),
    );
    out.insert("ok".into(), serde_json::Value::Bool(ok));
    out.insert(
        "iterations".into(),
        serde_json::Value::Array(iterations.clone()),
    );
    if let Some(rn) = opts.run_name.as_deref() {
        out.insert("run_name".into(), serde_json::Value::String(rn.to_string()));
    }
    // Back-compat: last-iteration fields + summary reload from a done run.
    if let Some(last) = iterations.last() {
        let last_map = last.as_object().cloned().unwrap_or_default();
        out.insert(
            "run_id".into(),
            last_map
                .get("run_id")
                .cloned()
                .unwrap_or(serde_json::Value::Null),
        );
        out.insert(
            "status".into(),
            last_map
                .get("status")
                .cloned()
                .unwrap_or(serde_json::Value::Null),
        );
        // Re-load summary from the last done iteration's summary.json if possible.
        for it in iterations.iter().rev() {
            let it_map = it.as_object().cloned().unwrap_or_default();
            let rid = it_map.get("run_id").and_then(|v| v.as_str());
            let st = it_map.get("status").and_then(|v| v.as_str());
            if let (Some(rid), Some("done")) = (rid, st) {
                let summary_path = cfg.reports_dir().join(rid).join("summary.json");
                if summary_path.exists() {
                    if let Ok(t) = std::fs::read_to_string(&summary_path) {
                        if let Ok(v) = serde_json::from_str::<serde_json::Value>(&t) {
                            out.insert("summary".into(), v);
                            break;
                        }
                    }
                }
            }
        }
    }
    Ok(out)
}

/// True when a run ended because the Gemini Live socket dropped mid-call
/// (port of `ops._is_transport_drop` — summary end_reason == gemini_socket_drop).
pub fn is_transport_drop(result: &serde_json::Map<String, serde_json::Value>) -> bool {
    result
        .get("summary")
        .and_then(|v| v.as_object())
        .and_then(|s| s.get("end_reason"))
        .and_then(|v| v.as_str())
        == Some("gemini_socket_drop")
}

/// Run one already-validated scenario (by id) against the configured LiveKit
/// agent. Shared by execute_scenario (repeat loop) and execute_scenarios.
pub async fn execute_scenario_parsed(
    project_root: &Path,
    scenario: &lks_core::scenario::Scenario,
    opts: &ExecuteOptions,
) -> Result<serde_json::Map<String, serde_json::Value>, RunError> {
    let mut cfg = load_config(
        project_root.to_path_buf(),
        opts.profile.as_deref(),
        opts.environment.as_deref(),
    )
    .map_err(|e| RunError(e.0))?;
    // agent_name overrides the target worker for this run only (Python
    // run_scenario_instance dataclasses.replace equivalent).
    if let Some(an) = opts.agent_name.as_deref() {
        if !an.is_empty() {
            cfg.livekit.agent_name = an.to_string();
        }
    }
    // Optimized artifact → persona-prompt override (ops._resolve_caller_policy).
    let persona_prompt = if let Some(opt) = opts.optimized.as_deref() {
        let artifact = cfg.optimized_dir().join(opt).join("prompt.yaml");
        if !artifact.is_file() {
            return Err(RunError(format!(
                "No optimized prompt at {} — run `lks optimize --name {opt}` first",
                artifact.display()
            )));
        }
        let text = std::fs::read_to_string(&artifact)
            .map_err(|e| RunError(format!("{}: read error — {e}", artifact.display())))?;
        let variant = lks_core::optimize::parse_variant_yaml(&text)
            .map_err(|e| RunError(format!("optimized prompt {opt}: {e}")))?;
        lks_core::optimize::render_variant_prompt_for_persona(
            &variant,
            &scenario.persona,
            &scenario.effective_locale(),
            &scenario.context,
            &scenario.script_steps,
            &scenario.run_spec().first_speaker,
        )
    } else {
        build_persona_prompt(scenario)
    };

    // --- report dir + run id ---
    let reports_dir = cfg.reports_dir();
    std::fs::create_dir_all(&reports_dir).map_err(|e| RunError(format!("reports dir: {e}")))?;

    // seq from existing report dirs
    let seq = next_seq(&reports_dir);
    let slug = opts.run_name.as_deref().unwrap_or(&scenario.id);
    let now = jiff::Zoned::now();
    let run_id = format!(
        "{:03}-{}-{}-{}",
        seq,
        slug,
        now.strftime("%Y%m%d-%H%M%S"),
        rand_suffix()
    );
    let report_dir = reports_dir.join(&run_id);
    std::fs::create_dir_all(&report_dir).map_err(|e| RunError(format!("report dir: {e}")))?;

    // Persist the run in runs.sqlite so get_run_status/list_runs see it.
    {
        let store =
            lks_core::logging::sqlite::RunStore::new(cfg.sqlite_path().to_string_lossy().as_ref());
        let started_utc = jiff::Zoned::now()
            .strftime("%Y-%m-%dT%H:%M:%SZ")
            .to_string();
        let _ = store.create_run(
            &run_id,
            &scenario.id,
            &room_name_placeholder(),
            &cfg.livekit.agent_name,
            &started_utc,
            &report_dir.to_string_lossy(),
        );
    }

    // --- writer ---
    let mut writer = EventWriter::new(
        &run_id,
        report_dir.clone(),
        &cfg.observe.timezone,
        cfg.observe.turn_taking_warn_ms,
    )
    .map_err(|e| RunError(format!("event writer: {e}")))?;

    let run_spec = scenario.run_spec();
    let config_snapshot = cfg.config_snapshot();
    let mut run_started = serde_json::Map::new();
    run_started.insert(
        "scenario_id".into(),
        serde_json::Value::String(scenario.id.clone()),
    );
    run_started.insert(
        "caller_mode".into(),
        serde_json::Value::String(scenario.effective_caller_mode().to_string()),
    );
    run_started.insert(
        "config_snapshot".into(),
        serde_json::Value::Object(config_snapshot),
    );
    writer.emit(
        "run.started",
        Some(&run_started),
        "sim",
        None,
        None,
        false,
        None,
    );

    // --- end_call channel ---
    let (end_tx, end_rx) = broadcast::channel::<()>(1);

    // Local conversation recorder → conversation.wav (16k stereo).
    let recorder: crate::script::SharedRecorder = Arc::new(std::sync::Mutex::new(
        crate::audio::LocalConversationRecorder::new(),
    ));

    // --- caller bridge (webrtc_sim + openai provider) ---
    let room_name = format!("lks-{run_id}");
    // Sim caller identity — Python adapter.py SIM_IDENTITY. The target agent
    // (e.g. voice-ai-agent's session-event-handlers) matches the caller by
    // identity, so the sim must use the stable Python name ("lks-caller"),
    // not a per-run identity (that would break caller detection/hangup).
    let identity = "lks-caller".to_string();
    let writer_arc = Arc::new(Mutex::new(writer));

    // Shared mic source so room_pcm cues play into the room (set by the bridge).
    let shared_mic: crate::script::SharedMicSource = Arc::new(Mutex::new(None));

    // Observer state shared with the script runtime.
    let script_state = Arc::new(Mutex::new(crate::script::ScriptObserverState::default()));
    let script_writer = writer_arc.clone();
    let script_state2 = script_state.clone();
    let end_tx2 = end_tx.clone();

    // Cue channel: ScriptRuntime → caller bridge (real delivery, port of
    // Python `bridge.inject_cue`). The bridge consumes Speak/Dtmf commands
    // from its own run loop.
    let (cue_tx, cue_rx) = tokio::sync::mpsc::unbounded_channel::<crate::script::CueCommand>();
    // Script runtime: fires the scenario's script steps (time/silence triggers).
    let script_task = if !scenario.script_steps.is_empty() {
        let end_rx_script = end_rx.resubscribe();
        let project_root_owned = project_root.to_path_buf();
        let shared_mic_closure = shared_mic.clone();
        let cue_tx_script = cue_tx.clone();
        let locale = cfg.simulator.language.clone();
        let runtime = crate::script::ScriptRuntime::new(
            scenario.script_steps.clone(),
            script_writer,
            script_state2,
            end_tx2,
            Box::new(move |action| match action {
                crate::script::ScriptAction::HangUp { farewell, label } => {
                    eprintln!("[lksr] script hang_up ({label}): {farewell}");
                    Ok(())
                }
                crate::script::ScriptAction::Speak { text, label, .. } => {
                    eprintln!("[lksr] script speak ({label}): {text}");
                    let _ = cue_tx_script.send(crate::script::CueCommand::Speak { text, label });
                    Ok(())
                }
                crate::script::ScriptAction::Dtmf { digits } => {
                    eprintln!("[lksr] script dtmf: {digits}");
                    let _ = cue_tx_script.send(crate::script::CueCommand::Dtmf { digits });
                    Ok(())
                }
                crate::script::ScriptAction::RoomPcm {
                    asset,
                    gain,
                    r#loop,
                    label,
                } => {
                    // Resolve the WAV: builtin:<name> → templates/cues, else target cues dir.
                    let cues_dir =
                        lks_core::config::load_config(project_root_owned.clone(), None, None)
                            .map(|c| c.cues_dir())
                            .unwrap_or_else(|_| project_root_owned.join(".agent-sim/cues"));
                    let resolved = if let Some(name) = asset.strip_prefix("builtin:") {
                        // Package templates/cues (walk up from the crate).
                        let templates = std::env::current_dir()
                            .unwrap_or_else(|_| std::path::PathBuf::from("."));
                        let mut p = templates;
                        let mut found = None;
                        for _ in 0..6 {
                            let cand = p.join("templates").join("cues").join(format!("{name}.wav"));
                            if cand.exists() {
                                found = Some(cand);
                                break;
                            }
                            if !p.pop() {
                                break;
                            }
                        }
                        found.unwrap_or_else(|| cues_dir.join(format!("{name}.wav")))
                    } else {
                        cues_dir.join(&asset)
                    };
                    match hound::WavReader::open(&resolved) {
                        Ok(mut reader) => {
                            let spec = reader.spec();
                            let samples: Vec<i16> =
                                reader.samples::<i16>().filter_map(Result::ok).collect();
                            let rloop = r#loop;
                            eprintln!(
                                "[lksr] room_pcm ({label}): {} — {} samples @ {}Hz, loop={rloop}, gain={gain}",
                                resolved.display(),
                                samples.len(),
                                spec.sample_rate
                            );
                            // Actually play the cue into the room (24 kHz mono).
                            let samples = if r#loop {
                                // Loop the samples to fill ~5s of playback.
                                let target = spec.sample_rate as usize * 5;
                                let mut out = Vec::with_capacity(target);
                                while out.len() < target {
                                    out.extend_from_slice(&samples);
                                }
                                out
                            } else {
                                samples
                            };
                            let mic = shared_mic_closure.clone();
                            tokio::spawn(async move {
                                if let Err(e) = crate::script::play_pcm_to_source(
                                    &mic,
                                    &samples,
                                    spec.sample_rate,
                                )
                                .await
                                {
                                    eprintln!("[lksr] room_pcm playback: {e}");
                                }
                            });
                            Ok(())
                        }
                        Err(e) => Err(format!(
                            "room_pcm asset not found/readable: {} ({e})",
                            resolved.display()
                        )),
                    }
                }
                _ => Ok(()),
            }),
            locale,
        );
        Some(tokio::spawn(
            async move { runtime.run(end_rx_script).await },
        ))
    } else {
        None
    };

    // SIP legs: dispatch by caller_mode.
    let mode = scenario.effective_caller_mode().to_string();
    let sip_leg_result: Option<Result<(), RunError>> = match mode.as_str() {
        "inbound_sip" => Some(
            crate::sim_leg::run_inbound_sip(
                &cfg,
                scenario,
                &run_id,
                persona_prompt.clone(),
                writer_arc.clone(),
                &cfg.simulator.provider,
            )
            .await,
        ),
        "outbound_sim_callee" => Some(
            crate::sim_leg::run_outbound_sim_callee(
                &cfg,
                scenario,
                &run_id,
                persona_prompt.clone(),
                writer_arc.clone(),
                &cfg.simulator.provider,
            )
            .await,
        ),
        "agent_dials" => Some(
            crate::sim_leg::run_agent_dials(
                &cfg,
                scenario,
                &run_id,
                persona_prompt.clone(),
                writer_arc.clone(),
                &cfg.simulator.provider,
            )
            .await,
        ),
        "outbound_human_pickup" => Some(
            crate::sim_leg::run_outbound_human_pickup(
                &cfg,
                scenario,
                &run_id,
                persona_prompt.clone(),
                writer_arc.clone(),
                &cfg.simulator.provider,
            )
            .await,
        ),
        _ => None,
    };
    if let Some(leg) = sip_leg_result {
        if let Some(t) = script_task {
            t.abort();
        }
        return match leg {
            Ok(()) => {
                let mut w = writer_arc.lock().await;
                let mut meta = serde_json::Map::new();
                meta.insert("run_id".into(), serde_json::Value::String(run_id.clone()));
                meta.insert(
                    "scenario_id".into(),
                    serde_json::Value::String(scenario.id.clone()),
                );
                let summary = w.finalize("done", Some(&meta), None);
                let mut out = serde_json::Map::new();
                out.insert("run_id".into(), serde_json::Value::String(run_id));
                out.insert("status".into(), serde_json::Value::String("done".into()));
                out.insert(
                    "report_dir".into(),
                    serde_json::Value::String(report_dir.to_string_lossy().into_owned()),
                );
                out.insert("summary".into(), serde_json::Value::Object(summary));
                Ok(out)
            }
            Err(e) => Err(e),
        };
    }

    // Caller nudge receiver (created before end_rx moves into the bridge).
    let nudge_rx = end_rx.resubscribe();
    let rate_end_rx = end_rx.resubscribe();

    // Provider dispatch: config `simulator.provider` selects the caller bridge.
    let provider = cfg.simulator.provider.trim().to_lowercase();
    let bridge_future: std::pin::Pin<
        Box<dyn std::future::Future<Output = Result<(), RunError>> + Send>,
    > = {
        let dispatch_meta = scenario
            .dispatch_metadata(cfg.livekit.dispatch_metadata.as_deref())
            .map(|s| s.to_string());
        let silent = lks_core::behavior_compile::silent_mode_enabled(&scenario.persona);
        let persona_sc =
            lks_core::behavior_compile::speech_conditions_of(&scenario.persona).clone();
        if provider == "google" {
            let bridge = crate::callers::GeminiCallerBridge::new(
                cfg.livekit.clone(),
                cfg.simulator.clone(),
                persona_prompt.clone(),
                room_name,
                identity,
                writer_arc.clone(),
            )
            .with_dispatch_metadata(dispatch_meta)
            .with_silent_mode(silent)
            .with_observe(cfg.observe.clone())
            .with_speech_conditions(persona_sc.clone());
            Box::pin(async move { bridge.run(end_rx.resubscribe()).await })
        } else {
            let bridge = OpenAiCallerBridge::new(
                cfg.livekit.clone(),
                cfg.simulator.clone(),
                persona_prompt.clone(),
                run_spec.first_speaker.clone(),
                run_spec.max_turns,
                room_name,
                identity,
                writer_arc.clone(),
            )
            .with_shared_mic(shared_mic.clone())
            .with_recorder(recorder.clone())
            .with_dispatch_metadata(dispatch_meta)
            .with_silent_mode(silent)
            .with_observe(cfg.observe.clone())
            .with_speech_conditions(persona_sc)
            .with_cue_rx(cue_rx);
            Box::pin(async move { bridge.run(end_rx).await })
        }
    };

    // Caller nudge: first_speaker=agent + no script → nudge after greeting.
    let nudge_task = if run_spec.first_speaker == "agent" && scenario.script_steps.is_empty() {
        let w = writer_arc.clone();
        let rx = nudge_rx;
        // Silent mode gate: nudge is off for silent callers (slice: first_speaker only).
        let nudge = crate::caller_nudge::nudge_caller_after_agent_greeting(
            w,
            rx,
            |_hint| Ok(()),
            || true,  // agent_has_spoken — poll-based; set via shared state later
            || false, // user_has_spoken
            "agent",
            false,
            1.0,
            0.15,
        );
        Some(tokio::spawn(nudge))
    } else {
        None
    };
    // Hold-music-timeout watchdog (P2.J port): agent dead air >=
    // Execute.spec.hold_music_timeout_s after the agent spoke once → sim
    // hangs up with end_reason hold_music_timeout (run_orchestrator.py:757-806).
    let (hold_tx, _hold_rx) = tokio::sync::mpsc::channel::<()>(1);
    let hold_task = scenario.execute.as_ref().and_then(|ex| ex.hold_music_timeout_s).map(
        |timeout_s| {
            let w = writer_arc.clone();
            let end = end_tx.clone();
            tokio::spawn(async move {
                let timeout = std::time::Duration::from_secs_f64(timeout_s);
                loop {
                    tokio::time::sleep(std::time::Duration::from_millis(250)).await;
                    if !crate::callers::openai::AGENT_HAS_SPOKEN
                        .load(std::sync::atomic::Ordering::SeqCst)
                    {
                        continue;
                    }
                    let last_ms = crate::callers::openai::LAST_AGENT_ACTIVITY_MS
                        .load(std::sync::atomic::Ordering::SeqCst);
                    if last_ms == 0 {
                        continue;
                    }
                    let now_ms = std::time::SystemTime::now()
                        .duration_since(std::time::UNIX_EPOCH)
                        .map(|d| d.as_millis() as i64)
                        .unwrap_or(0);
                    if now_ms - last_ms >= timeout.as_millis() as i64 {
                        {
                            let mut w = w.lock().await;
                            w.emit(
                                "sim.hold_timeout",
                                Some(&serde_json::json!({
                                    "timeout_s": timeout_s,
                                    "agent_idle_ms": now_ms - last_ms,
                                    "note": "Caller gave up waiting on agent dead air (hold_music_timeout_s)",
                                })
                                .as_object()
                                .cloned()
                                .unwrap_or_default()),
                                "sim",
                                None,
                                None,
                                false,
                                None,
                            );
                        }
                        let _ = hold_tx.send(()).await;
                        let _ = end.send(());
                        return;
                    }
                }
            })
        },
    );

    // Interruption-rate runner (P1.K port of InterruptRateRunner): recurring
    // barges while the agent is the active speaker. Emits the same
    // sim.script.cue / interruption events as Script barges so verify counts
    // them identically, and sends a real Speak cue through the cue channel.
    let rate_task = match lks_core::interrupt_rate::parse_interrupt_rate(&scenario.persona) {
        Ok(Some(spec)) if !lks_core::behavior_compile::silent_mode_enabled(&scenario.persona) => {
            let w = writer_arc.clone();
            let end = rate_end_rx;
            let cue = cue_tx.clone();
            Some(tokio::spawn(async move {
                w.lock().await.emit(
                    "sim.interrupt_rate",
                    Some(
                        &serde_json::json!({
                            "rate": spec.rate,
                            "interval_ms": spec.interval_ms,
                            "class": spec.interrupt_class,
                            "say": spec.say,
                            "min_agent_active_ms": spec.min_agent_active_ms,
                        })
                        .as_object()
                        .cloned()
                        .unwrap_or_default(),
                    ),
                    "sim.interrupt_rate",
                    None,
                    None,
                    false,
                    None,
                );
                let mut fired: u32 = 0;
                let mut last_fire: Option<std::time::Instant> = None;
                let mut armed: Option<std::time::Instant> = None;
                let mut was_active = false;
                let mut end = std::pin::pin!(end);
                loop {
                    tokio::select! {
                        _ = end.recv() => return,
                        _ = tokio::time::sleep(std::time::Duration::from_millis(50)) => {}
                    }
                    let active = crate::callers::openai::AGENT_ACTIVE_SPEAKER
                        .load(std::sync::atomic::Ordering::SeqCst);
                    if !active {
                        if was_active {
                            // Emit interrupt_rate_skip once when agent goes inactive
                            let skip_spec = serde_json::json!({
                                "reason": "agent_not_active",
                                "active_ms": 0,
                            });
                            w.lock().await.emit(
                                "sim.interrupt_rate_skip",
                                Some(skip_spec.as_object().unwrap()),
                                "sim.interrupt_rate",
                                None,
                                None,
                                false,
                                None,
                            );
                        }
                        armed = None;
                        was_active = false;
                        continue;
                    }
                    if !was_active {
                        armed = Some(std::time::Instant::now());
                    }
                    was_active = true;
                    let anchor = last_fire.or(armed);
                    let Some(anchor) = anchor else { continue };
                    let active_ms = anchor.elapsed().as_millis() as i64;
                    if active_ms < spec.min_agent_active_ms {
                        continue; // skip silently — not enough active time
                    }
                    if anchor.elapsed() < std::time::Duration::from_millis(spec.interval_ms as u64)
                    {
                        continue;
                    }
                    fired += 1;
                    last_fire = Some(std::time::Instant::now());
                    let step_id = format!("rate-barge-{fired}");
                    {
                        let mut w = w.lock().await;
                        let mut ispec = serde_json::Map::new();
                        ispec.insert("by".into(), serde_json::json!("sim"));
                        ispec.insert("barge_in".into(), serde_json::json!(true));
                        ispec.insert("class".into(), serde_json::json!(spec.interrupt_class));
                        ispec.insert("step_id".into(), serde_json::json!(step_id));
                        ispec.insert("label".into(), serde_json::json!(step_id));
                        ispec.insert(
                            "note".into(),
                            serde_json::json!("InterruptRateRunner barge (typed)."),
                        );
                        w.emit(
                            "interruption",
                            Some(&ispec),
                            "sim.interrupt_rate",
                            None,
                            None,
                            false,
                            None,
                        );
                        w.emit(
                            "sim.script.cue",
                            Some(
                                &serde_json::json!({
                                    "step_id": step_id,
                                    "label": step_id,
                                    "say": spec.say,
                                    "trigger": "agent_speaking",
                                    "action": "speak",
                                    "barge_in": true,
                                    "class": spec.interrupt_class,
                                })
                                .as_object()
                                .cloned()
                                .unwrap_or_default(),
                            ),
                            "sim.script",
                            None,
                            None,
                            false,
                            None,
                        );
                    }
                    let _ = cue.send(crate::script::CueCommand::Speak {
                        text: spec.say.clone(),
                        label: step_id,
                    });
                }
            }))
        }
        _ => None,
    };

    // The slice ends on the bridge's internal cap (agent hangup later).
    let run_result = bridge_future.await;
    if let Some(t) = script_task {
        t.abort();
    }
    if let Some(t) = nudge_task {
        t.abort();
    }
    if let Some(t) = hold_task {
        t.abort();
    }
    if let Some(t) = rate_task {
        t.abort();
    }

    // --- finalize ---
    let mut status = match &run_result {
        Ok(()) => "done",
        Err(e) => {
            eprintln!("[lksr] run error: {e}");
            "failed"
        }
    };

    let mut w = writer_arc.lock().await;
    let duration_ms = w.t0_mono().elapsed().as_millis() as i64;
    // Port of run_orchestrator.py:455-461 — a run failure emits run.error
    // (spec {error, mode}) BEFORE finalize so the report carries the cause.
    if let Err(e) = &run_result {
        let mut err_spec = serde_json::Map::new();
        err_spec.insert("error".into(), serde_json::Value::String(e.to_string()));
        err_spec.insert(
            "mode".into(),
            serde_json::Value::String(scenario.effective_caller_mode().to_string()),
        );
        w.emit("run.error", Some(&err_spec), "mcp", None, None, false, None);
    }
    // run.end_condition (port of run_orchestrator.py:448) — emitted before
    // finalize with the end reason. The Rust bridge ends on the 45s slice cap
    // when the agent doesn't disconnect (Python's end_reason is "timeout" for
    // the scenario timeout; the 45s cap is the port's hard bound).
    // Classify the end reason. Distinguish the caller's own hang-up (the
    // bridge emitted sim.end_call_token / max_turns reached) from a timeout
    // or an agent disconnect — parity with run_orchestrator.py end reasons
    // (sim_end_call / max_turns / timeout / agent_disconnected).
    let ended_by_caller = w
        .events()
        .iter()
        .any(|e| e.get("kind").and_then(|v| v.as_str()) == Some("sim.end_call_token"));
    let max_turns = run_spec.max_turns;
    let turn_count = w
        .events()
        .iter()
        .filter_map(|e| e.get("turn").and_then(|v| v.as_i64()))
        .max()
        .unwrap_or(0);
    let reached_max_turns = max_turns > 0 && turn_count >= max_turns;
    let end_reason = match &run_result {
        Ok(()) => {
            if ended_by_caller {
                "sim_end_call"
            } else if reached_max_turns {
                "max_turns"
            } else {
                // The bridge returned after the cap or agent disconnect — use
                // the run duration vs the scenario timeout to classify. The
                // 45s slice cap is the port's hard bound; when the run lasted
                // ≥45s and the agent never left, the scenario timeout is the
                // honest cause (parity with Python's end_reason "timeout").
                if status == "done" && duration_ms >= 45_000 {
                    "timeout"
                } else if status == "done" {
                    "agent_disconnected"
                } else {
                    "error"
                }
            }
        }
        Err(e) => {
            if e.to_string().contains("Agent `") && e.to_string().contains("did not join") {
                "agent_join_timeout"
            } else {
                "error"
            }
        }
    };
    {
        let mut ec = serde_json::Map::new();
        ec.insert(
            "reason".into(),
            serde_json::Value::String(end_reason.into()),
        );
        w.emit(
            "run.end_condition",
            Some(&ec),
            "mcp",
            None,
            None,
            false,
            None,
        );
    }
    // finalize() emits run.ended (source mcp) + writes summary.json (full 36-key
    // metrics) / meta.json / timeline.md, and returns the summary map.
    let mut meta = serde_json::Map::new();
    meta.insert("run_id".into(), serde_json::Value::String(run_id.clone()));
    meta.insert(
        "scenario_id".into(),
        serde_json::Value::String(scenario.id.clone()),
    );

    // ── Post-run hard verify (port of run_orchestrator.py:525-572) ────────
    // script.verify + Assert evaluation + caller behavior digest, merged into
    // the summary before it is written back. An assert failure flips a done
    // run to failed (hard gates beat the LLM judge).
    let events_snapshot = w.events().clone();
    let mut summary_extra = serde_json::Map::new();
    let has_script_verify = scenario.script_verify.is_some()
        && (!scenario.script_steps.is_empty()
            || scenario
                .script_verify
                .as_ref()
                .and_then(|v| v.get("plugins"))
                .and_then(|v| v.as_array())
                .is_some_and(|a| !a.is_empty()));
    if status == "done" && has_script_verify {
        let typed_verify = scenario
            .script_verify
            .as_ref()
            .and_then(|v| lks_core::script::parse::parse_script_verify(v).ok())
            .flatten();
        let typed_steps: Vec<lks_core::script::ScriptStep> = {
            let mut spec = serde_json::Map::new();
            spec.insert(
                "steps".into(),
                serde_json::Value::Array(scenario.script_steps.clone()),
            );
            lks_core::script::parse::parse_script_steps(&spec, "script").unwrap_or_default()
        };
        let mut script_verify = lks_core::script::verify::evaluate_script_log(
            &events_snapshot,
            &typed_steps,
            typed_verify.as_ref(),
        );
        // Verify plugins (P8): execute registered .py hooks when this build
        // embeds CPython; without the feature, record a loud skip per plugin.
        if let Some(verify) = &typed_verify {
            for plugin_name in &verify.plugins {
                #[cfg(feature = "python-plugins")]
                {
                    let ctx = lks_core::plugin_bridge::VerifyPluginContext {
                        events: events_snapshot
                            .iter()
                            .map(|e| serde_json::Value::Object(e.clone()))
                            .collect(),
                        scenario_id: scenario.id.clone(),
                        plugin_name: plugin_name.clone(),
                        project_root: project_root.to_path_buf(),
                    };
                    let result =
                        lks_core::plugin_bridge::run_verify_plugin(project_root, plugin_name, &ctx);
                    let check = match result {
                        Some(r) => serde_json::json!({
                            "check": format!("plugin:{plugin_name}"),
                            "pass": r.pass,
                            "plugin": plugin_name,
                            "checks": r.checks,
                        }),
                        None => serde_json::json!({
                            "check": format!("plugin:{plugin_name}"),
                            "pass": false,
                            "reason": format!("verify plugin {plugin_name:?} is not registered"),
                        }),
                    };
                    if let Some(checks) = script_verify
                        .get_mut("checks")
                        .and_then(|v| v.as_array_mut())
                    {
                        checks.push(check);
                    }
                    let all_pass = script_verify
                        .get("checks")
                        .and_then(|v| v.as_array())
                        .map(|arr| {
                            arr.iter()
                                .all(|c| c.get("pass").and_then(|v| v.as_bool()).unwrap_or(false))
                        })
                        .unwrap_or(false);
                    script_verify.insert("pass".into(), serde_json::json!(all_pass));
                }
                #[cfg(not(feature = "python-plugins"))]
                {
                    if let Some(checks) = script_verify
                        .get_mut("checks")
                        .and_then(|v| v.as_array_mut())
                    {
                        checks.push(serde_json::json!({
                            "check": format!("plugin:{plugin_name}"),
                            "pass": false,
                            "reason": format!(
                                "verify plugin {plugin_name:?} requires a lksr build with --features lks-core/python-plugins"
                            ),
                        }));
                    }
                    script_verify.insert("pass".into(), serde_json::json!(false));
                }
            }
        }
        let mut w = writer_arc.lock().await;
        w.emit(
            "script.verify",
            Some(&script_verify),
            "sim.script",
            None,
            None,
            false,
            None,
        );
        summary_extra.insert(
            "script_verify".into(),
            serde_json::Value::Object(script_verify),
        );
    }
    // llm_bool outcome prompts (extra judge criteria) + goals_met outcomes
    // (resolved via judge_goals after the soft judge) — mirror Python, which
    // reads scenario.asserts regardless of run status (run_orchestrator.py:583,
    // 616-624).
    let (llm_criteria, goals_outcomes) = {
        let mut llm: Vec<String> = Vec::new();
        let mut goals: Vec<(String, i64, Vec<String>)> = Vec::new();
        if let Some(am) = scenario.asserts.as_ref().and_then(|v| v.as_object()) {
            if let Ok(spec) = lks_core::asserts::parse_assert_spec(am, "Assert") {
                for oc in &spec.outcomes {
                    if oc.otype == "llm_bool" {
                        if let Some(p) = &oc.prompt {
                            llm.push(format!("[outcome:{}] {p}", oc.id));
                        }
                    } else if oc.otype == "goals_met" {
                        goals.push((oc.id.clone(), oc.min_goals, oc.goals.clone()));
                    }
                }
            }
        }
        (llm, goals)
    };
    if status == "done" {
        if let Some(asserts_map) = scenario.asserts.as_ref().and_then(|v| v.as_object()) {
            match lks_core::asserts::parse_assert_spec(asserts_map, "Assert") {
                Ok(assert_spec) if !assert_spec.empty() => {
                    let assert_result =
                        lks_core::asserts::evaluate_asserts(&events_snapshot, &assert_spec);
                    {
                        let mut w = writer_arc.lock().await;
                        w.emit(
                            "assert.verify",
                            Some(&assert_result),
                            "mcp",
                            None,
                            None,
                            false,
                            None,
                        );
                    }
                    let passed = assert_result
                        .get("pass")
                        .and_then(|v| v.as_bool())
                        .unwrap_or(false);
                    summary_extra.insert(
                        "assert_verify".into(),
                        serde_json::Value::Object(assert_result),
                    );
                    if !passed {
                        if status == "done" {
                            status = "failed";
                        }
                        meta.insert("assert_failed".into(), serde_json::Value::Bool(true));
                    }
                }
                _ => {}
            }
        }
    }
    // Caller behavior digest for reports / web (port of run_orchestrator.py:573-576).
    if status == "done" || status == "failed" {
        let mut behavior_summary =
            lks_core::script::summary::build_caller_behavior_summary(&events_snapshot);
        if let Some(av) = summary_extra
            .get("assert_verify")
            .and_then(|v| v.as_object())
        {
            for chk in av
                .get("checks")
                .and_then(|v| v.as_array())
                .into_iter()
                .flatten()
            {
                if chk.get("type").and_then(|v| v.as_str()) == Some("recovery") {
                    if let Some(ms) = chk.get("recovery_ms").and_then(|v| v.as_i64()) {
                        behavior_summary.insert("recovery_ms".into(), serde_json::json!(ms));
                        behavior_summary.insert(
                            "recovery_assert_pass".into(),
                            serde_json::json!(chk
                                .get("pass")
                                .and_then(|v| v.as_bool())
                                .unwrap_or(false)),
                        );
                    }
                    break;
                }
            }
        }
        let mut caller = serde_json::Map::new();
        caller.insert(
            "behavior_summary".into(),
            serde_json::Value::Object(behavior_summary),
        );
        summary_extra.insert("caller".into(), serde_json::Value::Object(caller));
    }

    let mut summary = w.finalize(status, Some(&meta), None);
    // end_reason into summary (port of run_orchestrator.py:692-693).
    if !end_reason.is_empty() {
        summary.insert(
            "end_reason".into(),
            serde_json::Value::String(end_reason.to_string()),
        );
    }

    // LLM judge over pass_criteria (P7) — HTTP backend (judge.base_url + key).
    // Soft judge only (Python parity): no judge config → no judge at all.
    if cfg.judge.is_some() && !scenario.pass_criteria.is_empty() {
        let turns: Vec<serde_json::Map<String, serde_json::Value>> = w
            .events()
            .iter()
            .filter_map(|e| e.get("kind").and_then(|k| k.as_str()).map(|k| (k, e)))
            .filter(|(k, _)| *k == "transcript.user.final" || *k == "transcript.agent.final")
            .map(|(_, e)| e.clone())
            .collect();
        let tool_events: Vec<serde_json::Map<String, serde_json::Value>> = w
            .events()
            .iter()
            .filter(|e| e.get("kind").and_then(|k| k.as_str()) == Some("tool.start"))
            .cloned()
            .collect();
        // Flow-lifecycle payloads published on observe.flow_topics (port of
        // run_orchestrator._collect_flow_events — opaque, never interpreted).
        let flow_events: Vec<serde_json::Map<String, serde_json::Value>> = w
            .events()
            .iter()
            .filter(|e| {
                e.get("kind").and_then(|k| k.as_str()) == Some("data.message")
                    && cfg
                        .observe
                        .flow_topics
                        .iter()
                        .any(|t| e.get("source").and_then(|s| s.as_str()) == Some(t.as_str()))
            })
            .filter(|e| {
                e.get("spec")
                    .and_then(|s| s.as_object())
                    .and_then(|s| s.get("payload"))
                    .and_then(|p| p.as_object())
                    .map(|o| !o.is_empty())
                    .unwrap_or(false)
            })
            .cloned()
            .collect();
        // Include llm_bool outcome prompts as extra criteria when present
        // (run_orchestrator.py:583-587).
        let mut criteria: Vec<String> = scenario.pass_criteria.clone();
        criteria.extend(llm_criteria.clone());
        let verdict = if scenario.pass_judges.is_empty() {
            lks_core::judge::judge_run(
                cfg.judge.as_ref(),
                &cfg.simulator.api_key,
                &criteria,
                &turns,
                &tool_events,
                &flow_events,
            )
            .await
        } else {
            lks_core::judge::judge_run_multi(
                cfg.judge.as_ref(),
                &cfg.simulator.api_key,
                &criteria,
                &turns,
                &tool_events,
                &flow_events,
                &scenario.pass_judges,
                &scenario.pass_criteria_mode,
            )
            .await
        };
        {
            let mut w = writer_arc.lock().await;
            w.emit("judge.verdict", Some(&verdict), "mcp", None, None, false, None);
        }
        summary.insert("verdict".into(), serde_json::Value::Object(verdict));
    }

    // Post-run goals_met (port of run_orchestrator.py:616-685): hard fail only
    // on an explicit LLM fail; judge unavailable → soft-skip.
    if cfg.judge.is_some() {
        let persona_goals: Vec<String> = scenario
            .persona
            .get("goals")
            .and_then(|v| v.as_array())
            .map(|a| a.iter().filter_map(|g| g.as_str().map(String::from)).collect())
            .unwrap_or_default();
        for (oc_id, min_goals, oc_goals) in &goals_outcomes {
            let goal_list: Vec<String> = if oc_goals.is_empty() {
                persona_goals.clone()
            } else {
                oc_goals.clone()
            };
            if goal_list.is_empty() {
                continue;
            }
            let goals_result = lks_core::judge::judge_goals(
                cfg.judge.as_ref(),
                &cfg.simulator.api_key,
                &goal_list,
                *min_goals,
                &w.turn_metrics(),
                &[],
            )
            .await;
            let gv = goals_result
                .get("verdict")
                .and_then(|v| v.as_str())
                .unwrap_or("fail")
                .to_lowercase();
            let notes = goals_result
                .get("notes")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            if gv == "skipped" || gv == "error" {
                let spec = serde_json::json!({
                    "outcome_id": oc_id,
                    "min_goals": min_goals,
                    "goals": goal_list,
                    "verdict": gv,
                    "pass": true,
                    "skipped": true,
                    "notes": if notes.is_empty() { "goals_met soft-skipped (judge unavailable).".to_string() } else { notes },
                });
                let mut w2 = writer_arc.lock().await;
                w2.emit("assert.goals_met", Some(spec.as_object().unwrap()), "mcp", None, None, false, None);
                continue;
            }
            let gs = goals_result
                .get("score")
                .and_then(|v| v.as_i64())
                .unwrap_or(0);
            let goals_pass = gv == "pass" && gs >= 50;
            let spec = serde_json::json!({
                "outcome_id": oc_id,
                "min_goals": min_goals,
                "goals": goal_list,
                "verdict": gv,
                "score": gs,
                "pass": goals_pass,
                "notes": notes,
            });
            {
                let mut w2 = writer_arc.lock().await;
                w2.emit("assert.goals_met", Some(spec.as_object().unwrap()), "mcp", None, None, false, None);
            }
            if !goals_pass {
                if status == "done" {
                    status = "failed";
                    summary.insert("status".into(), serde_json::Value::String("failed".into()));
                }
                meta.entry("goals_failed".to_string())
                    .and_modify(|v| {
                        if let Some(arr) = v.as_array_mut() {
                            arr.push(serde_json::Value::String(oc_id.clone()));
                        }
                    })
                    .or_insert_with(|| serde_json::json!([oc_id]));
            }
        }
        // finalize already wrote meta.json before the goals flip — persist the
        // updated meta (goals_failed) so artifacts match the final status.
        if meta.get("goals_failed").is_some() {
            let _ = std::fs::write(
                report_dir.join("meta.json"),
                serde_json::to_string_pretty(&meta).unwrap_or_default(),
            );
        }
    }
    // Merge verify/assert/caller extras + rewrite summary.json (port of
    // run_orchestrator.py:696-705 — extras always persisted).
    if !summary_extra.is_empty() {
        for (k, v) in summary_extra {
            summary.insert(k, v);
        }
    }
    let _ = std::fs::write(
        report_dir.join("summary.json"),
        serde_json::to_string_pretty(&summary).unwrap_or_default(),
    );

    // Save conversation.wav (agent audio captured during the run).
    let audio_record_result = recorder.lock().ok().map(|mut rec| {
        let res = rec.save(&report_dir.join("conversation.wav"));
        let t0_ms = rec.started_mono().map(|s| s.elapsed().as_millis() as i64).unwrap_or(0);
        (res, t0_ms)
    });
    if let Some((res, t0_ms)) = audio_record_result {
        match res {
            Ok(result) => {
                let spec = serde_json::json!({
                    "path": result.path,
                    "sample_rate": result.sample_rate,
                    "duration_ms": result.duration_ms,
                    "channels": {"left": "sim", "right": "agent"},
                    "sim_samples": result.sim_samples,
                    "agent_samples": result.agent_samples,
                    "t0_mono_ms": t0_ms,
                });
                {
                    let mut w2 = writer_arc.lock().await;
                    w2.emit("sim.audio_recorded", Some(spec.as_object().unwrap()), "sim", None, None, false, None);
                }
            }
            Err(e) => {
                let spec = serde_json::json!({"where": "audio_finalize", "error": e});
                {
                    let mut w2 = writer_arc.lock().await;
                    w2.emit("sim.error", Some(spec.as_object().unwrap()), "sim", None, None, false, None);
                }
            }
        }
    }

    // Finish the sqlite row + persist full events/turns (port of
    // run_orchestrator.py:707-709 — Python stores every envelope so
    // cross-implementation DB reads stay parity, invariant I2).
    {
        let store =
            lks_core::logging::sqlite::RunStore::new(cfg.sqlite_path().to_string_lossy().as_ref());
        let turns = w.turn_metrics();
        let _ = store.insert_events(&run_id, &events_snapshot);
        let _ = store.insert_turns(&run_id, &turns);
        let ended_utc = jiff::Zoned::now()
            .strftime("%Y-%m-%dT%H:%M:%SZ")
            .to_string();
        let _ = store.finish_run(&run_id, status, &summary, &ended_utc);
    }

    let mut out = serde_json::Map::new();
    out.insert("run_id".into(), serde_json::Value::String(run_id));
    out.insert(
        "status".into(),
        serde_json::Value::String(status.to_string()),
    );
    out.insert(
        "report_dir".into(),
        serde_json::Value::String(report_dir.to_string_lossy().into_owned()),
    );
    out.insert("summary".into(), serde_json::Value::Object(summary));
    Ok(out)
}

fn room_name_placeholder() -> String {
    "lks-rust-run".to_string()
}

fn next_seq(reports_dir: &Path) -> u32 {
    let mut max = 0u32;
    if let Ok(rd) = std::fs::read_dir(reports_dir) {
        for entry in rd.flatten() {
            let name = entry.file_name().to_string_lossy().into_owned();
            let prefix: String = name.chars().take_while(|c| c.is_ascii_digit()).collect();
            if let Ok(n) = prefix.parse::<u32>() {
                max = max.max(n);
            }
        }
    }
    max + 1
}

fn rand_suffix() -> String {
    format!("{:04x}", rand::random::<u16>())
}

pub fn build_persona_prompt(scenario: &lks_core::scenario::Scenario) -> String {
    let brief = scenario
        .persona
        .get("brief")
        .and_then(|v| v.as_str())
        .unwrap_or("");
    let goals: Vec<String> = scenario
        .persona
        .get("goals")
        .and_then(|v| v.as_array())
        .map(|a| {
            a.iter()
                .filter_map(|g| g.as_str().map(String::from))
                .collect()
        })
        .unwrap_or_default();
    let mut p = format!("You are a simulated caller. {brief}");
    if !goals.is_empty() {
        p.push_str("\nCaller goals:");
        for g in &goals {
            p.push_str(&format!("\n- {g}"));
        }
    }
    p.push_str("\nStay in character as the caller. Do not act like an assistant.");
    p
}
