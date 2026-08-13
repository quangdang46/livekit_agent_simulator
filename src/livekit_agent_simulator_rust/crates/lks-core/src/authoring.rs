//! Authoring scaffolds — init_project / init_scenario (mirror ops.py).
//!
//! init_project: scaffold `.agent-sim/` with config.yaml template + smoke
//!   scenario + example plugin + cues README; gitignore `.agent-sim/`.
//! init_scenario: scaffold `.agent-sim/scenarios/<id>.yaml` with `#` guide
//!   comments, substituting `{{SCENARIO_ID}}`; validate it parses after.

use std::path::{Path, PathBuf};

use serde_json::{json, Map, Value as Json};

use crate::errors::ConfigError;
use crate::scenario_ops::is_valid_scenario_id;
use crate::scenario_yaml::load_scenario_yaml;

pub const DOT_FOLDER: &str = ".agent-sim";

/// Find the package templates dir — repo-root `templates/` (walk up ≤ 6 parents).
fn package_templates_dir() -> PathBuf {
    // In the Rust port, templates live at the repo root `templates/`. Walk up
    // from the crate dir (crate is at <root>/src/livekit_agent_simulator_rust/
    // crates/lks-core) to find <root>/templates.
    let mut p = std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
    for _ in 0..8 {
        let cand = p.join("templates");
        if cand.join("config.yaml").exists() {
            return cand;
        }
        if !p.pop() {
            break;
        }
    }
    // Fallback: look relative to the crate source.
    PathBuf::from("templates")
}

fn copy_if_missing(src: &Path, dst: &Path, created: &mut Vec<String>) -> std::io::Result<()> {
    if dst.exists() {
        return Ok(());
    }
    std::fs::copy(src, dst)?;
    created.push(dst.to_string_lossy().into_owned());
    Ok(())
}

/// Scaffold `.agent-sim/` with templates + gitignore. Mirrors ops.init_project.
pub fn init_project(project_root: &Path) -> Result<Map<String, Json>, ConfigError> {
    let root = project_root
        .canonicalize()
        .unwrap_or_else(|_| project_root.to_path_buf());
    let templates = package_templates_dir();
    let dot = root.join(DOT_FOLDER);
    let mut created: Vec<String> = Vec::new();

    let mk = |sub: &str| -> std::io::Result<()> { std::fs::create_dir_all(dot.join(sub)) };
    mk("scenarios").map_err(io_err)?;
    mk("reports").map_err(io_err)?;
    mk("plugins").map_err(io_err)?;
    mk("cues").map_err(io_err)?;

    // cues README (if missing)
    let cues_readme = dot.join("cues").join("README.md");
    if !cues_readme.exists() {
        std::fs::write(
            &cues_readme,
            "# Target audio cues (room_pcm)\n\nDrop **PCM16 mono @ 24 kHz** WAVs here to override package built-ins or add project-specific noise.\n\nScenario: `\"delivery\":\"room_pcm\",\"asset\":\"my_noise.wav\"` or `\"asset\":\"builtin:noise.loud\"`.\n\nList: `lks cues --root .`\n",
        )
        .map_err(io_err)?;
        created.push(cues_readme.to_string_lossy().into_owned());
    }

    // config.yaml, smoke scenario, example plugin (copy from templates)
    copy_if_missing(
        &templates.join("config.yaml"),
        &dot.join("config.yaml"),
        &mut created,
    )
    .map_err(io_err)?;
    copy_if_missing(
        &templates.join("smoke-hello.yaml"),
        &dot.join("scenarios").join("smoke-hello.yaml"),
        &mut created,
    )
    .map_err(io_err)?;
    copy_if_missing(
        &templates.join("plugins").join("example_verify.py"),
        &dot.join("plugins").join("example_verify.py"),
        &mut created,
    )
    .map_err(io_err)?;

    // gitignore `.agent-sim/`
    let line = format!("{DOT_FOLDER}/");
    let gitignore = root.join(".gitignore");
    let mut content = if gitignore.exists() {
        std::fs::read_to_string(&gitignore).unwrap_or_default()
    } else {
        String::new()
    };
    let already = content.split('\n').any(|l| l.trim_end() == line);
    if !already {
        if !content.trim().is_empty() {
            content.push('\n');
        }
        content.push_str(&format!("\n# livekit-agent-simulator\n{line}\n"));
        std::fs::write(&gitignore, content).map_err(io_err)?;
        created.push(format!("{} (+{})", gitignore.to_string_lossy(), DOT_FOLDER));
    }

    let next = vec![
        json!("Fill in LiveKit + provider credentials in .agent-sim/config.yaml"),
        json!("Make sure your worker is running with the configured agent_name"),
        json!("Run the smoke scenario: lks execute smoke-hello"),
    ];

    let mut out = Map::new();
    out.insert("dot_dir".into(), json!(dot.to_string_lossy().into_owned()));
    out.insert(
        "created".into(),
        Json::Array(created.into_iter().map(Json::String).collect()),
    );
    out.insert("next_steps".into(), Json::Array(next));
    Ok(out)
}

fn io_err(e: std::io::Error) -> ConfigError {
    ConfigError(format!("I/O error: {e}"))
}

/// Scaffold `.agent-sim/scenarios/<id>.yaml`. Mirrors ops.init_scenario.
pub fn init_scenario(
    project_root: &Path,
    scenario_id: &str,
    force: bool,
) -> Result<Map<String, Json>, ConfigError> {
    let scenario_id = scenario_id.trim();
    if !is_valid_scenario_id(scenario_id) {
        return Err(ConfigError(format!(
            "Invalid scenario_id {scenario_id:?}: use letters/digits/[_-], start with alnum, max 64 chars"
        )));
    }
    let root = project_root
        .canonicalize()
        .unwrap_or_else(|_| project_root.to_path_buf());
    let scenarios_dir = root.join(DOT_FOLDER).join("scenarios");
    std::fs::create_dir_all(&scenarios_dir).map_err(io_err)?;

    let dest = scenarios_dir.join(format!("{scenario_id}.yaml"));
    if dest.exists() && !force {
        return Err(ConfigError(format!(
            "{} already exists. Pass force=true / --force to overwrite, or pick another id.",
            dest.display()
        )));
    }

    let templates = package_templates_dir();
    let scaffold = templates.join("scenario-scaffold.yaml");
    if !scaffold.exists() {
        return Err(ConfigError(format!(
            "Package scaffold missing: {}",
            scaffold.display()
        )));
    }
    let text = std::fs::read_to_string(&scaffold)
        .map_err(|e| ConfigError(format!("{}: read error — {e}", scaffold.display())))?
        .replace("{{SCENARIO_ID}}", scenario_id);
    std::fs::write(&dest, &text)
        .map_err(|e| ConfigError(format!("{}: write error — {e}", dest.display())))?;

    // Ensure the scaffold still parses after id substitution.
    if let Err(e) = load_scenario_yaml(&dest) {
        let _ = std::fs::remove_file(&dest);
        return Err(ConfigError(format!("Scaffold failed validation: {e}")));
    }

    let next = vec![
        json!(format!(
            "Edit .agent-sim/scenarios/{scenario_id}.yaml (persona.brief is required)"
        )),
        json!("Validate: lks validate <id> --root ."),
        json!("Run: lks execute <id> --root ."),
    ];

    let mut out = Map::new();
    out.insert("path".into(), json!(dest.to_string_lossy().into_owned()));
    out.insert("scenario_id".into(), json!(scenario_id));
    out.insert("created".into(), json!(true));
    out.insert("overwritten".into(), json!(force));
    out.insert("next_steps".into(), Json::Array(next));
    Ok(out)
}
