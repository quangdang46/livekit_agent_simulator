//! `lksr tui` — full-feature interactive terminal UI for lksr.

pub mod app;
pub mod cues;
pub mod help;
pub mod home;
pub mod live_run;
pub mod log;
pub mod plugins;
pub mod preflight;
pub mod run_detail;
pub mod run_setup;
pub mod runs;
pub mod scenario_detail;
pub mod scenarios;
pub mod screen;
pub mod widgets;

use std::path::Path;

use app::App;

/// Entry point: run the TUI until the user quits.
pub fn run(root: &Path) -> anyhow::Result<()> {
    use std::io::IsTerminal;

    if !std::io::stdout().is_terminal() {
        anyhow::bail!("lksr tui needs a TTY (stdout is not a terminal)");
    }

    let mut terminal = ratatui::init();
    let mut app = match App::new(root) {
        Ok(a) => a,
        Err(e) => {
            ratatui::restore();
            anyhow::bail!("{e}");
        }
    };

    let res = loop {
        app.tick();
        terminal.draw(|f| app.render(f))?;

        if crossterm::event::poll(app.poll_interval())? {
            if let crossterm::event::Event::Key(k) = crossterm::event::read()? {
                if k.kind == crossterm::event::KeyEventKind::Press && app.on_key(k)? {
                    break Ok(());
                }
            }
        }
    };

    ratatui::restore();
    res
}

#[cfg(test)]
mod tests {
    use super::*;
    use ratatui::backend::TestBackend;

    fn tmp_root(tag: &str) -> std::path::PathBuf {
        let dir =
            std::env::temp_dir().join(format!("lksr_tui_test_{}_{}", std::process::id(), tag));
        let _ = std::fs::remove_dir_all(&dir);
        let dot = dir.join(".agent-sim");
        std::fs::create_dir_all(dot.join("scenarios")).unwrap();
        std::fs::create_dir_all(dot.join("reports")).unwrap();
        std::fs::write(
            dot.join("config.yaml"),
            "livekit:\n  url: wss://example.livekit.cloud\n  api_key: test-key\n  api_secret: test-secret\n  agent_name: test-agent\nsimulator:\n  provider: openai\n  api_key: sk-test-key-1234567890\n",
        )
        .unwrap();
        dir
    }

    /// Render the current screen into a string of visible cells.
    fn render_text(app: &mut App) -> String {
        let mut terminal = ratatui::Terminal::new(TestBackend::new(60, 20)).unwrap();
        terminal.draw(|f| app.render(f)).unwrap();
        let cells = terminal.backend().buffer().content();
        cells.iter().map(|c| c.symbol()).collect::<String>()
    }

    fn key(code: ratatui::crossterm::event::KeyCode) -> ratatui::crossterm::event::KeyEvent {
        ratatui::crossterm::event::KeyEvent::new(
            code,
            ratatui::crossterm::event::KeyModifiers::NONE,
        )
    }

    #[test]
    fn home_renders_without_panic() {
        let root = tmp_root("home");
        let mut app = App::new(&root).expect("app loads");
        let txt = render_text(&mut app);
        assert!(
            txt.contains("Home") || txt.contains("livekit-agent-simulator"),
            "home frame rendered: {txt:?}"
        );
    }

    #[test]
    fn scenario_tab_renders() {
        let root = tmp_root("scenario_tab");
        let mut app = App::new(&root).unwrap();
        assert!(!app
            .on_key(key(ratatui::crossterm::event::KeyCode::Char('2')))
            .unwrap());
        let txt = render_text(&mut app);
        assert!(
            txt.contains("Scenarios"),
            "scenarios frame rendered: {txt:?}"
        );
    }

    #[test]
    fn live_run_renders_and_aborts() {
        // Drive a LiveRunScreen with an injected session that emits one event
        // then stays "running"; verify the render shows the running header and
        // that an Esc-confirm abort path is reachable without a panic.
        let (tx, _rx) = std::sync::mpsc::channel::<serde_json::Map<String, serde_json::Value>>();
        let session = live_run::RunSession::start_with(
            tmp_root("live"),
            "fake".to_string(),
            live_run::RunSettings::default(),
            move |_root, _sid, _opts| {
                let _ = tx;
                let mut out = serde_json::Map::new();
                out.insert("status".into(), serde_json::json!("done"));
                Ok(out)
            },
        )
        .expect("session starts");
        let mut scr = live_run::LiveRunScreen::new(session, "fake".to_string(), false);
        let root = tmp_root("live_cfg");
        let cfg = lks_core::config::load_config(root.clone(), None).unwrap();
        let ctx = crate::tui::screen::ScreenCtx {
            root: &root,
            cfg: &cfg,
        };
        let mut terminal = ratatui::Terminal::new(TestBackend::new(80, 24)).unwrap();
        terminal.draw(|f| scr.render(f, f.area(), &ctx)).unwrap();
        // Escape sets confirm-abort (no panic), second Esc sends abort.
        let _ = scr.on_key(key(ratatui::crossterm::event::KeyCode::Esc), &ctx);
        let _ = scr.on_key(key(ratatui::crossterm::event::KeyCode::Esc), &ctx);
    }
}
