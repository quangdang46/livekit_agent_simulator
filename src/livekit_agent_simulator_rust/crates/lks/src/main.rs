//! lksr — livekit-agent-simulator Rust CLI entry point.
//!
//! P0: thin binary that parses `--version`/`--help` via clap. The full 23-command
//! CLI (22 data + `mcp`) lands in P4 (plan §P4). There is NO `version` subcommand
//! in the Python CLI — `lksr --version` comes from clap's built-in flag
//! (`#[command(version)]`), mirroring typer's built-in flag.
//! Binary name is `lksr` to distinguish from the Python `lks`.
//!
//! P5: `lksr mcp` spawns the MCP stdio server (same entry as Python's `lks mcp`).

use clap::{Parser, Subcommand};

/// Dial any LiveKit voice agent with an AI simulated caller and keep a full
/// forensic log. Same public ops as the MCP server.
#[derive(Parser, Debug)]
#[command(name = "lksr", version, about)]
struct Cli {
    #[command(subcommand)]
    command: Option<Command>,
}

#[derive(Subcommand, Debug)]
enum Command {
    /// Start the MCP server over stdio (same 21 tools as the Python server).
    Mcp,
    /// Validate then execute one scenario against the configured LiveKit agent.
    Execute {
        /// Scenario id in .agent-sim/scenarios/.
        scenario_id: String,
        /// Target repo root containing .agent-sim/ (default: current dir).
        #[arg(long, default_value = ".")]
        root: String,
        /// Override the run-name slug after the auto seq prefix.
        #[arg(long)]
        name: Option<String>,
    },
}

fn main() -> anyhow::Result<()> {
    let cli = Cli::parse();
    match cli.command {
        Some(Command::Mcp) => {
            let rt = tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()?;
            rt.block_on(lks_mcp::serve_stdio())
        }
        Some(Command::Execute {
            scenario_id,
            root,
            name,
        }) => {
            let rt = tokio::runtime::Builder::new_multi_thread()
                .enable_all()
                .build()?;
            let result = rt.block_on(lks_livekit::run::execute_scenario(
                std::path::Path::new(&root),
                &scenario_id,
                name.as_deref(),
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
        None => {
            // P0 smoke: no data commands yet. P4 wires the 22-command surface.
            println!("lksr {}", env!("CARGO_PKG_VERSION"));
            Ok(())
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use clap::{CommandFactory, Parser};

    #[test]
    fn cli_parses_help_and_no_args() {
        let cli = Cli::parse_from(["lksr"]);
        assert!(matches!(cli, Cli { command: None }));
        assert_eq!(env!("CARGO_PKG_VERSION"), "0.1.0-rust");
    }

    #[test]
    fn version_flag_is_wired() {
        let cmd = Cli::command();
        let about = cmd.get_version().expect("version set");
        assert_eq!(about, "0.1.0-rust");
    }

    #[test]
    fn mcp_subcommand_parses() {
        let cli = Cli::parse_from(["lksr", "mcp"]);
        assert!(matches!(cli.command, Some(Command::Mcp)));
    }
}
