//! `lksr tui` — interactive run browser (ratatui). A new surface for lksr
//! (Python lks has no TUI — only rich tables). Browse runs from
//! `.agent-sim/runs.sqlite`, pick one → view summary + turns + events.

use crossterm::event::{self, Event, KeyCode, KeyEventKind};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, List, ListItem, ListState, Paragraph, TableState};
use ratatui::Frame;

/// One run row in the browser.
#[derive(Clone)]
struct RunRow {
    run_id: String,
    scenario_id: String,
    status: String,
    duration_ms: Option<i64>,
    turn_count: Option<i64>,
    started_utc: String,
}

/// Load run rows from `.agent-sim/runs.sqlite` (newest first).
fn load_runs(sqlite_path: &std::path::Path) -> Vec<RunRow> {
    let mut out = Vec::new();
    let Ok(db) = rusqlite::Connection::open(sqlite_path) else {
        return out;
    };
    let cols = "run_id, scenario_id, status, duration_ms, turn_count, started_utc";
    let Ok(mut stmt) = db.prepare(&format!(
        "SELECT {cols} FROM runs ORDER BY started_utc DESC LIMIT 200"
    )) else {
        return out;
    };
    let Ok(rows) = stmt.query_map([], |row| {
        Ok(RunRow {
            run_id: row.get(0).unwrap_or_default(),
            scenario_id: row.get(1).unwrap_or_default(),
            status: row.get(2).unwrap_or_default(),
            duration_ms: row.get(3).ok(),
            turn_count: row.get(4).ok(),
            started_utc: row.get(5).unwrap_or_default(),
        })
    }) else {
        return out;
    };
    for r in rows.flatten() {
        out.push(r);
    }
    out
}

/// Read summary.json for a run (fallback to the raw text on parse failure).
fn load_summary(reports_dir: &std::path::Path, run_id: &str) -> serde_json::Value {
    let path = reports_dir.join(run_id).join("summary.json");
    std::fs::read_to_string(path)
        .ok()
        .and_then(|t| serde_json::from_str(&t).ok())
        .unwrap_or(serde_json::Value::Null)
}

/// TUI app state.
struct TuiApp {
    runs: Vec<RunRow>,
    list_state: ListState,
    detail_state: TableState,
    selected: Option<RunRow>,
    summary: serde_json::Value,
    detail_lines: Vec<Line<'static>>,
    show_detail: bool,
    reports_dir: std::path::PathBuf,
}

impl TuiApp {
    fn new(project_root: &std::path::Path) -> Self {
        let dot = project_root.join(".agent-sim");
        let sqlite_path = dot.join("runs.sqlite");
        let reports_dir = dot.join("reports");
        let runs = load_runs(&sqlite_path);
        let mut list_state = ListState::default();
        if !runs.is_empty() {
            list_state.select(Some(0));
        }
        Self {
            runs,
            list_state,
            detail_state: TableState::default(),
            selected: None,
            summary: serde_json::Value::Null,
            detail_lines: Vec::new(),
            show_detail: false,
            reports_dir,
        }
    }

    fn select_run(&mut self, idx: usize) {
        if idx >= self.runs.len() {
            return;
        }
        self.selected = Some(self.runs[idx].clone());
        self.summary = load_summary(&self.reports_dir, &self.runs[idx].run_id);
        self.detail_lines = build_detail_lines(&self.summary);
        self.detail_state.select(Some(0));
        self.show_detail = true;
    }

    fn back_to_list(&mut self) {
        self.show_detail = false;
        self.selected = None;
        self.summary = serde_json::Value::Null;
    }

    fn status_style(status: &str) -> Style {
        match status {
            "done" => Style::default().fg(Color::Green),
            "running" => Style::default().fg(Color::Yellow),
            _ => Style::default().fg(Color::Red),
        }
    }
}

/// Build the detail view lines from a summary.json.
fn build_detail_lines(summary: &serde_json::Value) -> Vec<Line<'static>> {
    let mut lines: Vec<Line<'static>> = Vec::new();
    let s = summary.as_object().cloned().unwrap_or_default();
    let run_id = s.get("run_id").and_then(|v| v.as_str()).unwrap_or("?");
    let status = s.get("status").and_then(|v| v.as_str()).unwrap_or("?");
    let dur = s.get("duration_ms").and_then(|v| v.as_i64()).unwrap_or(0);
    let turns = s.get("turn_count").and_then(|v| v.as_i64()).unwrap_or(0);
    let events = s.get("event_count").and_then(|v| v.as_i64()).unwrap_or(0);
    lines.push(Line::from(format!("run_id: {run_id}")));
    lines.push(Line::from(format!("status: {status}")));
    lines.push(Line::from(format!("duration_ms: {dur}")));
    lines.push(Line::from(format!("turn_count: {turns}")));
    lines.push(Line::from(format!("event_count: {events}")));
    // Verdict
    if let Some(v) = s.get("verdict").and_then(|v| v.as_object()) {
        let jv = v.get("verdict").and_then(|x| x.as_str()).unwrap_or("?");
        let score = v.get("score").and_then(|x| x.as_f64()).unwrap_or(0.0);
        lines.push(Line::from(format!("verdict: {jv} (score {score})")));
    }
    // Metrics digest
    if let Some(m) = s.get("metrics").and_then(|v| v.as_object()) {
        let ttfw = m
            .get("ttfw_ms")
            .map(|v| v.to_string())
            .unwrap_or("—".into());
        let barge = m
            .get("barge_count")
            .map(|v| v.to_string())
            .unwrap_or("—".into());
        lines.push(Line::from(format!(
            "metrics: ttfw_ms={ttfw} barge_count={barge}"
        )));
        if let Some(tt) = m.get("turn_taking_ms").and_then(|v| v.as_object()) {
            let p50 = tt.get("p50").map(|v| v.to_string()).unwrap_or("—".into());
            let p95 = tt.get("p95").map(|v| v.to_string()).unwrap_or("—".into());
            lines.push(Line::from(format!("turn_taking: p50={p50} p95={p95}")));
        }
    }
    // Top-level turns
    if let Some(turns) = s.get("turns").and_then(|v| v.as_array()) {
        lines.push(Line::from(""));
        lines.push(Line::from(format!("turns ({}):", turns.len())));
        for t in turns.iter().take(30) {
            let to = t.as_object().cloned().unwrap_or_default();
            let turn = to.get("turn").and_then(|v| v.as_i64()).unwrap_or(0);
            let tt = to
                .get("turn_taking_ms")
                .map(|v| v.to_string())
                .unwrap_or("—".into());
            let user = to
                .get("user_text")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .chars()
                .take(40)
                .collect::<String>();
            lines.push(Line::from(format!("  #{turn} ({tt}ms) user: {user}")));
        }
    }
    lines
}

/// Render one frame.
fn render(frame: &mut Frame, app: &mut TuiApp) {
    if app.show_detail {
        render_detail(frame, app);
    } else {
        render_list(frame, app);
    }
}

fn render_list(frame: &mut Frame, app: &mut TuiApp) {
    let area = frame.area();
    let title = if app.runs.is_empty() {
        " lksr runs — no runs yet (empty sqlite) ".to_string()
    } else {
        format!(
            " lksr runs ({}) — ↑↓ select · enter detail · q quit ",
            app.runs.len()
        )
    };
    let items: Vec<ListItem> = app
        .runs
        .iter()
        .map(|r| {
            let dur = r
                .duration_ms
                .map(|d| format!("{d}ms"))
                .unwrap_or("—".into());
            let turns = r.turn_count.map(|t| t.to_string()).unwrap_or("—".into());
            let style = TuiApp::status_style(&r.status);
            ListItem::new(Line::from(vec![
                Span::styled(
                    format!("{} ", r.run_id),
                    Style::default().add_modifier(Modifier::BOLD),
                ),
                Span::styled(format!("{} ", r.scenario_id), Style::default()),
                Span::styled(format!("{} ", r.status), style),
                Span::raw(format!("{dur} · {turns} turns · {}", r.started_utc)),
            ]))
        })
        .collect();
    let list = List::new(items)
        .block(Block::default().borders(Borders::ALL).title(title))
        .highlight_style(Style::default().fg(Color::Black).bg(Color::Cyan))
        .highlight_symbol("▶ ");
    frame.render_stateful_widget(list, area, &mut app.list_state);
}

fn render_detail(frame: &mut Frame, app: &mut TuiApp) {
    let area = frame.area();
    let run_id = app
        .selected
        .as_ref()
        .map(|r| r.run_id.clone())
        .unwrap_or_default();
    let title = format!(" lksr run {run_id} — ← back · q quit ");
    let para = Paragraph::new(app.detail_lines.clone())
        .block(Block::default().borders(Borders::ALL).title(title))
        .scroll((app.detail_state.selected().unwrap_or(0) as u16, 0));
    frame.render_widget(para, area);
}

/// Run the TUI loop (blocking; ctrl+c / q quits).
pub fn run(project_root: &std::path::Path) -> anyhow::Result<()> {
    let mut terminal = ratatui::init();
    let mut app = TuiApp::new(project_root);
    let result = loop {
        terminal.draw(|f| render(f, &mut app))?;
        match event::read()? {
            Event::Key(key) if key.kind == KeyEventKind::Press => match key.code {
                KeyCode::Char('q') | KeyCode::Char('Q') => break Ok(()),
                KeyCode::Esc => {
                    if app.show_detail {
                        app.back_to_list();
                    }
                }
                KeyCode::Enter => {
                    if let Some(i) = app.list_state.selected() {
                        app.select_run(i);
                    }
                }
                KeyCode::Backspace | KeyCode::Left => {
                    if app.show_detail {
                        app.back_to_list();
                    }
                }
                KeyCode::Down | KeyCode::Char('j') => {
                    if app.show_detail {
                        let n = app.detail_lines.len().saturating_sub(3);
                        let sel = app
                            .detail_state
                            .selected()
                            .unwrap_or(0)
                            .min(n.saturating_sub(1));
                        app.detail_state.select(Some(sel.saturating_add(1)));
                    } else {
                        let n = app.runs.len();
                        let i = app.list_state.selected().unwrap_or(0);
                        if n > 0 {
                            app.list_state.select(Some((i + 1) % n));
                        }
                    }
                }
                KeyCode::Up | KeyCode::Char('k') => {
                    if app.show_detail {
                        let sel = app.detail_state.selected().unwrap_or(0);
                        app.detail_state.select(Some(sel.saturating_sub(1)));
                    } else {
                        let n = app.runs.len();
                        let i = app.list_state.selected().unwrap_or(0);
                        if n > 0 {
                            app.list_state.select(Some((i + n - 1) % n));
                        }
                    }
                }
                _ => {}
            },
            Event::Resize(_, _) => {}
            _ => {}
        }
    };
    ratatui::restore();
    result
}
