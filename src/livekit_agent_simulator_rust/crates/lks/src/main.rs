//! lksr — livekit-agent-simulator Rust CLI entry point.
//!
//! Same public ops as the MCP server (22 data commands + `mcp`), mirroring the
//! Python `lks` CLI. `--version`/`--help` from clap (typer-equivalent).

use clap::{Parser, Subcommand};
use std::sync::Arc;

use lks_core::ops;

/// Dial any LiveKit voice agent with an AI simulated caller and keep a full
/// forensic log. Same public ops as the MCP server.
#[derive(Parser, Debug)]
#[command(name = "lksr", version, about)]
struct Cli {
    /// Project root containing .agent-sim/ (default: current directory).
    #[arg(long, global = true, default_value = ".")]
    root: String,

    #[command(subcommand)]
    command: Option<Command>,
}

#[derive(Subcommand, Debug)]
enum Command {
    /// Scaffold .agent-sim/ in the target repo. (MCP: init_project)
    Init,
    /// Print setup/ops guide for agents and humans. (MCP: guide)
    Guide,
    /// List all scenarios with id, tags, and validity. (MCP: list_scenarios)
    Scenarios {
        /// Emit raw JSON instead of a human-readable table.
        #[arg(long)]
        json: bool,
    },
    /// List registered verify plugins and local .agent-sim/plugins. (MCP: list_plugins)
    Plugins {
        /// Emit raw JSON instead of a human-readable table.
        #[arg(long)]
        json: bool,
    },
    /// List built-in room_pcm cues + target overrides + aliases. (MCP: list_cues)
    Cues {
        /// Resolve one asset id/path and print path (builtin:voice.barge_short, …).
        #[arg(long)]
        resolve: Option<String>,
        /// Emit raw JSON instead of a human-readable table.
        #[arg(long)]
        json: bool,
    },
    /// Validate a scenario file: schema, required Persona brief, PassCriteria lint.
    Validate {
        scenario_id: String,
        /// Emit raw JSON instead of a human-readable table.
        #[arg(long)]
        json: bool,
    },
    /// Export a parsed scenario as JSON.
    Export {
        scenario_id: String,
        #[arg(long)]
        json: bool,
    },
    /// Convert a legacy .jsonl scenario to .yaml (keeps the .jsonl, idempotent).
    Convert {
        scenario_id: String,
        /// Overwrite existing .yaml.
        #[arg(long)]
        force: bool,
        #[arg(long)]
        json: bool,
    },
    /// Scaffold .agent-sim/scenarios/<id>.yaml with # guide comments.
    ScenarioInit {
        scenario_id: String,
        /// Overwrite existing file.
        #[arg(long)]
        force: bool,
    },
    /// Execute multiple scenarios; print suite matrix + CI gate. (MCP: execute_scenarios)
    ExecuteAll {
        /// Optional scenario ids; omit to run all valid scenarios.
        scenario_ids: Vec<String>,
        /// Only scenarios with this tag (when ids omitted).
        #[arg(long)]
        tag: Option<String>,
        /// Fail suite if any LLM judge verdict is fail (default: hard gates only).
        #[arg(long)]
        strict_judge: bool,
        /// Do not write suite-*.json/md under .agent-sim/reports/.
        #[arg(long)]
        no_report: bool,
        /// Repeat each scenario N times for flake control (pass@k).
        #[arg(long, short = 'n', default_value_t = 1)]
        repeat: i64,
        /// Minimum hard-pass iterations per scenario (default = repeat).
        #[arg(long, short = 'k')]
        pass_at_k: Option<i64>,
        /// Run up to N scenarios at once (default 1 = sequential).
        #[arg(long, short = 'p', default_value_t = 1)]
        parallel: i64,
        /// Cooldown seconds after a scenario finishes before the next starts.
        #[arg(long, default_value_t = 0.0)]
        wait: f64,
        /// Override the target LiveKit worker name for this run.
        #[arg(long)]
        agent_name: Option<String>,
        /// Select a named `simulator.profiles.<name>` caller profile.
        #[arg(long)]
        profile: Option<String>,
        /// Emit raw JSON instead of a human-readable table.
        #[arg(long)]
        json: bool,
    },
    /// Validate then run an in-memory scenario JSON. (MCP: execute_scenario_dict)
    ExecuteDict {
        /// JSON file with scenario dict; omit to read JSON from stdin.
        #[arg(long, short = 'f')]
        file: Option<String>,
        /// Override slug after seq prefix.
        #[arg(long)]
        name: Option<String>,
        /// Override the target LiveKit worker name for this run.
        #[arg(long)]
        agent_name: Option<String>,
        /// Select a named `simulator.profiles.<name>` caller profile.
        #[arg(long)]
        profile: Option<String>,
        /// Emit raw JSON instead of a human-readable table.
        #[arg(long)]
        json: bool,
    },
    /// Run the persona-prompt optimizer over a dataset (live benchmark loop).
    Optimize {
        /// Comma-separated scenario ids (dataset) to optimize over.
        scenario_ids: String,
        /// Scenario id held out for generalization check.
        #[arg(long)]
        held_out: Option<String>,
        /// Max candidate variants to evaluate.
        #[arg(long, short = 'c', default_value_t = 4)]
        candidates: i64,
        /// Cap on LLM-proposed variants.
        #[arg(long, default_value_t = 6)]
        max_candidates: i64,
        /// Treat judge fail as hard fail.
        #[arg(long)]
        strict_judge: bool,
        /// Run each scenario N times for pass@k.
        #[arg(long, short = 'n', default_value_t = 1)]
        repeat: i64,
        /// Min hard-pass iterations.
        #[arg(long, short = 'k')]
        pass_at_k: Option<i64>,
        /// Override target worker name.
        #[arg(long)]
        agent_name: Option<String>,
        /// Artifact slug (default auto).
        #[arg(long)]
        name: Option<String>,
        /// Select a named `simulator.profiles.<name>` caller profile.
        #[arg(long)]
        profile: Option<String>,
        /// Emit raw JSON instead of a human-readable table.
        #[arg(long)]
        json: bool,
    },
    /// Validate then execute one scenario against the configured LiveKit agent.
    Execute {
        scenario_id: String,
        /// Override the run-name slug after the auto seq prefix.
        #[arg(long)]
        name: Option<String>,
        /// Run the scenario N times for flake control (pass@k).
        #[arg(long, short = 'n', default_value_t = 1)]
        repeat: i64,
        /// Minimum hard-pass iterations (default = repeat).
        #[arg(long, short = 'k')]
        pass_at_k: Option<i64>,
        /// Override the target LiveKit worker name for this run.
        #[arg(long)]
        agent_name: Option<String>,
        /// Apply a saved `lks optimize` artifact as the persona-prompt override.
        #[arg(long)]
        optimized: Option<String>,
        /// Select a named `simulator.profiles.<name>` caller profile.
        #[arg(long)]
        profile: Option<String>,
        /// Emit raw JSON instead of a human-readable table.
        #[arg(long)]
        json: bool,
    },
    /// Status of a run from SQLite: running / done / failed.
    Status {
        run_id: String,
        /// Emit raw JSON instead of a human-readable table.
        #[arg(long)]
        json: bool,
    },
    /// Read events.jsonl with filters. `kind` supports trailing `*` prefix match.
    Log {
        run_id: String,
        /// Filter by event kind (trailing `*` supported, e.g. tool.*).
        #[arg(long)]
        kind: Option<String>,
        /// Filter by turn number.
        #[arg(long)]
        turn: Option<i64>,
        /// Filter by source.
        #[arg(long)]
        source: Option<String>,
        /// Only events at or after this ts_mono_ms.
        #[arg(long)]
        since_mono_ms: Option<i64>,
        /// Max events to return.
        #[arg(long, default_value_t = 200)]
        limit: i64,
        #[arg(long)]
        json: bool,
    },
    /// Full report: summary, judge, suspicious turns, paths.
    Report {
        run_id: String,
        /// Emit raw JSON instead of a human-readable table.
        #[arg(long)]
        json: bool,
    },
    /// Diff two runs. --baseline hard-fails regressions.
    Compare {
        run_id_a: String,
        run_id_b: String,
        /// run_id_a is the golden baseline; gate hard-fails regressions.
        #[arg(long)]
        baseline: bool,
        #[arg(long, default_value_t = 1500.0)]
        max_ttfw_regression_ms: f64,
        #[arg(long, default_value_t = 2000.0)]
        max_turn_p95_regression_ms: f64,
        #[arg(long, default_value_t = 30000.0)]
        max_duration_regression_ms: f64,
        #[arg(long, default_value_t = 0.0)]
        max_barge_recovery_drop: f64,
        /// Emit raw JSON instead of a human-readable table.
        #[arg(long)]
        json: bool,
    },
    /// Run history from SQLite, newest first.
    Runs {
        /// Max runs to list.
        #[arg(long, default_value_t = 20)]
        limit: i64,
        /// Filter by scenario_id.
        #[arg(long)]
        scenario_id: Option<String>,
        /// Emit raw JSON instead of a human-readable table.
        #[arg(long)]
        json: bool,
    },
    /// Promote a finished run into a draft scenario YAML (fail → golden).
    ScenarioFromRun {
        run_id: String,
        /// Override draft scenario id (default: auto from source).
        #[arg(long)]
        id: Option<String>,
        /// Write the draft .yaml under .agent-sim/scenarios/.
        #[arg(long, short = 'w')]
        write: bool,
    },
    /// Start the lks REST API (JSON over HTTP; same ops as CLI/MCP). Ctrl+C to stop.
    Serve {
        /// Port to serve on.
        #[arg(long, short = 'p', default_value_t = 8787)]
        port: u16,
        /// Host to bind.
        #[arg(long, default_value = "127.0.0.1")]
        host: String,
    },
    /// Start the MCP server over stdio (same 21 tools as the Python server).
    Mcp,
    /// Check config + folders + optional LiveKit API connectivity.
    Preflight {
        /// Skip the LiveKit API connectivity check.
        #[arg(long)]
        no_connectivity: bool,
        /// Select a named `simulator.profiles.<name>` caller profile.
        #[arg(long)]
        profile: Option<String>,
        /// Emit raw JSON instead of a human-readable table.
        #[arg(long)]
        json: bool,
    },
    /// Start the local report player (audio + transcript sync). (MCP: web)
    Web {
        /// Run id under .agent-sim/reports/ (default: home list of all runs).
        run_id: Option<String>,
        /// Port to serve on.
        #[arg(long, short = 'p', default_value_t = 8765)]
        port: u16,
        /// Host to bind.
        #[arg(long, default_value = "127.0.0.1")]
        host: String,
        /// Do not open a browser.
        #[arg(long)]
        no_open: bool,
    },
}

fn main() -> anyhow::Result<()> {
    // reqwest + livekit both use rustls — install one default CryptoProvider.
    let _ = rustls::crypto::ring::default_provider().install_default();
    let cli = Cli::parse();
    let root = cli.root.clone();
    let rt = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()?;

    match cli.command {
        None => {
            println!("lksr {}", env!("CARGO_PKG_VERSION"));
            Ok(())
        }
        Some(Command::Mcp) => {
            let rt = tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()?;
            rt.block_on(lks_mcp::serve_stdio())
        }
        Some(Command::Serve { port, host }) => {
            let server = Arc::new(lks_web::api_server::ApiServer::new(std::path::Path::new(
                &root,
            )));
            let addr = rt.block_on(lks_web::api_server::serve_api(server, &host, port))?;
            eprintln!(
                "[lksr] REST API: http://{addr}{} (root: {root}) — Ctrl+C to stop",
                lks_web::api_server::PREFIX
            );
            std::thread::park();
            Ok(())
        }
        Some(Command::Preflight {
            no_connectivity,
            profile,
            json,
        }) => {
            let result = rt.block_on(lks_livekit::preflight::op_preflight(
                std::path::Path::new(&root),
                !no_connectivity,
                profile.as_deref(),
            ))?;
            if json {
                print_json(&serde_json::to_value(&result)?)?;
            } else {
                for c in result
                    .get("checks")
                    .and_then(|v| v.as_array())
                    .cloned()
                    .unwrap_or_default()
                {
                    let name = c.get("name").and_then(|v| v.as_str()).unwrap_or("?");
                    let pass = c.get("pass").and_then(|v| v.as_bool()).unwrap_or(false);
                    let detail = c.get("detail").and_then(|v| v.as_str()).unwrap_or("");
                    println!("{} {name}: {detail}", if pass { "✓" } else { "✗" });
                }
            }
            if !result.get("ok").and_then(|v| v.as_bool()).unwrap_or(false) {
                std::process::exit(1);
            }
            Ok(())
        }
        Some(Command::Web {
            run_id,
            port,
            host,
            no_open,
        }) => {
            let server = Arc::new(lks_web::WebServer::new(std::path::Path::new(&root)));
            let _ = (run_id, no_open);
            let addr = rt.block_on(lks_web::serve(server, &host, port))?;
            eprintln!("[lksr] report UI: http://{addr} — Ctrl+C to stop");
            std::thread::park();
            Ok(())
        }
        Some(Command::Init) => {
            print_map(&ops::op_init_project(std::path::Path::new(&root))?)?;
            Ok(())
        }
        Some(Command::Guide) => {
            let g = ops::guide()?;
            println!("{}", g.get("text").and_then(|v| v.as_str()).unwrap_or(""));
            Ok(())
        }
        Some(Command::Scenarios { json }) => {
            let list = ops::op_list_scenarios(std::path::Path::new(&root))?;
            if json {
                print_json(&serde_json::to_value(list)?)?;
            } else {
                for item in &list {
                    let id = item.get("id").and_then(|v| v.as_str()).unwrap_or("?");
                    let valid = item.get("valid").and_then(|v| v.as_bool()).unwrap_or(false);
                    let err = item.get("error").and_then(|v| v.as_str()).unwrap_or("");
                    println!(
                        "{} {id}{}",
                        if valid { "✓" } else { "✗" },
                        if err.is_empty() {
                            String::new()
                        } else {
                            format!(" — {err}")
                        }
                    );
                }
            }
            Ok(())
        }
        Some(Command::Plugins { json }) => {
            let p = ops::op_list_plugins(std::path::Path::new(&root))?;
            if json {
                print_map(&p)?;
            } else {
                for plug in p
                    .get("plugins")
                    .and_then(|v| v.as_array())
                    .cloned()
                    .unwrap_or_default()
                {
                    println!(
                        "{}",
                        plug.get("name").and_then(|v| v.as_str()).unwrap_or("?")
                    );
                }
            }
            Ok(())
        }
        Some(Command::Cues { resolve, json }) => {
            if let Some(asset) = &resolve {
                let root_path = std::path::Path::new(&root);
                let dot = root_path.join(".agent-sim");
                if dot.is_dir() {
                    let cfg = lks_core::config::load_config(root_path.to_path_buf(), None).ok();
                    if let Some(cfg) = cfg {
                        let cues = cfg.cues_dir();
                        let cand = if let Some(name) = asset.strip_prefix("builtin:") {
                            cues.join(format!("{name}.wav"))
                        } else {
                            cues.join(asset)
                        };
                        println!("{}", cand.display());
                        return Ok(());
                    }
                }
                println!("{asset}");
                return Ok(());
            }
            let c = ops::op_list_cues(std::path::Path::new(&root))?;
            if json {
                print_map(&c)?;
            } else {
                for cue in c
                    .get("cues")
                    .and_then(|v| v.as_array())
                    .cloned()
                    .unwrap_or_default()
                {
                    println!("{}", cue.get("id").and_then(|v| v.as_str()).unwrap_or("?"));
                }
            }
            Ok(())
        }
        Some(Command::Validate { scenario_id, json }) => {
            let v = ops::op_validate_scenario(std::path::Path::new(&root), &scenario_id)?;
            if json {
                print_json(&serde_json::to_value(v)?)?;
            } else {
                let valid = v.get("valid").and_then(|x| x.as_bool()).unwrap_or(false);
                println!("valid: {}", if valid { "✓" } else { "✗" });
                if let Some(e) = v.get("error").and_then(|x| x.as_str()) {
                    println!("error: {e}");
                }
                if let Some(w) = v.get("warnings").and_then(|x| x.as_array()) {
                    for msg in w {
                        println!("warn: {}", msg.as_str().unwrap_or(""));
                    }
                }
            }
            Ok(())
        }
        Some(Command::Export {
            scenario_id,
            json: _,
        }) => {
            let e = ops::op_export_scenario(std::path::Path::new(&root), &scenario_id)?;
            print_json(&serde_json::to_value(e)?)?;
            Ok(())
        }
        Some(Command::Convert {
            scenario_id,
            force,
            json,
        }) => {
            let c = ops::op_convert_scenario(std::path::Path::new(&root), &scenario_id, force)
                .map_err(anyhow::Error::msg)?;
            if json {
                print_json(&serde_json::to_value(c)?)?;
            } else {
                println!(
                    "converted: {}",
                    c.get("written_to").and_then(|v| v.as_str()).unwrap_or("?")
                );
            }
            Ok(())
        }
        Some(Command::ScenarioInit { scenario_id, force }) => print_map(&ops::op_init_scenario(
            std::path::Path::new(&root),
            &scenario_id,
            force,
        )?),
        Some(Command::Execute {
            scenario_id,
            name,
            repeat,
            pass_at_k,
            agent_name,
            optimized,
            profile,
            json: _,
        }) => {
            let opts = lks_livekit::run::ExecuteOptions {
                run_name: name,
                repeat,
                pass_at_k,
                agent_name,
                optimized,
                profile,
            };
            let result = rt.block_on(lks_livekit::run::execute_scenario(
                std::path::Path::new(&root),
                &scenario_id,
                &opts,
            ))?;
            println!(
                "run_id: {}\nstatus: {}\nreport_dir: {}",
                result.get("run_id").and_then(|v| v.as_str()).unwrap_or("?"),
                result.get("status").and_then(|v| v.as_str()).unwrap_or("?"),
                result
                    .get("report_dir")
                    .and_then(|v| v.as_str())
                    .unwrap_or("?"),
            );
            if result.get("status").and_then(|v| v.as_str()) != Some("done") {
                std::process::exit(1);
            }
            Ok(())
        }
        Some(Command::ExecuteAll {
            scenario_ids,
            tag,
            strict_judge,
            no_report,
            repeat,
            pass_at_k,
            parallel,
            wait,
            agent_name,
            profile,
            json,
        }) => {
            let opts = lks_livekit::ops_execute::SuiteOptions {
                scenario_ids: if scenario_ids.is_empty() {
                    None
                } else {
                    Some(scenario_ids)
                },
                tag,
                strict_judge,
                write_report: !no_report,
                repeat,
                pass_at_k,
                parallel,
                wait_s: wait,
                agent_name,
                profile,
            };
            let result = rt.block_on(lks_livekit::ops_execute::op_execute_scenarios(
                std::path::Path::new(&root),
                &opts,
            ))?;
            if json {
                print_json(&serde_json::to_value(&result)?)?;
            } else {
                let suite = result
                    .get("suite")
                    .and_then(|v| v.as_object())
                    .cloned()
                    .unwrap_or_default();
                let ok = suite.get("ok").and_then(|v| v.as_bool()).unwrap_or(false);
                let exit = suite.get("exit_code").and_then(|v| v.as_i64()).unwrap_or(1);
                println!(
                    "suite {} (CI) · exit {exit}",
                    if ok { "ok" } else { "FAILED" }
                );
                if let Some(rows) = suite.get("matrix").and_then(|v| v.as_array()) {
                    for row in rows {
                        let r = row.as_object().cloned().unwrap_or_default();
                        println!(
                            "{} | gate={} | status={} | run_id={}",
                            r.get("scenario_id").and_then(|v| v.as_str()).unwrap_or("?"),
                            r.get("gate").and_then(|v| v.as_str()).unwrap_or("?"),
                            r.get("status").and_then(|v| v.as_str()).unwrap_or("?"),
                            r.get("run_id").and_then(|v| v.as_str()).unwrap_or("—"),
                        );
                    }
                }
            }
            let suite_ok = result
                .get("suite")
                .and_then(|v| v.as_object())
                .and_then(|s| s.get("ok"))
                .and_then(|v| v.as_bool())
                .unwrap_or(false);
            if !suite_ok {
                std::process::exit(1);
            }
            Ok(())
        }
        Some(Command::ExecuteDict {
            file,
            name,
            agent_name,
            profile,
            json: _,
        }) => {
            let scenario_json: serde_json::Value = if let Some(f) = &file {
                let text = std::fs::read_to_string(f)
                    .map_err(|e| anyhow::anyhow!("Invalid scenario JSON: {e}"))?;
                serde_json::from_str(&text)
                    .map_err(|e| anyhow::anyhow!("Invalid scenario JSON: {e}"))?
            } else {
                let mut buf = String::new();
                use std::io::Read;
                std::io::stdin()
                    .read_to_string(&mut buf)
                    .map_err(|e| anyhow::anyhow!("stdin read: {e}"))?;
                serde_json::from_str(&buf)
                    .map_err(|e| anyhow::anyhow!("Invalid scenario JSON: {e}"))?
            };
            let scenario = scenario_json
                .as_object()
                .cloned()
                .ok_or_else(|| anyhow::anyhow!("Scenario JSON must be an object"))?;
            let result = rt.block_on(lks_livekit::ops_execute::op_execute_scenario_dict(
                std::path::Path::new(&root),
                &scenario,
                name.as_deref(),
                agent_name.as_deref(),
                profile.as_deref(),
            ))?;
            print_json(&serde_json::to_value(&result)?)?;
            let ok = result
                .get("executed")
                .and_then(|v| v.as_bool())
                .unwrap_or(false)
                && result.get("status").and_then(|v| v.as_str()) == Some("done");
            if !ok {
                std::process::exit(1);
            }
            Ok(())
        }
        Some(Command::Optimize {
            scenario_ids,
            held_out,
            candidates,
            max_candidates,
            strict_judge,
            repeat,
            pass_at_k,
            agent_name,
            name,
            profile,
            json: _,
        }) => {
            let ids: Vec<String> = scenario_ids
                .split(',')
                .map(|s| s.trim().to_string())
                .filter(|s| !s.is_empty())
                .collect();
            if ids.is_empty() {
                anyhow::bail!("scenario_ids must be a non-empty comma-separated list");
            }
            let opts = lks_livekit::ops_execute::OptimizeOptions {
                scenario_ids: ids,
                held_out,
                candidates,
                max_candidates,
                strict_judge,
                repeat,
                pass_at_k,
                agent_name,
                name,
                profile,
            };
            let result = rt.block_on(lks_livekit::ops_execute::op_optimize_persona(
                std::path::Path::new(&root),
                &opts,
            ))?;
            print_json(&serde_json::to_value(&result)?)?;
            let winner_is_null = result.get("winner").map(|v| v.is_null()).unwrap_or(true);
            if winner_is_null {
                eprintln!("No candidate beat baseline — keeping the builtin prompt.");
            }
            Ok(())
        }
        Some(Command::Status { run_id, json }) => {
            let s = ops::op_get_run_status(std::path::Path::new(&root), &run_id)?;
            if json {
                print_map(&s)?;
            } else {
                println!(
                    "run_id: {}\nstatus: {}\nscenario_id: {}",
                    s.get("run_id").and_then(|v| v.as_str()).unwrap_or("?"),
                    s.get("status").and_then(|v| v.as_str()).unwrap_or("?"),
                    s.get("scenario_id").and_then(|v| v.as_str()).unwrap_or("?"),
                );
            }
            Ok(())
        }
        Some(Command::Log {
            run_id,
            kind,
            turn,
            source,
            since_mono_ms,
            limit,
            json,
        }) => {
            let l = ops::op_get_run_log(
                std::path::Path::new(&root),
                &run_id,
                kind.as_deref(),
                turn,
                source.as_deref(),
                since_mono_ms,
                limit.max(0) as usize,
            )?;
            if json {
                print_json(&serde_json::to_value(l)?)?;
            } else if let Some(events) = l.get("events").and_then(|v| v.as_array()) {
                for e in events {
                    println!("{}", serde_json::to_string(e).unwrap_or_default());
                }
            }
            Ok(())
        }
        Some(Command::Report { run_id, json }) => {
            let r = ops::op_get_run_report(std::path::Path::new(&root), &run_id)?;
            if json {
                print_map(&r)?;
            } else {
                if let Some(summary) = r.get("summary").and_then(|v| v.as_object()) {
                    println!(
                        "run_id: {}\nstatus: {}\nduration_ms: {}\nturn_count: {}",
                        summary
                            .get("run_id")
                            .and_then(|v| v.as_str())
                            .unwrap_or(&run_id),
                        summary
                            .get("status")
                            .and_then(|v| v.as_str())
                            .unwrap_or("?"),
                        summary
                            .get("duration_ms")
                            .and_then(|v| v.as_i64())
                            .unwrap_or(0),
                        summary
                            .get("turn_count")
                            .and_then(|v| v.as_i64())
                            .unwrap_or(0),
                    );
                } else {
                    println!("found: false\nrun_id: {run_id}");
                }
            }
            Ok(())
        }
        Some(Command::Compare {
            run_id_a,
            run_id_b,
            baseline,
            max_ttfw_regression_ms,
            max_turn_p95_regression_ms,
            max_duration_regression_ms,
            max_barge_recovery_drop,
            json: _,
        }) => {
            let r = if baseline {
                ops::op_compare_runs_with_baseline(
                    std::path::Path::new(&root),
                    &run_id_a,
                    &run_id_b,
                    max_ttfw_regression_ms,
                    max_turn_p95_regression_ms,
                    max_duration_regression_ms,
                    max_barge_recovery_drop,
                )?
            } else {
                ops::op_compare_runs(std::path::Path::new(&root), &run_id_a, &run_id_b)?
            };
            print_map(&r)
        }
        Some(Command::Runs {
            limit,
            scenario_id,
            json,
        }) => {
            let list = ops::op_list_runs(
                std::path::Path::new(&root),
                limit.max(0),
                scenario_id.as_deref(),
            )?;
            if json {
                print_json(&serde_json::to_value(list)?)?;
            } else {
                for run in &list {
                    println!(
                        "{} | {} | {}",
                        run.get("run_id").and_then(|v| v.as_str()).unwrap_or("?"),
                        run.get("scenario_id")
                            .and_then(|v| v.as_str())
                            .unwrap_or("?"),
                        run.get("status").and_then(|v| v.as_str()).unwrap_or("?"),
                    );
                }
            }
            Ok(())
        }
        Some(Command::ScenarioFromRun { run_id, id, write }) => {
            let s = ops::op_scenario_from_run(
                std::path::Path::new(&root),
                &run_id,
                id.as_deref(),
                write,
            )
            .map_err(anyhow::Error::msg)?;
            print_map(&s)
        }
    }
}

fn print_json(v: &serde_json::Value) -> anyhow::Result<()> {
    println!("{}", serde_json::to_string_pretty(v)?);
    Ok(())
}

fn print_map(m: &serde_json::Map<String, serde_json::Value>) -> anyhow::Result<()> {
    print_json(&serde_json::Value::Object(m.clone()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use clap::{CommandFactory, Parser};

    #[test]
    fn cli_parses_help_and_no_args() {
        let cli = Cli::parse_from(["lksr"]);
        assert!(matches!(cli, Cli { command: None, .. }));
        assert_eq!(env!("CARGO_PKG_VERSION"), "0.1.0-rust");
    }

    #[test]
    fn version_flag_is_wired() {
        let cmd = Cli::command();
        let about = cmd.get_version().expect("version set");
        assert_eq!(about, "0.1.0-rust");
    }

    #[test]
    fn all_23_commands_parse() {
        for args in [
            &["lksr", "init"][..],
            &["lksr", "guide"][..],
            &["lksr", "scenarios"][..],
            &["lksr", "plugins"][..],
            &["lksr", "cues"][..],
            &["lksr", "validate", "x"][..],
            &["lksr", "export", "x"][..],
            &["lksr", "convert", "x"][..],
            &["lksr", "scenario-init", "x"][..],
            &["lksr", "execute", "x"][..],
            &["lksr", "execute-all", "a", "b"][..],
            &["lksr", "execute-dict"][..],
            &["lksr", "optimize", "a,b"][..],
            &["lksr", "status", "r"][..],
            &["lksr", "log", "r"][..],
            &["lksr", "report", "r"][..],
            &["lksr", "compare", "a", "b"][..],
            &["lksr", "runs"][..],
            &["lksr", "scenario-from-run", "r"][..],
            &["lksr", "preflight"][..],
            &["lksr", "web"][..],
            &["lksr", "mcp"][..],
        ] {
            let cli = Cli::parse_from(args);
            assert!(cli.command.is_some(), "parse {:?}", args);
        }
    }

    #[test]
    fn new_commands_accept_python_flags() {
        // execute-all flags match cli.py
        let cli = Cli::parse_from([
            "lksr",
            "execute-all",
            "--tag",
            "smoke",
            "--strict-judge",
            "--no-report",
            "--repeat",
            "3",
            "--pass-at-k",
            "2",
            "--parallel",
            "2",
            "--wait",
            "5",
            "--agent-name",
            "worker",
            "--profile",
            "gemini",
            "--json",
        ]);
        match cli.command {
            Some(Command::ExecuteAll { tag, repeat, .. }) => {
                assert_eq!(tag.as_deref(), Some("smoke"));
                assert_eq!(repeat, 3);
            }
            other => panic!("expected ExecuteAll, got {other:?}"),
        }
        // execute-dict --file
        let cli = Cli::parse_from(["lksr", "execute-dict", "-f", "scenario.json"]);
        match cli.command {
            Some(Command::ExecuteDict { file, .. }) => {
                assert_eq!(file.as_deref(), Some("scenario.json"));
            }
            other => panic!("expected ExecuteDict, got {other:?}"),
        }
        // optimize flags
        let cli = Cli::parse_from([
            "lksr",
            "optimize",
            "a,b",
            "--held-out",
            "c",
            "--candidates",
            "2",
        ]);
        match cli.command {
            Some(Command::Optimize {
                scenario_ids,
                held_out,
                candidates,
                ..
            }) => {
                assert_eq!(scenario_ids, "a,b");
                assert_eq!(held_out.as_deref(), Some("c"));
                assert_eq!(candidates, 2);
            }
            other => panic!("expected Optimize, got {other:?}"),
        }
        // web --no-open + preflight --profile/--json
        let cli = Cli::parse_from(["lksr", "web", "--no-open"]);
        match cli.command {
            Some(Command::Web { no_open, .. }) => assert!(no_open),
            other => panic!("expected Web, got {other:?}"),
        }
        let cli = Cli::parse_from(["lksr", "preflight", "--profile", "gemini", "--json"]);
        match cli.command {
            Some(Command::Preflight { profile, json, .. }) => {
                assert_eq!(profile.as_deref(), Some("gemini"));
                assert!(json);
            }
            other => panic!("expected Preflight, got {other:?}"),
        }
        // scenario-from-run --id
        let cli = Cli::parse_from(["lksr", "scenario-from-run", "r", "--id", "golden"]);
        match cli.command {
            Some(Command::ScenarioFromRun { id, .. }) => {
                assert_eq!(id.as_deref(), Some("golden"));
            }
            other => panic!("expected ScenarioFromRun, got {other:?}"),
        }
    }
}
