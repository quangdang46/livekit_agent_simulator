//! Live scenario run engine — spawns `execute_scenario` on a dedicated thread
//! with its own tokio runtime, streams events back over an mpsc channel, and
//! supports a graceful abort. This is the testable core the LiveRun screen
//! drives; terminal rendering stays a thin layer on top.
//!
//! Some accessors (root/report_dir/scenario_id/RunFn) are engine API covered
//! by the unit tests below but not yet consumed by a screen — keep them for
//! the public engine surface.
#![allow(dead_code)]

use std::path::{Path, PathBuf};
use std::sync::mpsc::Receiver;
use std::time::Instant;

use serde_json::{Map, Value};

/// Runtime settings for one live run.
#[derive(Debug, Clone, Default)]
pub struct RunSettings {
    pub run_name: Option<String>,
    pub repeat: i64,
    pub pass_at_k: Option<i64>,
    pub agent_name: Option<String>,
    pub profile: Option<String>,
    pub strict_judge: bool,
}

impl RunSettings {
    pub fn single() -> Self {
        Self {
            repeat: 1,
            ..Default::default()
        }
    }
}

/// The actual runner — injectable for tests.
pub type RunFn = Box<
    dyn FnOnce(
            PathBuf,
            String,
            lks_livekit::run::ExecuteOptions,
        ) -> Result<Map<String, Value>, String>
        + Send,
>;

/// Poll outcome from [`RunSession::poll`].
#[derive(Debug, PartialEq)]
pub enum RunPoll {
    /// Zero or more new events since the last poll.
    Events(Vec<Map<String, Value>>),
    /// The run finished (Ok = result envelope, Err = runner error).
    Finished(Result<Map<String, Value>, String>),
    /// Still running.
    Running,
}

impl std::fmt::Debug for RunSession {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("RunSession")
            .field("scenario_id", &self.scenario_id)
            .field("events", &self.events.len())
            .field("result", &self.result.as_ref().map(|r| r.is_ok()))
            .finish()
    }
}

/// A live scenario run: thread + channel + abort handle.
pub struct RunSession {
    root: PathBuf,
    scenario_id: String,
    rx: Receiver<Map<String, Value>>,
    result_rx: Receiver<Result<Map<String, Value>, String>>,
    abort_tx: tokio::sync::watch::Sender<bool>,
    handle: Option<std::thread::JoinHandle<()>>,
    /// Accumulated live events (all sources).
    pub events: Vec<Map<String, Value>>,
    /// Finished result, if any.
    pub result: Option<Result<Map<String, Value>, String>>,
    started: Instant,
}

impl RunSession {
    /// Start a live run of `scenario_id` with the given settings, using the
    /// default runner (`execute_scenario` on its own tokio runtime).
    pub fn start(
        root: PathBuf,
        scenario_id: String,
        settings: RunSettings,
    ) -> Result<Self, String> {
        Self::start_with(root, scenario_id, settings, |root, sid, opts| {
            let rt = tokio::runtime::Builder::new_multi_thread()
                .enable_all()
                .build()
                .map_err(|e| e.to_string())?;
            rt.block_on(lks_livekit::run::execute_scenario(&root, &sid, &opts))
                .map_err(|e| e.to_string())
        })
    }

    /// Start with an injected runner (tests). The runner receives the root,
    /// scenario id, and fully-wired options (live channel + abort receiver).
    pub fn start_with<F>(
        root: PathBuf,
        scenario_id: String,
        settings: RunSettings,
        run: F,
    ) -> Result<Self, String>
    where
        F: FnOnce(
                PathBuf,
                String,
                lks_livekit::run::ExecuteOptions,
            ) -> Result<Map<String, Value>, String>
            + Send
            + 'static,
    {
        let (tx, rx) = std::sync::mpsc::channel::<Map<String, Value>>();
        let (result_tx, result_rx) = std::sync::mpsc::channel();
        let (abort_tx, abort_rx) = tokio::sync::watch::channel(false);

        let opts = lks_livekit::run::ExecuteOptions {
            run_name: settings.run_name.clone(),
            repeat: settings.repeat,
            pass_at_k: settings.pass_at_k,
            agent_name: settings.agent_name.clone(),
            optimized: None,
            profile: settings.profile.clone(),
            environment: None,
            live: Some(tx),
            abort_rx: Some(abort_rx),
        };

        let root2 = root.clone();
        let sid2 = scenario_id.clone();
        let handle = std::thread::spawn(move || {
            // The runner owns opts — dropping it at thread end closes the
            // live channel, which the poller uses to detect completion.
            let out = run(root2, sid2, opts);
            let _ = result_tx.send(out);
        });

        Ok(RunSession {
            root,
            scenario_id,
            rx,
            result_rx,
            abort_tx,
            handle: Some(handle),
            events: Vec::new(),
            result: None,
            started: Instant::now(),
        })
    }

    /// Drain new events / completion. Call frequently (each TUI tick).
    pub fn poll(&mut self) -> RunPoll {
        // Drain events first (so a Finished that also has pending events never
        // loses them), then surface completion.
        let mut new = Vec::new();
        while let Ok(ev) = self.rx.try_recv() {
            self.events.push(ev.clone());
            new.push(ev);
        }
        if let Ok(out) = self.result_rx.try_recv() {
            self.result = Some(out.clone());
            // Also flush any events that raced in alongside the result.
            while let Ok(ev) = self.rx.try_recv() {
                self.events.push(ev.clone());
                new.push(ev);
            }
            return RunPoll::Finished(out);
        }
        if !new.is_empty() {
            return RunPoll::Events(new);
        }
        // Thread exited without a result.
        if let Some(h) = &self.handle {
            if h.is_finished() && self.result.is_none() {
                let out = Err("run thread exited without a result".to_string());
                self.result = Some(out.clone());
                return RunPoll::Finished(out);
            }
        }
        RunPoll::Running
    }

    /// Signal a graceful abort (fired through the run's end signal).
    pub fn abort(&self) {
        let _ = self.abort_tx.send(true);
    }

    pub fn is_running(&self) -> bool {
        self.result.is_none()
    }

    pub fn run_id(&self) -> Option<&str> {
        for e in self.events.iter().rev() {
            if let Some(rid) = e.get("run_id").and_then(|v| v.as_str()) {
                return Some(rid);
            }
        }
        None
    }

    /// Report dir once run_id is known (from the first run.started envelope).
    pub fn report_dir(&self, reports_dir: &Path) -> Option<PathBuf> {
        self.run_id().map(|rid| reports_dir.join(rid))
    }

    pub fn elapsed(&self) -> std::time::Duration {
        self.started.elapsed()
    }

    /// Live 36-key metrics over accumulated events (safe on partial runs).
    pub fn metrics(&self) -> Map<String, Value> {
        lks_core::metrics::compute_voice_metrics(&self.events)
    }

    /// 13-key flat digest of the live metrics.
    pub fn digest(&self) -> Map<String, Value> {
        lks_core::ops::metrics_digest(Some(&self.metrics()))
    }

    /// Current turn from the newest event that carries one.
    pub fn current_turn(&self) -> i64 {
        self.events
            .iter()
            .rev()
            .find_map(|e| e.get("turn").and_then(|v| v.as_i64()))
            .unwrap_or(0)
    }

    pub fn scenario_id(&self) -> &str {
        &self.scenario_id
    }

    pub fn root(&self) -> &Path {
        &self.root
    }
}

/// Compact event line for the activity stream (reuses the report's exact
/// describe format).
pub fn describe_event(e: &Map<String, Value>) -> String {
    lks_core::logging::event::describe(e)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fake_envelope(kind: &str, run_id: &str, turn: i64) -> Map<String, Value> {
        let mut e = Map::new();
        e.insert("kind".into(), serde_json::json!(kind));
        e.insert("run_id".into(), serde_json::json!(run_id));
        e.insert("turn".into(), serde_json::json!(turn));
        e
    }

    /// An injected runner that emits two events then returns an Ok envelope.
    fn ok_runner(
        live: std::sync::mpsc::Sender<Map<String, Value>>,
    ) -> impl FnOnce(
        PathBuf,
        String,
        lks_livekit::run::ExecuteOptions,
    ) -> Result<Map<String, Value>, String> {
        move |_root, _sid, opts| {
            let tx = opts.live.expect("live channel wired");
            let _ = live;
            tx.send(fake_envelope(
                "run.started",
                "001-fake-20260101-000000-abcd",
                0,
            ))
            .unwrap();
            tx.send(fake_envelope(
                "transcript.agent.final",
                "001-fake-20260101-000000-abcd",
                1,
            ))
            .unwrap();
            let mut out = Map::new();
            out.insert("status".into(), serde_json::json!("done"));
            out.insert(
                "run_id".into(),
                serde_json::json!("001-fake-20260101-000000-abcd"),
            );
            Ok(out)
        }
    }

    #[test]
    fn poll_streams_events_then_finishes() {
        let (live_tx, _keep) = std::sync::mpsc::channel();
        let mut s = RunSession::start_with(
            PathBuf::from("/tmp"),
            "fake".to_string(),
            RunSettings::single(),
            ok_runner(live_tx),
        )
        .unwrap();

        // Keep polling until Finished — events may arrive with or before the
        // result, so assert on the session's accumulated events, not the count
        // of Events-returned polls.
        let mut finished = false;
        for _ in 0..1000 {
            match s.poll() {
                RunPoll::Events(_) => {}
                RunPoll::Running => std::thread::sleep(std::time::Duration::from_millis(1)),
                RunPoll::Finished(out) => {
                    assert!(out.is_ok());
                    finished = true;
                    break;
                }
            }
        }
        assert!(finished, "run never finished");
        assert!(
            s.events.len() >= 2,
            "expected >=2 events, got {}",
            s.events.len()
        );
        assert_eq!(s.run_id(), Some("001-fake-20260101-000000-abcd"));
        assert!(!s.is_running());
    }

    #[test]
    fn metrics_and_digest_are_safe_on_partial_events() {
        let (live_tx, _keep) = std::sync::mpsc::channel();
        let mut s = RunSession::start_with(
            PathBuf::from("/tmp"),
            "fake".to_string(),
            RunSettings::single(),
            ok_runner(live_tx),
        )
        .unwrap();
        // Drain until finished.
        while s.is_running() {
            let _ = s.poll();
        }
        let _ = s.poll();
        // Agent final present → metrics not empty; digest has ttfw etc.
        let m = s.metrics();
        assert!(!m.is_empty());
        let d = s.digest();
        assert!(d.contains_key("ttfw_ms"));
    }

    #[test]
    fn abort_sets_watch_signal() {
        let (live_tx, _keep) = std::sync::mpsc::channel();
        let s = RunSession::start_with(
            PathBuf::from("/tmp"),
            "fake".to_string(),
            RunSettings::single(),
            ok_runner(live_tx),
        )
        .unwrap();
        // abort() fires the watch sender — the run's forwarder would observe
        // true. Here we just assert no panic and that the session stays alive.
        s.abort();
        assert!(s.handle.is_some());
    }
}

// ===========================================================================
// LiveRunScreen — the ratatui view that drives a RunSession.
// ===========================================================================

use ratatui::layout::{Constraint, Direction, Layout, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, List, ListItem, ListState, Paragraph};
use ratatui::Frame;

use crate::tui::screen::{NavAction, Screen, ScreenCtx};
use crate::tui::widgets;

#[derive(Debug, Clone, Copy, PartialEq)]
enum Phase {
    Running,
    Finished,
    Failed,
}

/// The live run view: transcript + live metrics + activity stream.
#[derive(Debug)]
pub struct LiveRunScreen {
    scenario_id: String,
    session: RunSession,
    strict_judge: bool,
    phase: Phase,
    confirm_abort: bool,
    transcript: Vec<(String, String, String)>, // (role, turn, text)
    scroll: usize,
    /// True = auto-follow the newest transcript line; off when the user scrolls
    /// up so they can read earlier turns while the run continues.
    follow: bool,
    activity: Vec<String>,
    gate: Option<Map<String, Value>>,
}

impl LiveRunScreen {
    pub fn new(session: RunSession, scenario_id: String, strict_judge: bool) -> Self {
        LiveRunScreen {
            scenario_id,
            session,
            strict_judge,
            phase: Phase::Running,
            confirm_abort: false,
            transcript: Vec::new(),
            scroll: 0,
            follow: true,
            activity: Vec::new(),
            gate: None,
        }
    }

    /// Drain the session — called each render tick.
    fn pump(&mut self, ctx: &ScreenCtx) {
        loop {
            match self.session.poll() {
                RunPoll::Events(evs) => {
                    for ev in evs {
                        let kind = ev.get("kind").and_then(Value::as_str).unwrap_or("");
                        match kind {
                            "transcript.user.final" | "transcript.agent.final" => {
                                let role = if kind.starts_with("transcript.user") {
                                    "user"
                                } else {
                                    "agent"
                                };
                                let turn = ev
                                    .get("turn")
                                    .and_then(Value::as_i64)
                                    .unwrap_or(0)
                                    .to_string();
                                let text = ev
                                    .get("spec")
                                    .and_then(Value::as_object)
                                    .and_then(|s| s.get("text"))
                                    .and_then(Value::as_str)
                                    .unwrap_or("")
                                    .to_string();
                                self.transcript.push((role.to_string(), turn, text));
                            }
                            _ => {}
                        }
                        self.activity.push(describe_event(&ev));
                    }
                    // Follow mode: keep the newest line visible unless the
                    // user scrolled up to read earlier turns.
                    if self.follow {
                        self.scroll = self.transcript.len().saturating_sub(1);
                    }
                }
                RunPoll::Finished(out) => {
                    match out {
                        Ok(result) => {
                            self.gate = Some(lks_core::suite::evaluate_run_result(
                                &result,
                                self.strict_judge,
                            ));
                            self.phase =
                                if result.get("status").and_then(Value::as_str) == Some("done") {
                                    Phase::Finished
                                } else {
                                    Phase::Failed
                                };
                        }
                        Err(e) => {
                            self.activity.push(format!("run error: {e}"));
                            self.phase = Phase::Failed;
                        }
                    }
                    let _ = ctx;
                    return;
                }
                RunPoll::Running => break,
            }
        }
    }

    pub fn render(&mut self, f: &mut Frame, area: Rect, ctx: &ScreenCtx) {
        self.pump(ctx);

        let header_h = 4u16;
        let cols = Layout::default()
            .direction(Direction::Horizontal)
            .constraints([Constraint::Percentage(60), Constraint::Percentage(40)])
            .split(area);

        // Header
        let header_area = Rect {
            height: header_h,
            ..area
        };
        let phase_txt = match self.phase {
            Phase::Running => "RUNNING",
            Phase::Finished => "DONE",
            Phase::Failed => "FAILED",
        };
        let phase_style = match self.phase {
            Phase::Running => Style::default().fg(Color::Yellow),
            Phase::Finished => Style::default().fg(Color::Green),
            Phase::Failed => Style::default().fg(Color::Red),
        };
        let mut hl = vec![
            Span::styled(phase_txt, phase_style),
            Span::raw(format!("  {}  ", self.scenario_id)),
            Span::raw(format!("turn {}", self.session.current_turn())),
            Span::raw(format!("  events {}", self.session.events.len())),
        ];
        if let Some(rid) = self.session.run_id() {
            hl.push(Span::styled(
                format!("  {rid}"),
                Style::default().fg(Color::DarkGray),
            ));
        }
        f.render_widget(
            Paragraph::new(Line::from(hl))
                .block(Block::default().borders(Borders::ALL).title("Live run")),
            header_area,
        );

        // Left: transcript (below header)
        let left = Rect {
            y: header_h,
            height: area.height.saturating_sub(header_h),
            ..cols[0]
        };
        let items: Vec<ListItem> = self
            .transcript
            .iter()
            .map(|(role, turn, text)| {
                let color = if role == "user" {
                    Color::Cyan
                } else {
                    Color::Magenta
                };
                let label = if role == "user" { "USER" } else { "AGENT" };
                ListItem::new(Line::from(vec![
                    Span::styled(
                        format!("[{label} {turn}] "),
                        Style::default().fg(color).add_modifier(Modifier::BOLD),
                    ),
                    Span::raw(text.clone()),
                ]))
            })
            .collect();
        let mut state = ListState::default();
        state.select(Some(
            self.scroll.min(self.transcript.len().saturating_sub(1)),
        ));
        f.render_stateful_widget(
            List::new(items)
                .block(widgets::title_block("Transcript", None))
                .highlight_style(Style::default().add_modifier(Modifier::REVERSED)),
            left,
            &mut state,
        );

        // Right: metrics digest + activity
        let right = cols[1];
        let split = Layout::default()
            .direction(Direction::Vertical)
            .constraints([Constraint::Percentage(35), Constraint::Percentage(65)])
            .split(right);
        let digest = self.session.digest();
        let mut dlines = vec![
            Line::from(format!("elapsed: {}", self.session.elapsed().as_secs())),
            Line::from(format!("ttfw_ms:  {}", get_digest(&digest, "ttfw_ms"))),
            Line::from(format!("turn_p50: {}", get_digest(&digest, "turn_p50_ms"))),
            Line::from(format!("turn_p95: {}", get_digest(&digest, "turn_p95_ms"))),
            Line::from(format!("barge:    {}", get_digest(&digest, "barge_count"))),
            Line::from(format!("tools:    {}", get_digest(&digest, "tool_count"))),
            Line::from(format!("errors:   {}", get_digest(&digest, "tool_errors"))),
        ];
        if let Some(g) = &self.gate {
            dlines.push(Line::from(""));
            dlines.push(Line::from(format!(
                "gate: {}",
                g.get("gate").and_then(Value::as_str).unwrap_or("")
            )));
        }
        f.render_widget(
            Paragraph::new(dlines).block(widgets::title_block("Live metrics", None)),
            split[0],
        );

        let act_items: Vec<ListItem> = self
            .activity
            .iter()
            .rev()
            .take(40)
            .map(|a| ListItem::new(Line::from(widgets::truncate(a, 60))))
            .collect();
        f.render_widget(
            List::new(act_items).block(widgets::title_block("Activity", None)),
            split[1],
        );
    }

    pub fn on_key(
        &mut self,
        key: ratatui::crossterm::event::KeyEvent,
        ctx: &ScreenCtx,
    ) -> NavAction {
        use ratatui::crossterm::event::KeyCode;
        match key.code {
            KeyCode::Esc | KeyCode::Char('x') => {
                if self.phase == Phase::Running {
                    if self.confirm_abort {
                        self.session.abort();
                        self.activity.push("abort requested".into());
                        self.confirm_abort = false;
                        NavAction::Toast("abort sent".into())
                    } else {
                        self.confirm_abort = true;
                        NavAction::Toast("abort? Esc again to confirm".into())
                    }
                } else {
                    NavAction::Pop
                }
            }
            KeyCode::Char('s') | KeyCode::Enter => {
                if self.phase != Phase::Running {
                    if let Some(rid) = self.session.run_id().map(|s| s.to_string()) {
                        NavAction::Push(Box::new(Screen::RunDetail(
                            crate::tui::run_detail::RunDetailScreen::load(ctx, &rid),
                        )))
                    } else {
                        NavAction::None
                    }
                } else {
                    NavAction::None
                }
            }
            KeyCode::Char('r') => {
                if self.phase != Phase::Running {
                    let id = self.scenario_id.clone();
                    NavAction::Push(Box::new(Screen::RunSetup(
                        crate::tui::run_setup::RunSetupScreen::new(ctx, &id),
                    )))
                } else {
                    NavAction::None
                }
            }
            KeyCode::Char('j') | KeyCode::Down => {
                self.follow = false;
                self.scroll = (self.scroll + 1).min(self.transcript.len().saturating_sub(1));
                NavAction::None
            }
            KeyCode::Char('k') | KeyCode::Up => {
                self.follow = false;
                self.scroll = self.scroll.saturating_sub(1);
                NavAction::None
            }
            KeyCode::Char('G') => {
                self.follow = true;
                self.scroll = self.transcript.len().saturating_sub(1);
                NavAction::None
            }
            _ => NavAction::None,
        }
    }
}

fn get_digest(d: &Map<String, Value>, k: &str) -> String {
    match d.get(k) {
        Some(Value::Number(n)) => n.to_string(),
        Some(Value::String(s)) => s.clone(),
        Some(Value::Null) | None => "–".to_string(),
        _ => "–".to_string(),
    }
}
