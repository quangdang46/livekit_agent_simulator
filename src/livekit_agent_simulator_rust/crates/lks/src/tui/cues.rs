//! Cues tab: list built-in + target room_pcm cues.

use ratatui::layout::Rect;
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{List, ListItem, ListState};
use ratatui::Frame;

use serde_json::Value;

use crate::tui::screen::{NavAction, ScreenCtx};
use crate::tui::widgets;

#[derive(Debug)]
pub struct CuesScreen {
    items: Vec<String>,
    state: ListState,
}

impl CuesScreen {
    pub fn load(ctx: &ScreenCtx) -> Self {
        let mut items = Vec::new();
        if let Ok(map) = lks_core::ops::op_list_cues(ctx.root) {
            for (group, list) in [
                ("builtin", "builtin"),
                ("target", "target"),
                ("aliases", "aliases"),
            ] {
                if let Some(arr) = map.get(list).and_then(Value::as_array) {
                    for c in arr {
                        let id = c.get("id").and_then(Value::as_str).unwrap_or("?");
                        let kind = c.get("kind").and_then(Value::as_str).unwrap_or("");
                        let desc = c.get("description").and_then(Value::as_str).unwrap_or("");
                        items.push(format!("[{group}] {id} ({kind}) — {desc}"));
                    }
                }
            }
        }
        let mut state = ListState::default();
        if !items.is_empty() {
            state.select(Some(0));
        }
        CuesScreen { items, state }
    }

    pub fn render(&mut self, f: &mut Frame, area: Rect, _ctx: &ScreenCtx) {
        let items: Vec<ListItem> = self
            .items
            .iter()
            .map(|c| ListItem::new(Line::from(Span::raw(widgets::truncate(c, 100)))))
            .collect();
        f.render_stateful_widget(
            List::new(items)
                .block(widgets::title_block("Cues", Some("r refresh")))
                .highlight_style(Style::default().add_modifier(Modifier::REVERSED)),
            area,
            &mut self.state,
        );
    }

    pub fn on_key(
        &mut self,
        key: ratatui::crossterm::event::KeyEvent,
        ctx: &ScreenCtx,
    ) -> NavAction {
        use ratatui::crossterm::event::KeyCode;
        let n = self.items.len();
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
            KeyCode::Char('r') => {
                *self = Self::load(ctx);
                NavAction::None
            }
            _ => NavAction::None,
        }
    }
}
