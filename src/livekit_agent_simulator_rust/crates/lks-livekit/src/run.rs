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
    let cfg = load_config(project_root.to_path_buf(), opts.profile.as_deref())
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
    let mut cfg = load_config(project_root.to_path_buf(), opts.profile.as_deref())
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
    let identity = format!("lks-caller-{}", &run_id[..8]);
    let writer_arc = Arc::new(Mutex::new(writer));

    // Shared mic source so room_pcm cues play into the room (set by the bridge).
    let shared_mic: crate::script::SharedMicSource = Arc::new(Mutex::new(None));

    // Observer state shared with the script runtime.
    let script_state = Arc::new(Mutex::new(crate::script::ScriptObserverState::default()));
    let script_writer = writer_arc.clone();
    let script_state2 = script_state.clone();
    let end_tx2 = end_tx.clone();

    // Script runtime: fires the scenario's script steps (time/silence triggers).
    let script_task = if !scenario.script_steps.is_empty() {
        let end_rx_script = end_rx.resubscribe();
        let project_root_owned = project_root.to_path_buf();
        let shared_mic_closure = shared_mic.clone();
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
                    Ok(())
                }
                crate::script::ScriptAction::RoomPcm {
                    asset,
                    gain,
                    r#loop,
                    label,
                } => {
                    // Resolve the WAV: builtin:<name> → templates/cues, else target cues dir.
                    let cues_dir = lks_core::config::load_config(project_root_owned.clone(), None)
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

    // Provider dispatch: config `simulator.provider` selects the caller bridge.
    let provider = cfg.simulator.provider.trim().to_lowercase();
    let bridge_future: std::pin::Pin<
        Box<dyn std::future::Future<Output = Result<(), RunError>> + Send>,
    > = if provider == "google" {
        let bridge = crate::callers::GeminiCallerBridge::new(
            cfg.livekit.clone(),
            cfg.simulator.clone(),
            persona_prompt.clone(),
            room_name,
            identity,
            writer_arc.clone(),
        );
        Box::pin(async move { bridge.run(end_rx.resubscribe()).await })
    } else {
        let bridge = OpenAiCallerBridge::new(
            cfg.livekit.clone(),
            cfg.simulator.clone(),
            persona_prompt.clone(),
            run_spec.first_speaker.clone(),
            room_name,
            identity,
            writer_arc.clone(),
        )
        .with_shared_mic(shared_mic.clone())
        .with_recorder(recorder.clone());
        Box::pin(async move { bridge.run(end_rx).await })
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

    // The slice ends on the bridge's internal cap (agent hangup later).
    let run_result = bridge_future.await;
    if let Some(t) = script_task {
        t.abort();
    }
    if let Some(t) = nudge_task {
        t.abort();
    }

    // --- finalize ---
    let status = match &run_result {
        Ok(()) => "done",
        Err(e) => {
            eprintln!("[lksr] run error: {e}");
            "failed"
        }
    };

    let mut w = writer_arc.lock().await;
    // finalize() emits run.ended (source mcp) + writes summary.json (full 36-key
    // metrics) / meta.json / timeline.md, and returns the summary map.
    let mut meta = serde_json::Map::new();
    meta.insert("run_id".into(), serde_json::Value::String(run_id.clone()));
    meta.insert(
        "scenario_id".into(),
        serde_json::Value::String(scenario.id.clone()),
    );
    let mut summary = w.finalize(status, Some(&meta), None);

    // LLM judge over pass_criteria (P7) — HTTP backend (judge.base_url + key).
    if !scenario.pass_criteria.is_empty() {
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
        let criteria: Vec<String> = scenario.pass_criteria.clone();
        let verdict = if scenario.pass_judges.is_empty() {
            lks_core::judge::judge_run(
                cfg.judge.as_ref(),
                &cfg.simulator.api_key,
                &criteria,
                &turns,
                &tool_events,
            )
            .await
        } else {
            lks_core::judge::judge_run_multi(
                cfg.judge.as_ref(),
                &cfg.simulator.api_key,
                &criteria,
                &turns,
                &tool_events,
                &scenario.pass_judges,
                &scenario.pass_criteria_mode,
            )
            .await
        };
        summary.insert("verdict".into(), serde_json::Value::Object(verdict));
        // Re-write summary.json with the verdict (finalize wrote it already).
        let _ = std::fs::write(
            report_dir.join("summary.json"),
            serde_json::to_string_pretty(&summary).unwrap_or_default(),
        );
    }

    // Save conversation.wav (agent audio captured during the run).
    if let Ok(mut rec) = recorder.lock() {
        let _ = rec.save(&report_dir.join("conversation.wav"));
    }

    // Finish the sqlite row.
    {
        let store =
            lks_core::logging::sqlite::RunStore::new(cfg.sqlite_path().to_string_lossy().as_ref());
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

fn build_persona_prompt(scenario: &lks_core::scenario::Scenario) -> String {
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
