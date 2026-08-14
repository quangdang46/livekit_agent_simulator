//! Verify plugins for livekit-agent-simulator.
//!
//! P8 decision: the Rust build does NOT embed CPython (binary size + CI
//! complexity). `.py` verify plugins (before_run/after_run/verify) run under
//! the Python build (`lks`). Here we implement the fail-loud contract:
//! - `plugins_available()` → false
//! - `reject_plugins(modules)` → a loud error message naming the modules and
//!   the Python build, so a scenario with plugin_modules never silently skips.

/// Are Python verify plugins available in this build? Always false.
pub fn plugins_available() -> bool {
    false
}

/// Loud error for a scenario that sets plugin_modules.
pub fn reject_plugins(modules: &[String]) -> String {
    format!(
        "verify plugins require the Python build (lks): {} — the Rust build does not embed CPython. Remove plugin_modules or use lks.",
        modules.join(", ")
    )
}

/// Validate-time hook: returns Some(error) if plugin_modules is set.
pub fn validate_plugins(modules: &[String]) -> Option<String> {
    if modules.is_empty() {
        None
    } else {
        Some(reject_plugins(modules))
    }
}
