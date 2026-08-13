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

/// Run one scenario against the configured LiveKit agent (webrtc_sim slice).
pub async fn execute_scenario(
    project_root: &Path,
    scenario_id: &str,
    run_name: Option<&str>,
) -> Result<serde_json::Map<String, serde_json::Value>, RunError> {
    let cfg = load_config(project_root.to_path_buf(), None).map_err(|e| RunError(e.0))?;
    let scenario =
        find_scenario(&cfg.scenarios_dir(), scenario_id).map_err(|e| RunError(e.to_string()))?;

    // --- report dir + run id ---
    let reports_dir = cfg.reports_dir();
    std::fs::create_dir_all(&reports_dir).map_err(|e| RunError(format!("reports dir: {e}")))?;

    // seq from existing report dirs
    let seq = next_seq(&reports_dir);
    let slug = run_name.unwrap_or(scenario_id);
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
    let (_end_tx, end_rx) = broadcast::channel::<()>(1);

    // --- persona prompt (minimal: persona brief + goals) ---
    let persona_prompt = build_persona_prompt(&scenario);

    // --- caller bridge (webrtc_sim + openai provider) ---
    let room_name = format!("lks-{run_id}");
    let identity = format!("lks-caller-{}", &run_id[..8]);
    let writer_arc = Arc::new(Mutex::new(writer));

    let bridge = OpenAiCallerBridge::new(
        cfg.livekit.clone(),
        cfg.simulator.clone(),
        persona_prompt,
        run_spec.first_speaker.clone(),
        room_name,
        identity,
        writer_arc.clone(),
    );

    // The slice ends on the bridge's internal cap (agent hangup later).
    let run_result = bridge.run(end_rx).await;

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
    let summary = w.finalize(status, Some(&meta), None);

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
