//! lks — livekit-agent-simulator CLI entry point.
//!
//! P0: thin binary that parses `--version`/`--help` via clap. The full 23-command
//! CLI (22 data + `mcp`) lands in P4 (plan §P4). There is NO `version` subcommand
//! in the Python CLI — `lks --version` comes from clap's built-in flag
//! (`#[command(version)]`), mirroring typer's built-in flag.

use clap::Parser;

/// Dial any LiveKit voice agent with an AI simulated caller and keep a full
/// forensic log. Same public ops as the MCP server.
#[derive(Parser, Debug)]
#[command(name = "lks", version, about)]
struct Cli {}

fn main() {
    let _cli = Cli::parse();
    // P0 smoke: no commands yet. P4 wires the 23-command surface.
    println!("lks {}", env!("CARGO_PKG_VERSION"));
}

#[cfg(test)]
mod tests {
    use super::*;
    use clap::{CommandFactory, Parser};

    #[test]
    fn cli_parses_help_and_no_args() {
        // `Cli` is a zero-field struct: parsing plain args (or `--help`, which
        // clap also handles by exiting) must not panic. We can't invoke the
        // exiting `--version`/`--help` flags inside a test (they call process::exit),
        // so assert the struct parses cleanly and the package version is wired.
        let cli = Cli::parse_from(["lks"]);
        assert!(matches!(cli, Cli {}));
        assert_eq!(env!("CARGO_PKG_VERSION"), "0.1.0-rust");
    }

    #[test]
    fn version_flag_is_wired() {
        // `#[command(version)]` wires `--version` to CARGO_PKG_VERSION.
        // (We can't run `--version` here — clap exits the process — so assert the
        // version string is the one the flag would print.)
        let cmd = Cli::command();
        let about = cmd.get_version().expect("version set");
        assert_eq!(about, "0.1.0-rust");
    }
}
