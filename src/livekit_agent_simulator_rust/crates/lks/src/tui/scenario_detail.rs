//! Scenario detail: persona + execute + dispatch + pass criteria.

use ratatui::layout::Rect;
use ratatui::style::Style;
use ratatui::text::{Line, Span};
use ratatui::widgets::Paragraph;
use ratatui::Frame;

use serde_json::Value;

use crate::tui::screen::{NavAction, Screen, ScreenCtx};
use crate::tui::widgets;

#[derive(Debug)]
pub struct ScenarioDetailScreen {
    scenario_id: String,
    lines: Vec<Line<'static>>,
}

fn val_str(v: Option<&Value>) -> String {
    match v {
        Some(Value::String(s)) => s.clone(),
        Some(Value::Number(n)) => n.to_string(),
        Some(Value::Bool(b)) => b.to_string(),
        Some(Value::Array(a)) => a
            .iter()
            .filter_map(|x| x.as_str())
            .collect::<Vec<_>>()
            .join(", "),
        _ => String::new(),
    }
}

impl ScenarioDetailScreen {
    pub fn load(ctx: &ScreenCtx, scenario_id: &str) -> Self {
        let mut lines = Vec::new();
        lines.push(Line::from(Span::styled(
            format!("Scenario: {scenario_id}"),
            Style::default().add_modifier(ratatui::style::Modifier::BOLD),
        )));

        let validate = lks_core::ops::op_validate_scenario(ctx.root, scenario_id);
        match validate {
            Ok(v) => {
                let valid = v.get("valid").and_then(Value::as_bool).unwrap_or(false);
                lines.push(Line::from(format!(
                    "valid: {}",
                    if valid { "✓" } else { "✗" }
                )));
                if let Some(errs) = v.get("errors").and_then(Value::as_array) {
                    for e in errs {
                        lines.push(Line::from(format!("  error: {e}")));
                    }
                }
                if let Some(ws) = v.get("warnings").and_then(Value::as_array) {
                    for w in ws {
                        lines.push(Line::from(format!("  warn:  {w}")));
                    }
                }
            }
            Err(e) => lines.push(Line::from(format!("validate error: {e}"))),
        }

        lines.push(Line::from(""));
        if let Ok(exported) = lks_core::ops::op_export_scenario(ctx.root, scenario_id) {
            if let Some(persona) = exported.get("persona").and_then(Value::as_object) {
                lines.push(Line::from(Span::styled(
                    "Persona",
                    Style::default().add_modifier(ratatui::style::Modifier::BOLD),
                )));
                lines.push(Line::from(format!(
                    "  name:  {}",
                    val_str(persona.get("name"))
                )));
                if let Some(b) = persona.get("brief") {
                    lines.push(Line::from(format!("  brief: {}", val_str(Some(b)))));
                }
                if let Some(g) = persona.get("goals").and_then(Value::as_array) {
                    lines.push(Line::from("  goals:"));
                    for x in g {
                        lines.push(Line::from(format!("    - {x}")));
                    }
                }
                lines.push(Line::from(""));
            }
            if let Some(ex) = exported.get("execute").and_then(Value::as_object) {
                lines.push(Line::from(Span::styled(
                    "Execute",
                    Style::default().add_modifier(ratatui::style::Modifier::BOLD),
                )));
                lines.push(Line::from(format!(
                    "  max_turns:  {}",
                    val_str(ex.get("max_turns"))
                )));
                lines.push(Line::from(format!(
                    "  timeout_s:  {}",
                    val_str(ex.get("timeout_s"))
                )));
                lines.push(Line::from(format!(
                    "  first_speaker: {}",
                    val_str(ex.get("first_speaker"))
                )));
                lines.push(Line::from(""));
            }
            if let Some(pc) = exported.get("pass_criteria").and_then(Value::as_object) {
                lines.push(Line::from(Span::styled(
                    "Pass criteria",
                    Style::default().add_modifier(ratatui::style::Modifier::BOLD),
                )));
                if let Some(c) = pc.get("criteria").and_then(Value::as_array) {
                    for x in c {
                        lines.push(Line::from(format!("  - {x}")));
                    }
                }
            }
        } else {
            lines.push(Line::from("(no exportable scenario)"));
        }

        lines.push(Line::from(""));
        lines.push(Line::from(Span::styled(
            "r  run · v  validate · Esc back",
            Style::default().fg(ratatui::style::Color::DarkGray),
        )));

        ScenarioDetailScreen {
            scenario_id: scenario_id.to_string(),
            lines,
        }
    }

    pub fn render(&mut self, f: &mut Frame, area: Rect, _ctx: &ScreenCtx) {
        f.render_widget(
            Paragraph::new(self.lines.clone()).block(widgets::title_block("Scenario detail", None)),
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
            KeyCode::Char('r') | KeyCode::Enter => {
                let id = self.scenario_id.clone();
                NavAction::Push(Box::new(Screen::RunSetup(
                    crate::tui::run_setup::RunSetupScreen::new(ctx, &id),
                )))
            }
            KeyCode::Char('v') => {
                *self = Self::load(ctx, &self.scenario_id);
                NavAction::None
            }
            _ => NavAction::None,
        }
    }
}
