//! Preflight tab: run config/connectivity checks.

use ratatui::layout::Rect;
use ratatui::text::{Line, Span};
use ratatui::widgets::Paragraph;
use ratatui::Frame;

use serde_json::Value;

use crate::tui::screen::{NavAction, ScreenCtx};
use crate::tui::widgets;

#[derive(Debug)]
pub struct PreflightScreen {
    lines: Vec<Line<'static>>,
}

impl PreflightScreen {
    pub fn load(ctx: &ScreenCtx) -> Self {
        let mut lines = Vec::new();
        lines.push(Line::from(Span::styled(
            "Preflight",
            ratatui::style::Style::default().add_modifier(ratatui::style::Modifier::BOLD),
        )));
        match lks_core::ops::op_preflight_core(ctx.root, None) {
            Ok((map, _cfg)) => {
                let ok = map.get("ok").and_then(Value::as_bool).unwrap_or(false);
                lines.push(Line::from(format!(
                    "overall: {}",
                    if ok { "✓" } else { "✗" }
                )));
                if let Some(checks) = map.get("checks").and_then(Value::as_array) {
                    for c in checks {
                        let name = c.get("name").and_then(Value::as_str).unwrap_or("?");
                        let status = c.get("status").and_then(Value::as_str).unwrap_or("?");
                        let detail = c.get("detail").and_then(Value::as_str).unwrap_or("");
                        let marker = match status {
                            "pass" => "✓",
                            "fail" => "✗",
                            _ => "⚠",
                        };
                        lines.push(Line::from(format!("  {marker} {name}: {detail}")));
                    }
                }
            }
            Err(e) => lines.push(Line::from(format!("preflight error: {e}"))),
        }
        lines.push(Line::from(""));
        lines.push(Line::from(Span::styled(
            "c  full connectivity check · r  re-run",
            ratatui::style::Style::default().fg(ratatui::style::Color::DarkGray),
        )));
        PreflightScreen { lines }
    }

    pub fn render(&mut self, f: &mut Frame, area: Rect, _ctx: &ScreenCtx) {
        f.render_widget(
            Paragraph::new(self.lines.clone()).block(widgets::title_block("Preflight", None)),
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
            KeyCode::Char('c') => {
                let root = ctx.root.to_path_buf();
                let mut new_lines = vec![Line::from("connectivity check…")];
                let result = std::thread::spawn(move || {
                    let rt = tokio::runtime::Builder::new_multi_thread()
                        .enable_all()
                        .build()
                        .map_err(|e| e.to_string())?;
                    rt.block_on(lks_livekit::preflight::op_preflight(&root, true, None))
                        .map_err(|e| e.to_string())
                })
                .join()
                .unwrap_or(Err("thread panicked".into()));
                match result {
                    Ok(map) => {
                        let ok = map.get("ok").and_then(Value::as_bool).unwrap_or(false);
                        new_lines.push(Line::from(format!(
                            "overall: {}",
                            if ok { "✓" } else { "✗" }
                        )));
                        if let Some(checks) = map.get("checks").and_then(Value::as_array) {
                            for c in checks {
                                let name = c.get("name").and_then(Value::as_str).unwrap_or("?");
                                let status = c.get("status").and_then(Value::as_str).unwrap_or("?");
                                let detail = c.get("detail").and_then(Value::as_str).unwrap_or("");
                                let marker = match status {
                                    "pass" => "✓",
                                    "fail" => "✗",
                                    _ => "⚠",
                                };
                                new_lines.push(Line::from(format!("  {marker} {name}: {detail}")));
                            }
                        }
                    }
                    Err(e) => new_lines.push(Line::from(format!("connectivity error: {e}"))),
                }
                new_lines.push(Line::from(""));
                new_lines.push(Line::from(Span::styled(
                    "c  full connectivity check · r  re-run",
                    ratatui::style::Style::default().fg(ratatui::style::Color::DarkGray),
                )));
                self.lines = new_lines;
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
