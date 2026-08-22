//! Plugins tab: list registered verify plugins.

use ratatui::layout::Rect;
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{List, ListItem, ListState};
use ratatui::Frame;

use serde_json::Value;

use crate::tui::screen::{NavAction, ScreenCtx};
use crate::tui::widgets;

#[derive(Debug)]
pub struct PluginsScreen {
    items: Vec<String>,
    state: ListState,
}

impl PluginsScreen {
    pub fn load(ctx: &ScreenCtx) -> Self {
        let mut items = Vec::new();
        if let Ok(map) = lks_core::ops::op_list_plugins(ctx.root) {
            // op_list_plugins returns {verify_plugins: [names], local_modules:
            // [file stems]} — render both (registered names first).
            if let Some(arr) = map.get("verify_plugins").and_then(Value::as_array) {
                for name in arr.iter().filter_map(Value::as_str) {
                    items.push(name.to_string());
                }
            }
            if let Some(arr) = map.get("local_modules").and_then(Value::as_array) {
                for m in arr.iter().filter_map(Value::as_str) {
                    items.push(format!("{m} (local)"));
                }
            }
        }
        if items.is_empty() {
            items.push("(no plugins)".into());
        }
        let mut state = ListState::default();
        state.select(Some(0));
        PluginsScreen { items, state }
    }

    pub fn render(&mut self, f: &mut Frame, area: Rect, _ctx: &ScreenCtx) {
        let items: Vec<ListItem> = self
            .items
            .iter()
            .map(|p| ListItem::new(Line::from(Span::raw(p.clone()))))
            .collect();
        f.render_stateful_widget(
            List::new(items)
                .block(widgets::title_block("Plugins", Some("r refresh")))
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
