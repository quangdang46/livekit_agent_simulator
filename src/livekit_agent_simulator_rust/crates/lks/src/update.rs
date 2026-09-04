//! Background version check + `lksr update` for the Rust CLI.
//!
//! Follows the pattern used by strix / marimo / pyselfupdate:
//! - Background daemon-thread check against GitHub releases, at most once per 24 h.
//! - State cached in `~/.lks/update-check.json`.
//! - Non-intrusive notice printed to stderr after command output.
//! - `lksr update` downloads and replaces the binary in place.

use std::fs;
use std::io::Read as _;
use std::path::PathBuf;
use std::sync::LazyLock;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use anyhow::{Context, Result};

// ── constants ──────────────────────────────────────────────────────────────

const GITHUB_REPO: &str = "quangdang46/livekit-agent-simulator";
const CHECK_INTERVAL: Duration = Duration::from_secs(24 * 60 * 60); // 24 h
const REQUEST_TIMEOUT: Duration = Duration::from_secs(5);

static CURRENT_VERSION: LazyLock<String> = LazyLock::new(|| {
    // Workspace version: 0.1.0-rust; strip the suffix for comparison.
    let v = env!("CARGO_PKG_VERSION");
    v.split('-').next().unwrap_or(v).to_string()
});

fn current_version() -> &'static str {
    &CURRENT_VERSION
}

// ── env / CI gates ──────────────────────────────────────────────────────────

const SKIP_ENV_KEYS: &[&str] = &[
    "CI",
    "GITHUB_ACTIONS",
    "GITLAB_CI",
    "JENKINS_URL",
    "BUILDKITE",
    "CIRCLECI",
];

fn is_disabled() -> bool {
    if std::env::var("NO_UPDATE_CHECK").is_ok() {
        return true;
    }
    SKIP_ENV_KEYS.iter().any(|k| std::env::var(k).is_ok())
}

// ── state file ──────────────────────────────────────────────────────────────

fn state_path() -> PathBuf {
    dirs().join("update-check.json")
}

fn dirs() -> PathBuf {
    std::env::var("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from("."))
        .join(".lks")
}

fn read_cache() -> serde_json::Value {
    fs::read_to_string(state_path())
        .ok()
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or(serde_json::Value::Null)
}

fn write_cache(map: &serde_json::Map<String, serde_json::Value>) {
    let _ = fs::create_dir_all(dirs());
    let _ = fs::write(
        state_path(),
        serde_json::to_string_pretty(map).unwrap_or_default(),
    );
}

fn cache_timestamp() -> Option<u64> {
    read_cache().get("checked_at").and_then(|v| v.as_u64())
}

// ── version helpers ─────────────────────────────────────────────────────────

fn parse_version(v: &str) -> Vec<u64> {
    v.trim()
        .strip_prefix('v')
        .unwrap_or(v)
        .split('.')
        .filter_map(|p| p.parse().ok())
        .collect()
}

fn is_newer(latest: &str, current: &str) -> bool {
    parse_version(latest) > parse_version(current)
}

// ── network ─────────────────────────────────────────────────────────────────

fn fetch_available_update() -> Option<String> {
    // Check timestamp BEFORE the request (prevents API hammering on failure).
    if let Some(ts) = cache_timestamp() {
        if let Ok(elapsed) = SystemTime::now().duration_since(UNIX_EPOCH) {
            if elapsed.as_secs() < ts + CHECK_INTERVAL.as_secs() {
                return None;
            }
        }
    }

    let url = format!("https://api.github.com/repos/{GITHUB_REPO}/releases/latest");
    let client = reqwest::blocking::Client::builder()
        .user_agent("lksr-update-check")
        .timeout(REQUEST_TIMEOUT)
        .build()
        .ok()?;

    let resp = client.get(&url).send().ok()?;
    let release: serde_json::Value = resp.json().ok()?;

    let tag = release.get("tag_name")?.as_str()?;
    let version = tag.strip_prefix('v').unwrap_or(tag);

    // Save to cache (timestamp first, even on partial success).
    let mut cache = read_cache().as_object().cloned().unwrap_or_default();
    if let Ok(now) = SystemTime::now().duration_since(UNIX_EPOCH) {
        cache.insert("checked_at".into(), serde_json::Value::from(now.as_secs()));
    }
    cache.insert(
        "latest_version".into(),
        serde_json::Value::String(version.to_string()),
    );
    write_cache(&cache);

    Some(version.to_string())
}

// ── platform detection ──────────────────────────────────────────────────────

fn current_platform() -> Option<&'static str> {
    let os = std::env::consts::OS;
    let arch = std::env::consts::ARCH;
    match (os, arch) {
        ("macos", "aarch64") => Some("macos-aarch64"),
        ("macos", "x86_64") => Some("macos-x86_64"),
        ("linux", "x86_64") => Some("linux-x86_64"),
        ("linux", "aarch64") => Some("linux-aarch64"),
        _ => None,
    }
}

// ── background check ────────────────────────────────────────────────────────

fn refresh_cache_background() {
    // Spawn a daemon thread to do the network check.
    let _ = std::thread::Builder::new()
        .name("update-check".into())
        .spawn(|| {
            fetch_available_update();
        });
}

pub fn start_background_check() {
    if is_disabled() {
        return;
    }
    refresh_cache_background();
}

// ── notification (called from CLI after command output) ──────────────────────

pub fn get_available_update(respect_skip: bool) -> Option<String> {
    if is_disabled() {
        return None;
    }

    let cache = read_cache();
    let latest = cache.get("latest_version")?.as_str()?;
    let current = current_version();

    if !is_newer(latest, current) {
        return None;
    }

    if respect_skip {
        if let Some(skip) = cache.get("skipped_version").and_then(|v| v.as_str()) {
            if skip == latest {
                return None;
            }
        }
    }

    Some(latest.to_string())
}

pub fn notify_update() {
    if let Some(latest) = get_available_update(true) {
        let current = current_version();
        let platform = current_platform().unwrap_or("your-platform");
        eprintln!(
            "A new version of lksr is available: {current} → {latest}\n\
             \x20 Run `lksr update` to upgrade (detected: {platform})."
        );
    }
}

// ── interactive update (lksr update) ────────────────────────────────────────

pub fn run_update() -> Result<()> {
    let platform = current_platform()
        .context("unsupported platform (expected linux-x86_64 or macos-aarch64)")?
        .to_string();

    let cache = read_cache();
    let latest = cache
        .get("latest_version")
        .and_then(|v| v.as_str())
        .map(|s| s.to_string());

    // If no cached version, fetch now.
    let latest = match latest {
        Some(v) => v,
        None => fetch_available_update().context("failed to fetch latest version from GitHub")?,
    };

    let current = current_version().to_string();
    if !is_newer(&latest, &current) {
        println!("lksr is already up to date ({current}).");
        return Ok(());
    }

    let asset_name = format!("lksr-{platform}.tar.gz");
    let url = format!("https://github.com/{GITHUB_REPO}/releases/download/v{latest}/{asset_name}");

    eprintln!("Downloading: {url}");

    // Download tarball to temp file.
    let client = reqwest::blocking::Client::builder()
        .user_agent("lksr-update")
        .timeout(Duration::from_secs(60))
        .build()
        .context("failed to build HTTP client")?;

    let bytes = client
        .get(&url)
        .send()
        .context("download request failed")?
        .bytes()
        .context("download body read failed")?;

    eprintln!("Downloaded {} bytes — extracting...", bytes.len());

    // Extract lksr binary from tarball.
    let decoder = flate2::read::GzDecoder::new(bytes.as_ref());
    let mut archive = tar::Archive::new(decoder);
    let mut new_binary: Option<Vec<u8>> = None;

    for entry in archive.entries().context("failed to read tar entries")? {
        let mut entry = entry.context("failed to read tar entry")?;
        let entry_path = entry.path().context("invalid tar entry path")?;
        if entry_path.file_name() == Some(std::ffi::OsStr::new("lksr")) {
            let mut buf = Vec::new();
            entry.read_to_end(&mut buf)?;
            new_binary = Some(buf);
            break;
        }
    }

    let binary_data = new_binary.context("lksr binary not found in tarball")?;

    // Determine current binary path.
    let exe_path = std::env::current_exe().context("cannot determine current executable path")?;

    // Write new binary to a temp file, then rename atomically.
    let tmp_path = exe_path.with_extension("tmp");
    fs::write(&tmp_path, &binary_data).context("failed to write new binary")?;

    // On Unix, ensure executable permission.
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&tmp_path, fs::Permissions::from_mode(0o755))
            .context("failed to set executable permission")?;
    }

    fs::rename(&tmp_path, &exe_path).context("failed to replace binary (permission denied?)")?;

    eprintln!("✓ lksr updated: {current} → {latest} — restart to use the new version.");
    Ok(())
}
