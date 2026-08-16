//! Run setup: pick repeat/pass@k/profile/agent → launch a live run.

use ratatui::layout::{Constraint, Direction, Layout, Rect};
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Paragraph};
use ratatui::Frame;

use crate::tui::live_run::RunSettings;
use crate::tui::screen::{NavAction, Screen, ScreenCtx};

#[derive(Debug)]
pub struct RunSetupScreen {
    scenario_id: String,
    pub run_name: String,
    pub repeat: String,
    pub pass_at_k: String,
    pub profile: String,
    pub agent_name: String,
    pub strict_judge: bool,
    focus: usize, // 0=name 1=repeat 2=pass_at_k 3=profile 4=agent 5=strict 6=run
}

impl RunSetupScreen {
    pub fn new(ctx: &ScreenCtx, scenario_id: &str) -> Self {
        let active = ctx.cfg.active_profile.clone().unwrap_or_default();
        RunSetupScreen {
            scenario_id: scenario_id.to_string(),
            run_name: String::new(),
            repeat: "1".into(),
            pass_at_k: String::new(),
            profile: active,
            agent_name: String::new(),
            strict_judge: false,
            focus: 1,
        }
    }

    fn fields(&self) -> Vec<(&str, &str)> {
        vec![
            ("run name", &self.run_name),
            ("repeat", &self.repeat),
            ("pass at k", &self.pass_at_k),
            ("profile", &self.profile),
            ("agent name", &self.agent_name),
        ]
    }

    fn settings(&self) -> RunSettings {
        RunSettings {
            run_name: if self.run_name.trim().is_empty() {
                None
            } else {
                Some(self.run_name.trim().to_string())
            },
            repeat: self.repeat.trim().parse().unwrap_or(1),
            pass_at_k: if self.pass_at_k.trim().is_empty() {
                None
            } else {
                self.pass_at_k.trim().parse().ok()
            },
            agent_name: if self.agent_name.trim().is_empty() {
                None
            } else {
                Some(self.agent_name.trim().to_string())
            },
            profile: if self.profile.trim().is_empty() {
                None
            } else {
                Some(self.profile.trim().to_string())
            },
            strict_judge: self.strict_judge,
        }
    }

    pub fn render(&mut self, f: &mut Frame, area: Rect, _ctx: &ScreenCtx) {
        let chunk = Layout::default()
            .direction(Direction::Vertical)
            .constraints([
                Constraint::Length(1),
                Constraint::Length(1),
                Constraint::Length(5),
                Constraint::Length(1),
                Constraint::Length(1),
                Constraint::Length(1),
                Constraint::Length(1),
                Constraint::Length(1),
                Constraint::Length(1),
            ])
            .split(area);

        let mut lines = vec![Line::from(format!("Run: {}", self.scenario_id))];
        lines.push(Line::from(""));
        for (i, (label, val)) in self.fields().iter().enumerate() {
            let selected = i == self.focus;
            let mut spans = vec![
                Span::styled(
                    format!(" {label}: "),
                    if selected {
                        Style::default().add_modifier(Modifier::REVERSED)
                    } else {
                        Style::default()
                    },
                ),
                Span::raw(*val),
            ];
            if selected {
                spans.push(Span::styled(
                    " ▌",
                    Style::default().fg(ratatui::style::Color::Cyan),
                ));
            }
            lines.push(Line::from(spans));
        }
        // strict toggle
        let sel = 5 == self.focus;
        lines.push(Line::from(vec![
            Span::styled(
                " strict judge: ",
                if sel {
                    Style::default().add_modifier(Modifier::REVERSED)
                } else {
                    Style::default()
                },
            ),
            Span::raw(if self.strict_judge { "ON" } else { "OFF" }),
        ]));
        // run button
        let sel = 6 == self.focus;
        lines.push(Line::from(Span::styled(
            " ▶ Run",
            if sel {
                Style::default().add_modifier(Modifier::REVERSED)
            } else {
                Style::default().fg(ratatui::style::Color::Cyan)
            },
        )));

        f.render_widget(
            Paragraph::new(lines).block(Block::default().borders(Borders::ALL).title("Run setup")),
            chunk[0].union(area),
        );
        let _ = chunk;
    }

    pub fn on_key(
        &mut self,
        key: ratatui::crossterm::event::KeyEvent,
        ctx: &ScreenCtx,
    ) -> NavAction {
        use ratatui::crossterm::event::KeyCode;
        match key.code {
            KeyCode::Char('j') | KeyCode::Down | KeyCode::Tab => {
                self.focus = (self.focus + 1).min(6);
                NavAction::None
            }
            KeyCode::Char('k') | KeyCode::Up | KeyCode::BackTab => {
                self.focus = self.focus.saturating_sub(1);
                NavAction::None
            }
            KeyCode::Enter => {
                if self.focus == 6 {
                    let id = self.scenario_id.clone();
                    let settings = self.settings();
                    match crate::tui::live_run::RunSession::start(
                        ctx.root.to_path_buf(),
                        id.clone(),
                        settings.clone(),
                    ) {
                        Ok(session) => {
                            let strict = settings.strict_judge;
                            NavAction::Push(Box::new(Screen::LiveRun(
                                crate::tui::live_run::LiveRunScreen::new(session, id, strict),
                            )))
                        }
                        Err(e) => NavAction::Toast(format!("run start failed: {e}")),
                    }
                } else {
                    NavAction::None
                }
            }
            KeyCode::Char(c) => {
                match self.focus {
                    0 => self.run_name.push(c),
                    1 => self.repeat.push(c),
                    2 => self.pass_at_k.push(c),
                    3 => self.profile.push(c),
                    4 => self.agent_name.push(c),
                    5 => {
                        if c == ' ' || c == 't' {
                            self.strict_judge = !self.strict_judge;
                        }
                    }
                    _ => {}
                }
                NavAction::None
            }
            KeyCode::Backspace => {
                match self.focus {
                    0 => {
                        self.run_name.pop();
                    }
                    1 => {
                        self.repeat.pop();
                    }
                    2 => {
                        self.pass_at_k.pop();
                    }
                    3 => {
                        self.profile.pop();
                    }
                    4 => {
                        self.agent_name.pop();
                    }
                    _ => {}
                }
                NavAction::None
            }
            _ => NavAction::None,
        }
    }
}
