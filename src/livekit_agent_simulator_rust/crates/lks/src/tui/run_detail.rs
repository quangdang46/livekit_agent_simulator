//! Run detail: summary + metrics + verdict + turns.

use ratatui::layout::Rect;
use ratatui::style::Style;
use ratatui::text::{Line, Span};
use ratatui::widgets::Paragraph;
use ratatui::Frame;

use serde_json::Value;

use crate::tui::screen::{NavAction, Screen, ScreenCtx};
use crate::tui::widgets;

#[derive(Debug)]
pub struct RunDetailScreen {
    run_id: String,
    lines: Vec<Line<'static>>,
}

fn f(v: Option<&Value>) -> String {
    match v {
        Some(Value::Number(n)) => n.to_string(),
        Some(Value::String(s)) => s.clone(),
        Some(Value::Bool(b)) => b.to_string(),
        _ => String::new(),
    }
}

impl RunDetailScreen {
    pub fn load(ctx: &ScreenCtx, run_id: &str) -> Self {
        let mut lines = Vec::new();
        lines.push(Line::from(Span::styled(
            format!("Run: {run_id}"),
            Style::default().add_modifier(ratatui::style::Modifier::BOLD),
        )));

        match lks_core::ops::op_get_run_report(ctx.root, run_id) {
            Ok(report) => {
                if let Some(s) = report.get("summary").and_then(Value::as_object) {
                    lines.push(Line::from(format!("status: {}", f(s.get("status")))));
                    lines.push(Line::from(format!(
                        "duration_ms: {}",
                        f(s.get("duration_ms"))
                    )));
                    lines.push(Line::from(format!(
                        "turn_count: {}",
                        f(s.get("turn_count"))
                    )));
                    if let Some(ec) = s.get("end_reason").and_then(Value::as_str) {
                        lines.push(Line::from(format!("end_reason: {ec}")));
                    }
                    if let Some(verdict) = s.get("verdict").and_then(Value::as_object) {
                        lines.push(Line::from(""));
                        lines.push(Line::from(Span::styled(
                            "Verdict",
                            Style::default().add_modifier(ratatui::style::Modifier::BOLD),
                        )));
                        lines.push(Line::from(format!(
                            "  verdict: {}",
                            f(verdict.get("verdict"))
                        )));
                        if let Some(os) = verdict.get("overall_summary").and_then(Value::as_str) {
                            lines.push(Line::from(format!("  {os}")));
                        }
                    }
                    lines.push(Line::from(""));
                    if let Some(tt) = s.get("metrics").and_then(Value::as_object) {
                        lines.push(Line::from(Span::styled(
                            "Metrics",
                            Style::default().add_modifier(ratatui::style::Modifier::BOLD),
                        )));
                        for k in [
                            "ttfw_ms",
                            "agent_finals",
                            "user_finals",
                            "barge_count",
                            "interruption_count",
                        ] {
                            if let Some(v) = tt.get(k) {
                                lines.push(Line::from(format!("  {k}: {v}")));
                            }
                        }
                    }
                }
                if let Some(turns) = report.get("turns").and_then(Value::as_array) {
                    lines.push(Line::from(""));
                    lines.push(Line::from(Span::styled(
                        "Turns",
                        Style::default().add_modifier(ratatui::style::Modifier::BOLD),
                    )));
                    for t in turns.iter().take(30) {
                        let turn = f(t.get("turn"));
                        let user = widgets::truncate(&f(t.get("user_text")), 40);
                        let agent = widgets::truncate(&f(t.get("agent_text")), 40);
                        lines.push(Line::from(format!("  #{turn} U: {user}")));
                        lines.push(Line::from(format!("      A: {agent}")));
                    }
                }
            }
            Err(e) => lines.push(Line::from(format!("report error: {e}"))),
        }

        lines.push(Line::from(""));
        lines.push(Line::from(Span::styled(
            "l  log · Esc back",
            Style::default().fg(ratatui::style::Color::DarkGray),
        )));

        RunDetailScreen {
            run_id: run_id.to_string(),
            lines,
        }
    }

    pub fn render(&mut self, f: &mut Frame, area: Rect, _ctx: &ScreenCtx) {
        f.render_widget(
            Paragraph::new(self.lines.clone()).block(widgets::title_block("Run report", None)),
            area,
        );
    }

    pub fn on_key(
        &mut self,
        key: ratatui::crossterm::event::KeyEvent,
        ctx: &ScreenCtx,
    ) -> NavAction {
        use ratatui::crossterm::event::KeyCode;
        match key.code {
            KeyCode::Char('l') => {
                let rid = self.run_id.clone();
                NavAction::Push(Box::new(Screen::Log(crate::tui::log::LogScreen::load(
                    ctx, &rid,
                ))))
            }
            _ => NavAction::None,
        }
    }
}
