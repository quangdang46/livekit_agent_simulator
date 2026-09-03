//! Shared render helpers for the lksr TUI.

use ratatui::style::{Color, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders};

/// Status color by run/scenario state.
pub fn status_style(status: &str) -> Style {
    match status.to_lowercase().as_str() {
        "done" | "pass" | "ok" | "passed" | "success" => Style::default().fg(Color::Green),
        "running" | "pending" | "in_progress" => Style::default().fg(Color::Yellow),
        "fail" | "failed" | "error" => Style::default().fg(Color::Red),
        "valid" => Style::default().fg(Color::Green),
        _ => Style::default().fg(Color::DarkGray),
    }
}

/// A bordered block with a title line (title + optional trailing hint).
pub fn title_block<'a>(title: &'a str, hint: Option<&'a str>) -> Block<'a> {
    let mut block = Block::default().borders(Borders::ALL).title(title);
    if let Some(h) = hint {
        block = block.title_bottom(Line::from(Span::styled(
            h,
            Style::default().fg(Color::DarkGray),
        )));
    }
    block
}

/// Truncate to at most `n` chars, appending ellipsis.
pub fn truncate(s: &str, n: usize) -> String {
    if s.chars().count() <= n {
        s.to_string()
    } else {
        let mut out: String = s.chars().take(n.saturating_sub(1)).collect();
        out.push('…');
        out
    }
}
