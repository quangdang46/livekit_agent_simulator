//! Scenarios tab: list all scenarios → open detail.

use ratatui::layout::Rect;
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{List, ListItem, ListState};
use ratatui::Frame;

use serde_json::Value;

use crate::tui::scenario_detail::ScenarioDetailScreen;
use crate::tui::screen::{NavAction, Screen, ScreenCtx};
use crate::tui::widgets;

#[derive(Debug)]
pub struct ScenariosScreen {
    items: Vec<ScenarioRow>,
    state: ListState,
}

#[derive(Debug, Clone)]
struct ScenarioRow {
    id: String,
    tags: String,
    valid: bool,
    error: Option<String>,
}

impl ScenariosScreen {
    pub fn load(ctx: &ScreenCtx) -> Self {
        let rows: Vec<ScenarioRow> = lks_core::ops::op_list_scenarios(ctx.root)
            .unwrap_or_default()
            .iter()
            .map(|m| ScenarioRow {
                id: m
                    .get("id")
                    .and_then(Value::as_str)
                    .unwrap_or("?")
                    .to_string(),
                tags: m
                    .get("tags")
                    .and_then(Value::as_array)
                    .map(|a| {
                        a.iter()
                            .filter_map(Value::as_str)
                            .collect::<Vec<_>>()
                            .join(",")
                    })
                    .unwrap_or_default(),
                valid: m.get("valid").and_then(Value::as_bool).unwrap_or(false),
                error: m
                    .get("error")
                    .and_then(Value::as_str)
                    .map(|s| s.to_string()),
            })
            .collect();
        let mut state = ListState::default();
        if !rows.is_empty() {
            state.select(Some(0));
        }
        ScenariosScreen { items: rows, state }
    }

    pub fn render(&mut self, f: &mut Frame, area: Rect, _ctx: &ScreenCtx) {
        let items: Vec<ListItem> = self
            .items
            .iter()
            .map(|r| {
                let marker = if r.valid { "✓" } else { "✗" };
                let color = if r.valid {
                    Style::default().fg(ratatui::style::Color::Green)
                } else {
                    Style::default().fg(ratatui::style::Color::Red)
                };
                let mut line = Line::from(Span::styled(marker, color));
                line.spans.push(Span::styled(
                    format!(" {}", r.id),
                    Style::default().add_modifier(Modifier::BOLD),
                ));
                if !r.tags.is_empty() {
                    line.spans.push(Span::styled(
                        format!("  [{}]", r.tags),
                        Style::default().fg(ratatui::style::Color::DarkGray),
                    ));
                }
                if let Some(e) = &r.error {
                    line.spans.push(Span::styled(
                        format!("  {e}"),
                        Style::default().fg(ratatui::style::Color::Red),
                    ));
                }
                ListItem::new(line)
            })
            .collect();
        let hint = "↑↓ move · Enter detail · r refresh".to_string();
        f.render_stateful_widget(
            List::new(items)
                .block(widgets::title_block("Scenarios", Some(&hint)))
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
                    .select(Some(if n == 0 { 0 } else { (s + 1) % n }));
                NavAction::None
            }
            KeyCode::Char('k') | KeyCode::Up => {
                let s = self.state.selected().unwrap_or(0);
                self.state
                    .select(Some(if n == 0 { 0 } else { (s + n - 1) % n }));
                NavAction::None
            }
            KeyCode::Enter => match self.state.selected() {
                Some(i) if i < self.items.len() => {
                    let id = self.items[i].id.clone();
                    NavAction::Push(Box::new(Screen::ScenarioDetail(
                        ScenarioDetailScreen::load(ctx, &id),
                    )))
                }
                _ => NavAction::None,
            },
            KeyCode::Char('r') => {
                *self = Self::load(ctx);
                NavAction::None
            }
            _ => NavAction::None,
        }
    }
}
