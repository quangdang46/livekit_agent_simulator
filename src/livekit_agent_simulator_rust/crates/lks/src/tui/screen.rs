//! Screen model + navigation actions for the lksr TUI.

use ratatui::Frame;
use std::path::Path;

use lks_core::config::SimConfig;

use crate::tui::home::HomeScreen;
use crate::tui::live_run::LiveRunScreen;
use crate::tui::preflight::PreflightScreen;
use crate::tui::run_detail::RunDetailScreen;
use crate::tui::run_setup::RunSetupScreen;
use crate::tui::scenario_detail::ScenarioDetailScreen;
use crate::tui::scenarios::ScenariosScreen;
use crate::tui::{cues::CuesScreen, log::LogScreen, plugins::PluginsScreen, runs::RunsScreen};

/// What a screen wants the app to do after handling a key.
#[derive(Debug, Default)]
pub enum NavAction {
    #[default]
    None,
    /// Push a new screen onto the drill-down stack.
    Push(Box<Screen>),
    /// Pop back one level.
    Pop,
    /// Show a transient message in the footer.
    Toast(String),
}

/// Every screen. Tab screens are top-level; the rest live on the stack.
#[derive(Debug)]
// LiveRun carries a RunSession (thread + channels); the size gap is expected
// for a screen enum — the boxed-Screen stack keeps allocation trivial.
#[allow(clippy::large_enum_variant)]
pub enum Screen {
    Home(HomeScreen),
    Scenarios(ScenariosScreen),
    ScenarioDetail(ScenarioDetailScreen),
    RunSetup(RunSetupScreen),
    LiveRun(LiveRunScreen),
    Runs(RunsScreen),
    RunDetail(RunDetailScreen),
    Log(LogScreen),
    Preflight(PreflightScreen),
    Cues(CuesScreen),
    Plugins(PluginsScreen),
}

impl Screen {
    /// Render this screen inside the given frame (with its area already split
    /// by the app: content area + footer).
    pub fn render(&mut self, f: &mut Frame, area: ratatui::layout::Rect, ctx: &ScreenCtx) {
        match self {
            Screen::Home(s) => s.render(f, area, ctx),
            Screen::Scenarios(s) => s.render(f, area, ctx),
            Screen::ScenarioDetail(s) => s.render(f, area, ctx),
            Screen::RunSetup(s) => s.render(f, area, ctx),
            Screen::LiveRun(s) => s.render(f, area, ctx),
            Screen::Runs(s) => s.render(f, area, ctx),
            Screen::RunDetail(s) => s.render(f, area, ctx),
            Screen::Log(s) => s.render(f, area, ctx),
            Screen::Preflight(s) => s.render(f, area, ctx),
            Screen::Cues(s) => s.render(f, area, ctx),
            Screen::Plugins(s) => s.render(f, area, ctx),
        }
    }

    /// Handle a key. Return the resulting nav action (default None).
    pub fn on_key(
        &mut self,
        key: ratatui::crossterm::event::KeyEvent,
        ctx: &ScreenCtx,
    ) -> NavAction {
        match self {
            Screen::Home(s) => s.on_key(key, ctx),
            Screen::Scenarios(s) => s.on_key(key, ctx),
            Screen::ScenarioDetail(s) => s.on_key(key, ctx),
            Screen::RunSetup(s) => s.on_key(key, ctx),
            Screen::LiveRun(s) => s.on_key(key, ctx),
            Screen::Runs(s) => s.on_key(key, ctx),
            Screen::RunDetail(s) => s.on_key(key, ctx),
            Screen::Log(s) => s.on_key(key, ctx),
            Screen::Preflight(s) => s.on_key(key, ctx),
            Screen::Cues(s) => s.on_key(key, ctx),
            Screen::Plugins(s) => s.on_key(key, ctx),
        }
    }

    /// Tab index (top-level screens only; drill-downs return None).
    pub fn tab_index(&self) -> Option<usize> {
        match self {
            Screen::Home(_) => Some(0),
            Screen::Scenarios(_) => Some(1),
            Screen::Runs(_) => Some(2),
            Screen::Preflight(_) => Some(3),
            Screen::Cues(_) => Some(4),
            Screen::Plugins(_) => Some(5),
            _ => None,
        }
    }

    /// A screen that is actively ticking (drains live channels / timers).
    pub fn needs_fast_tick(&self) -> bool {
        matches!(self, Screen::LiveRun(_))
    }
}

/// Context handed to every screen: the project root + loaded config.
pub struct ScreenCtx<'a> {
    pub root: &'a Path,
    pub cfg: &'a SimConfig,
}
