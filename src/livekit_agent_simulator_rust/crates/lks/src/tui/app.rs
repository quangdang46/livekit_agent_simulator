//! App state: navigation stack, config, caches, toast.

use std::path::{Path, PathBuf};
use std::time::Instant;

use lks_core::config::load_config;
use serde_json::{Map, Value};

use crate::tui::home::HomeScreen;
use crate::tui::scenarios::ScenariosScreen;
use crate::tui::screen::{NavAction, Screen, ScreenCtx};

pub const TAB_LABELS: [&str; 6] = ["Home", "Scenarios", "Runs", "Preflight", "Cues", "Plugins"];

pub struct App {
    root: PathBuf,
    pub cfg: lks_core::config::SimConfig,
    stack: Vec<Screen>,
    toast: Option<(String, Instant)>,
    pub scenarios_cache: Vec<Map<String, Value>>,
    pub runs_cache: Vec<Map<String, Value>>,
}

impl App {
    pub fn new(root: &Path) -> anyhow::Result<Self> {
        let cfg = load_config(root.to_path_buf(), None, None).map_err(|e| {
            anyhow::anyhow!(
                "load config: {e} (run `lksr init --root {}`)",
                root.display()
            )
        })?;
        let home = HomeScreen::default();
        let mut app = App {
            root: root.to_path_buf(),
            cfg,
            stack: vec![Screen::Home(home)],
            toast: None,
            scenarios_cache: Vec::new(),
            runs_cache: Vec::new(),
        };
        app.reload_all();
        Ok(app)
    }

    /// Build a screen context borrowing the app's root + cfg (disjoint from
    /// the navigation stack, so it can coexist with a mutable screen borrow).
    fn ctx(&self) -> ScreenCtx<'_> {
        ScreenCtx {
            root: &self.root,
            cfg: &self.cfg,
        }
    }

    fn current(&mut self) -> &mut Screen {
        self.stack.last_mut().expect("stack never empty")
    }

    fn top(&self) -> &Screen {
        self.stack.last().expect("stack never empty")
    }

    fn current_index(&self) -> usize {
        self.stack.len() - 1
    }

    /// The top-level tab currently active (0..TAB_LABELS.len()).
    pub fn active_tab(&self) -> usize {
        for s in self.stack.iter().rev() {
            if let Some(t) = s.tab_index() {
                return t;
            }
        }
        0
    }

    /// Refresh list caches (scenarios/runs) from disk.
    pub fn reload_all(&mut self) {
        self.scenarios_cache = lks_core::ops::op_list_scenarios(&self.root).unwrap_or_default();
        self.runs_cache = lks_core::ops::op_list_runs(&self.root, 50, None).unwrap_or_default();
    }

    /// Called every loop iteration — screens can tick (drain live channels).
    pub fn tick(&mut self) {
        // A live run tick is driven by LiveRunScreen.poll inside its own
        // render/on_key path; here we just expire the toast.
        if let Some((_, at)) = &self.toast {
            if at.elapsed() > std::time::Duration::from_secs(4) {
                self.toast = None;
            }
        }
    }

    /// Render the whole frame: content (current screen) + footer.
    pub fn render(&mut self, f: &mut ratatui::Frame) {
        let size = f.area();
        let content = ratatui::layout::Rect {
            height: size.height.saturating_sub(1).max(1),
            ..size
        };
        {
            // Build the ctx from disjoint fields so the mutable screen borrow
            // below doesn't overlap.
            let ctx = ScreenCtx {
                root: &self.root,
                cfg: &self.cfg,
            };
            self.stack
                .last_mut()
                .expect("stack never empty")
                .render(f, content, &ctx);
        }
        // Footer
        let footer = ratatui::layout::Rect {
            y: content.height,
            height: 1,
            ..size
        };
        let tabs = TAB_LABELS
            .iter()
            .enumerate()
            .map(|(i, t)| {
                let active = i == self.active_tab();
                ratatui::text::Span::styled(
                    format!(" {}:{t} ", i + 1),
                    ratatui::style::Style::default().add_modifier(if active {
                        ratatui::style::Modifier::REVERSED
                    } else {
                        ratatui::style::Modifier::empty()
                    }),
                )
            })
            .collect::<Vec<_>>();
        let mut line = ratatui::text::Line::from(tabs);
        line.spans.push(ratatui::text::Span::raw("  "));
        line.spans.push(ratatui::text::Span::styled(
            "[?]help [q]quit [Esc]back",
            ratatui::style::Style::default().fg(ratatui::style::Color::DarkGray),
        ));
        if let Some((msg, _)) = &self.toast {
            line.spans.push(ratatui::text::Span::raw("  "));
            line.spans.push(ratatui::text::Span::styled(
                msg.as_str(),
                ratatui::style::Style::default().fg(ratatui::style::Color::Cyan),
            ));
        }
        f.render_widget(ratatui::widgets::Paragraph::new(line), footer);
    }

    /// Handle one keypress. Returns true if the app should quit.
    pub fn on_key(&mut self, key: ratatui::crossterm::event::KeyEvent) -> anyhow::Result<bool> {
        use ratatui::crossterm::event::KeyCode;

        // Global keys first.
        match key.code {
            KeyCode::Char('q') | KeyCode::Char('Q') => {
                // If a live run is active, require Esc first (abort).
                if matches!(self.current(), Screen::LiveRun(_)) {
                    self.toast = Some(("run active — Esc to abort, then q".into(), Instant::now()));
                    return Ok(false);
                }
                return Ok(true);
            }
            KeyCode::Char('?') | KeyCode::Char('h') => {
                self.toast = Some((crate::tui::help::HELP.into(), Instant::now()));
                return Ok(false);
            }
            KeyCode::Tab => {
                let next = (self.active_tab() + 1) % TAB_LABELS.len();
                self.switch_tab(next);
                return Ok(false);
            }
            KeyCode::BackTab => {
                let prev = (self.active_tab() + TAB_LABELS.len() - 1) % TAB_LABELS.len();
                self.switch_tab(prev);
                return Ok(false);
            }
            KeyCode::Char(c) if c.is_ascii_digit() && ('1'..='6').contains(&c) => {
                let tab = c as usize - '1' as usize;
                self.switch_tab(tab);
                return Ok(false);
            }
            _ => {}
        }

        // Esc pops (unless it's a top-level tab screen, then it's a no-op).
        if key.code == KeyCode::Esc {
            if self.current_index() > 0 {
                self.stack.pop();
                self.reload_all();
            }
            return Ok(false);
        }

        // Disjoint field borrows (see render).
        let ctx = ScreenCtx {
            root: &self.root,
            cfg: &self.cfg,
        };
        let action = self
            .stack
            .last_mut()
            .expect("stack never empty")
            .on_key(key, &ctx);
        match action {
            NavAction::None => {}
            NavAction::Push(s) => {
                self.stack.push(*s);
            }
            NavAction::Pop => {
                if self.current_index() > 0 {
                    self.stack.pop();
                    self.reload_all();
                }
            }
            NavAction::Toast(m) => self.toast = Some((m, Instant::now())),
        }
        Ok(false)
    }

    fn switch_tab(&mut self, tab: usize) {
        let tab = tab.min(TAB_LABELS.len() - 1);
        let new_screen = match tab {
            0 => Screen::Home(HomeScreen::default()),
            1 => Screen::Scenarios(ScenariosScreen::load(&self.ctx())),
            2 => Screen::Runs(crate::tui::runs::RunsScreen::load(&self.ctx())),
            3 => Screen::Preflight(crate::tui::preflight::PreflightScreen::load(&self.ctx())),
            4 => Screen::Cues(crate::tui::cues::CuesScreen::load(&self.ctx())),
            _ => Screen::Plugins(crate::tui::plugins::PluginsScreen::load(&self.ctx())),
        };
        // Replace the stack with a fresh single screen for the tab.
        self.stack = vec![new_screen];
        self.reload_all();
    }

    pub fn poll_interval(&self) -> std::time::Duration {
        if self.top().needs_fast_tick() {
            std::time::Duration::from_millis(50)
        } else {
            std::time::Duration::from_millis(250)
        }
    }
}
