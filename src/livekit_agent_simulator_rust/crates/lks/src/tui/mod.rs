//! `lksr tui` — full-feature interactive terminal UI for lksr.
//!
//! Phase A stub: the live-run engine is in place; screens land in Phase B.
//! Entry stays `pub fn run(root)` so `main.rs` is unchanged.

// The engine is not yet wired to a screen (Phase B) — silence dead-code
// warnings until then; the module is covered by unit tests.
#![allow(dead_code)]

pub mod live_run;

/// Entry point — kept minimal until Phase B lands the screens.
pub fn run(_root: &std::path::Path) -> anyhow::Result<()> {
    eprintln!("lksr tui: full-feature TUI is under construction (Phase B)");
    Ok(())
}
