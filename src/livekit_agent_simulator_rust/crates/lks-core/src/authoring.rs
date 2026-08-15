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

/// Embedded template texts — shipped inside the binary so `guide`/`init`/
/// `scenario-init` work from ANY cwd (installed binary has no repo walk).
const EMBEDDED: &[(&str, &str)] = &[
    (
        "GUIDE.md",
        include_str!("../../../../../templates/GUIDE.md"),
    ),
    (
        "config.yaml",
        include_str!("../../../../../templates/config.yaml"),
    ),
    (
        "smoke-hello.yaml",
        include_str!("../../../../../templates/smoke-hello.yaml"),
    ),
    (
        "scenario-scaffold.yaml",
        include_str!("../../../../../templates/scenario-scaffold.yaml"),
    ),
    (
        "plugins/example_verify.py",
        include_str!("../../../../../templates/plugins/example_verify.py"),
    ),
    (
        "cues/README.md",
        include_str!("../../../../../templates/cues/README.md"),
    ),
];

/// Find the package templates dir — repo-root `templates/` (walk up ≤ 6
/// parents, dev checkout) with an embedded fallback for installed binaries.
/// Returns a path only when the CWD walk finds the repo (needed for the cues
/// WAV catalog); callers use the embedded texts when this returns None.
pub fn package_templates_dir() -> Option<PathBuf> {
    let mut p = std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
    for _ in 0..8 {
        let cand = p.join("templates");
        if cand.join("config.yaml").exists() {
            return Some(cand);
        }
        if !p.pop() {
            break;
        }
    }
    None
}

/// Read an embedded template text (installed-binary fallback).
pub fn embedded_template(name: &str) -> Option<&'static str> {
    EMBEDDED.iter().find(|(n, _)| *n == name).map(|(_, t)| *t)
}

fn copy_if_missing(src: &Path, dst: &Path, created: &mut Vec<String>) -> std::io::Result<()> {
    if dst.exists() {
        return Ok(());
    }
    std::fs::copy(src, dst)?;
    created.push(dst.to_string_lossy().into_owned());
    Ok(())
}

/// Copy a template to dst — from the repo file when available (dev), else the
/// embedded text (installed binary).
fn copy_text_if_missing(
    name: &str,
    src: Option<PathBuf>,
    dst: &Path,
    created: &mut Vec<String>,
) -> std::io::Result<()> {
    if dst.exists() {
        return Ok(());
    }
    if let Some(src) = src {
        return copy_if_missing(&src, dst, created);
    }
    let text = embedded_template(name).ok_or_else(|| {
        std::io::Error::new(std::io::ErrorKind::NotFound, format!("{name} not embedded"))
    })?;
    std::fs::write(dst, text)?;
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
            embedded_template("cues/README.md").unwrap_or(
                "# Target audio cues (room_pcm)\n\nDrop **PCM16 mono @ 24 kHz** WAVs here to override package built-ins or add project-specific noise.\n\nScenario: `\"delivery\":\"room_pcm\",\"asset\":\"my_noise.wav\"` or `\"asset\":\"builtin:noise.loud\"`.\n\nList: `lks cues --root .`\n",
            ),
        )
        .map_err(io_err)?;
        created.push(cues_readme.to_string_lossy().into_owned());
    }

    // config.yaml, smoke scenario, example plugin (copy from templates;
    // embedded texts when the repo walk fails — installed binary).
    copy_text_if_missing(
        "config.yaml",
        templates.as_ref().map(|t| t.join("config.yaml")),
        &dot.join("config.yaml"),
        &mut created,
    )
    .map_err(io_err)?;
    copy_text_if_missing(
        "smoke-hello.yaml",
        templates.as_ref().map(|t| t.join("smoke-hello.yaml")),
        &dot.join("scenarios").join("smoke-hello.yaml"),
        &mut created,
    )
    .map_err(io_err)?;
    copy_text_if_missing(
        "plugins/example_verify.py",
        templates
            .as_ref()
            .map(|t| t.join("plugins").join("example_verify.py")),
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
    let gitignore_existed = gitignore.exists();
    let already = content.split('\n').any(|l| l.trim_end() == line);
    if !already {
        if !content.trim().is_empty() {
            content.push('\n');
        }
        content.push_str(&format!("\n# livekit-agent-simulator\n{line}\n"));
        std::fs::write(&gitignore, content).map_err(io_err)?;
        if gitignore_existed {
            created.push(format!(
                "{} (+{})",
                gitignore.to_string_lossy(),
                line.trim_end()
            ));
        } else {
            created.push(gitignore.to_string_lossy().into_owned());
        }
    }

    let config_dst = dot.join("config.yaml");
    let next = vec![
        json!(format!(
            "Fill in LiveKit + Google credentials in {}",
            config_dst.display()
        )),
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
    let text = match templates {
        Some(t) => {
            let scaffold = t.join("scenario-scaffold.yaml");
            if !scaffold.exists() {
                return Err(ConfigError(format!(
                    "Package scaffold missing: {}",
                    scaffold.display()
                )));
            }
            std::fs::read_to_string(&scaffold)
                .map_err(|e| ConfigError(format!("{}: read error — {e}", scaffold.display())))?
        }
        None => embedded_template("scenario-scaffold.yaml")
            .ok_or_else(|| ConfigError("scenario-scaffold.yaml not embedded".into()))?
            .to_string(),
    }
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
            "Edit {} — # lines are guides; remove unused sections",
            dest.display()
        )),
        json!(format!(
            "Validate: lks validate {scenario_id} --root {}",
            root.display()
        )),
        json!(format!(
            "Run: lks execute {scenario_id} --root {}",
            root.display()
        )),
    ];

    let mut out = Map::new();
    out.insert("path".into(), json!(dest.to_string_lossy().into_owned()));
    out.insert("scenario_id".into(), json!(scenario_id));
    out.insert("created".into(), json!(true));
    out.insert("overwritten".into(), json!(force));
    out.insert("next_steps".into(), Json::Array(next));
    Ok(out)
}
