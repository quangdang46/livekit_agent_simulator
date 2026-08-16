//! Runs tab: browse run history → open report.

use ratatui::layout::Rect;
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{List, ListItem, ListState};
use ratatui::Frame;

use serde_json::Value;

use crate::tui::screen::{NavAction, Screen, ScreenCtx};
use crate::tui::widgets;

#[derive(Debug)]
pub struct RunsScreen {
    items: Vec<RunRow>,
    state: ListState,
}

#[derive(Debug, Clone)]
struct RunRow {
    run_id: String,
    scenario_id: String,
    status: String,
}

impl RunsScreen {
    pub fn load(ctx: &ScreenCtx) -> Self {
        let items: Vec<RunRow> = lks_core::ops::op_list_runs(ctx.root, 50, None)
            .unwrap_or_default()
            .iter()
            .map(|m| RunRow {
                run_id: m
                    .get("run_id")
                    .and_then(Value::as_str)
                    .unwrap_or("?")
                    .to_string(),
                scenario_id: m
                    .get("scenario_id")
                    .and_then(Value::as_str)
                    .unwrap_or("?")
                    .to_string(),
                status: m
                    .get("status")
                    .and_then(Value::as_str)
                    .unwrap_or("?")
                    .to_string(),
            })
            .collect();
        let mut state = ListState::default();
        if !items.is_empty() {
            state.select(Some(0));
        }
        RunsScreen { items, state }
    }

    pub fn render(&mut self, f: &mut Frame, area: Rect, _ctx: &ScreenCtx) {
        let items: Vec<ListItem> = self
            .items
            .iter()
            .map(|r| {
                let mut line = Line::from(Span::styled(
                    r.status.clone(),
                    widgets::status_style(&r.status),
                ));
                line.spans.push(Span::styled(
                    format!(" {}  ", r.run_id),
                    Style::default().add_modifier(Modifier::BOLD),
                ));
                line.spans.push(Span::styled(
                    format!("{}", r.scenario_id),
                    Style::default().fg(ratatui::style::Color::DarkGray),
                ));
                ListItem::new(line)
            })
            .collect();
        f.render_stateful_widget(
            List::new(items)
                .block(widgets::title_block(
                    "Runs",
                    Some("↑↓ move · Enter report · r refresh"),
                ))
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
                    let rid = self.items[i].run_id.clone();
                    NavAction::Push(Box::new(Screen::RunDetail(
                        crate::tui::run_detail::RunDetailScreen::load(ctx, &rid),
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
