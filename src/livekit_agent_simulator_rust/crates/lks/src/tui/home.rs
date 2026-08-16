//! Home / dashboard tab: project overview + quick counts.

use ratatui::layout::{Constraint, Direction, Layout, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Paragraph};
use ratatui::Frame;

use crate::tui::screen::{NavAction, ScreenCtx};
use crate::tui::widgets;

#[derive(Debug, Default)]
pub struct HomeScreen {}

impl HomeScreen {
    pub fn render(&mut self, f: &mut Frame, area: Rect, ctx: &ScreenCtx) {
        let cols = Layout::default()
            .direction(Direction::Horizontal)
            .constraints([Constraint::Percentage(50), Constraint::Percentage(50)])
            .split(area);

        let scenario_count = ctx.root.join(".agent-sim/scenarios").is_dir().then(|| {
            std::fs::read_dir(ctx.root.join(".agent-sim/scenarios"))
                .map(|d| d.count())
                .unwrap_or(0)
        });
        let runs = ctx.cfg.reports_dir();
        let run_count = std::fs::read_dir(&runs).map(|d| d.count()).unwrap_or(0);

        let mut lines = vec![
            Line::from(Span::styled(
                "livekit-agent-simulator — lksr",
                Style::default().add_modifier(Modifier::BOLD),
            )),
            Line::from(""),
            Line::from(Span::styled(
                format!("Project root: {}", ctx.root.display()),
                Style::default().fg(Color::DarkGray),
            )),
            Line::from(format!("LiveKit URL:   {}", ctx.cfg.livekit.url)),
            Line::from(format!("Agent name:    {}", ctx.cfg.livekit.agent_name)),
            Line::from(format!("Provider:      {}", ctx.cfg.simulator.provider)),
            Line::from(""),
            Line::from(Span::styled(
                format!("Scenarios:   {}", scenario_count.unwrap_or(0)),
                widgets::status_style("scenarios"),
            )),
            Line::from(Span::styled(
                format!("Runs:        {run_count}"),
                widgets::status_style("runs"),
            )),
        ];
        lines.push(Line::from(""));
        lines.push(Line::from("Keys:"));
        lines.push(Line::from(
            "  1-6  switch tab · Enter on a list opens detail",
        ));
        lines.push(Line::from("  r    refresh lists · ?  help · q  quit"));

        f.render_widget(
            Paragraph::new(lines).block(Block::default().borders(Borders::ALL).title("Home")),
            cols[0],
        );

        let _ = cols[1];
    }

    pub fn on_key(
        &mut self,
        _key: ratatui::crossterm::event::KeyEvent,
        _ctx: &ScreenCtx,
    ) -> NavAction {
        NavAction::None
    }
}
