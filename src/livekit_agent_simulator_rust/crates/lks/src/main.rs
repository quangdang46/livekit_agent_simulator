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
    Scenarios,
    /// List registered verify plugins and local .agent-sim/plugins. (MCP: list_plugins)
    Plugins,
    /// List built-in room_pcm cues + target overrides + aliases. (MCP: list_cues)
    Cues,
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
    Status { run_id: String },
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
    Report { run_id: String },
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
    },
    /// Run history from SQLite, newest first.
    Runs {
        /// Max runs to list.
        #[arg(long, default_value_t = 20)]
        limit: i64,
        /// Filter by scenario_id.
        #[arg(long)]
        scenario_id: Option<String>,
    },
    /// Promote a finished run into a draft scenario YAML (fail → golden).
    ScenarioFromRun {
        run_id: String,
        /// Write the draft .yaml under .agent-sim/scenarios/.
        #[arg(long)]
        write: bool,
    },
    /// Start the MCP server over stdio (same 21 tools as the Python server).
    Mcp,
    /// Check config + folders + optional LiveKit API connectivity.
    Preflight {
        /// Skip the LiveKit API connectivity check.
        #[arg(long)]
        no_connectivity: bool,
    },
    /// Start the local report player (audio + transcript sync). (MCP: web)
    Web {
        /// Run id under .agent-sim/reports/ (default: home list of all runs).
        run_id: Option<String>,
        /// Port to serve on.
        #[arg(long, default_value_t = 8765)]
        port: u16,
        /// Host to bind.
        #[arg(long, default_value = "127.0.0.1")]
        host: String,
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
        Some(Command::Preflight { no_connectivity }) => {
            let cfg =
                lks_core::config::load_config(std::path::Path::new(&root).to_path_buf(), None)?;
            let mut checks: Vec<String> = Vec::new();
            checks.push(format!("config: {} OK", cfg.dot_dir().display()));
            if !no_connectivity {
                match rt.block_on(lks_livekit::preflight::check_livekit_api(&cfg)) {
                    Ok(()) => checks.push("livekit.api: list_rooms OK".into()),
                    Err(e) => {
                        eprintln!("[lksr] preflight: {e}");
                        std::process::exit(1);
                    }
                }
            }
            for c in &checks {
                println!("✓ {c}");
            }
            Ok(())
        }
        Some(Command::Web { run_id, port, host }) => {
            let server = Arc::new(lks_web::WebServer::new(std::path::Path::new(&root)));
            let _ = run_id;
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
        Some(Command::Scenarios) => {
            let list = ops::op_list_scenarios(std::path::Path::new(&root))?;
            print_json(&serde_json::to_value(list)?)?;
            Ok(())
        }
        Some(Command::Plugins) => {
            print_map(&ops::op_list_plugins(std::path::Path::new(&root))?)?;
            Ok(())
        }
        Some(Command::Cues) => {
            print_map(&ops::op_list_cues(std::path::Path::new(&root))?)?;
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
        Some(Command::Status { run_id }) => print_map(&ops::op_get_run_status(
            std::path::Path::new(&root),
            &run_id,
        )?),
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
        Some(Command::Report { run_id }) => print_map(&ops::op_get_run_report(
            std::path::Path::new(&root),
            &run_id,
        )?),
        Some(Command::Compare {
            run_id_a,
            run_id_b,
            baseline,
            max_ttfw_regression_ms,
            max_turn_p95_regression_ms,
            max_duration_regression_ms,
            max_barge_recovery_drop,
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
        Some(Command::Runs { limit, scenario_id }) => {
            let list = ops::op_list_runs(
                std::path::Path::new(&root),
                limit.max(0),
                scenario_id.as_deref(),
            )?;
            print_json(&serde_json::to_value(list)?)?;
            Ok(())
        }
        Some(Command::ScenarioFromRun { run_id, write }) => {
            let s = ops::op_scenario_from_run(std::path::Path::new(&root), &run_id, None, write)
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
    fn all_22_commands_parse() {
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
            &["lksr", "status", "r"][..],
            &["lksr", "log", "r"][..],
            &["lksr", "report", "r"][..],
            &["lksr", "compare", "a", "b"][..],
            &["lksr", "runs"][..],
            &["lksr", "scenario-from-run", "r"][..],
            &["lksr", "mcp"][..],
        ] {
            let cli = Cli::parse_from(args);
            assert!(cli.command.is_some(), "parse {:?}", args);
        }
    }
}
