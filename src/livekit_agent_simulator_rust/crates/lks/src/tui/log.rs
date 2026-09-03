//! Event log viewer for a run.

use ratatui::layout::Rect;
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{List, ListItem, ListState};
use ratatui::Frame;

use serde_json::{Map, Value};

use crate::tui::screen::{NavAction, ScreenCtx};
use crate::tui::widgets;

#[derive(Debug)]
pub struct LogScreen {
    run_id: String,
    lines: Vec<String>,
    state: ListState,
}

impl LogScreen {
    pub fn load(ctx: &ScreenCtx, run_id: &str) -> Self {
        let lines: Vec<String> =
            lks_core::ops::op_get_run_log(ctx.root, run_id, None, None, None, None, 500)
                .ok()
                .and_then(|m| m.get("events").and_then(Value::as_array).cloned())
                .unwrap_or_default()
                .iter()
                .map(|v| lks_core::logging::event::describe(v.as_object().unwrap_or(&Map::new())))
                .collect();
        let mut state = ListState::default();
        if !lines.is_empty() {
            state.select(Some(0));
        }
        LogScreen {
            run_id: run_id.to_string(),
            lines,
            state,
        }
    }

    pub fn render(&mut self, f: &mut Frame, area: Rect, _ctx: &ScreenCtx) {
        let items: Vec<ListItem> = self
            .lines
            .iter()
            .map(|l| ListItem::new(Line::from(Span::raw(widgets::truncate(l, 100)))))
            .collect();
        f.render_stateful_widget(
            List::new(items)
                .block(widgets::title_block(
                    &format!("Log: {}", self.run_id),
                    Some("↑↓ scroll"),
                ))
                .highlight_style(Style::default().add_modifier(Modifier::REVERSED)),
            area,
            &mut self.state,
        );
    }

    pub fn on_key(
        &mut self,
        key: ratatui::crossterm::event::KeyEvent,
        _ctx: &ScreenCtx,
    ) -> NavAction {
        use ratatui::crossterm::event::KeyCode;
        let n = self.lines.len();
        match key.code {
            KeyCode::Char('j') | KeyCode::Down => {
                let s = self.state.selected().unwrap_or(0);
                self.state
                    .select(Some(if n == 0 { 0 } else { (s + 1).min(n - 1) }));
                NavAction::None
            }
            KeyCode::Char('k') | KeyCode::Up => {
                let s = self.state.selected().unwrap_or(0);
                self.state.select(Some(s.saturating_sub(1)));
                NavAction::None
            }
            _ => NavAction::None,
        }
    }
}
