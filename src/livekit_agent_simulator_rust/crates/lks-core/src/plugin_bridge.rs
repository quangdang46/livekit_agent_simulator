//! Python plugin bridge — load and execute `.agent-sim/plugins/*.py`.
//!
//! Without `python-plugins` feature: static scan of `@verify_plugin` decorators
//! (no Python import). With `python-plugins`: embedded Python via PyO3.

use std::path::Path;

use serde_json::Value as Json;

use crate::config::DOT_FOLDER;

/// Plugin load info — mirrors Python's `ensure_plugins_loaded` return shape.
#[derive(Debug, Clone, Default)]
pub struct PluginLoadInfo {
    pub loaded: Vec<String>,
    pub errors: Vec<String>,
    pub verify_plugins: Vec<String>,
}

/// Plugin context for verify hooks.
#[derive(Debug, Clone)]
pub struct VerifyPluginContext {
    pub events: Vec<Json>,
    pub scenario_id: String,
    pub plugin_name: String,
    pub project_root: std::path::PathBuf,
}

/// Result from a verify plugin.
#[derive(Debug, Clone)]
pub struct VerifyPluginResult {
    pub pass: bool,
    pub checks: Vec<Json>,
}

/// Context for before_run hooks (port of api.BeforeRunContext).
#[derive(Debug, Clone)]
pub struct BeforeRunContext {
    pub scenario_id: String,
    pub project_root: std::path::PathBuf,
    pub run_id: String,
    pub run_name: Option<String>,
}

/// Context for after_run hooks (port of api.AfterRunContext).
#[derive(Debug, Clone)]
pub struct AfterRunContext {
    pub scenario_id: String,
    pub project_root: std::path::PathBuf,
    pub run_id: String,
    pub run_name: Option<String>,
    pub report_dir: std::path::PathBuf,
    pub status: String,
}

// ===========================================================================
// Feature-gated implementations
// ===========================================================================

/// Discover and load all Python plugins from `.agent-sim/plugins/*.py`.
pub fn ensure_plugins_loaded(
    project_root: &Path,
    module_names: Option<&[String]>,
) -> PluginLoadInfo {
    let plugins_dir = project_root.join(DOT_FOLDER).join("plugins");

    if !plugins_dir.exists() {
        return PluginLoadInfo::default();
    }

    // Collect .py module names from the plugins directory
    let mut py_modules: Vec<String> = Vec::new();
    if let Ok(rd) = std::fs::read_dir(&plugins_dir) {
        for entry in rd.flatten() {
            let p = entry.path();
            if p.extension().and_then(|e| e.to_str()) == Some("py") {
                if let Some(stem) = p.file_stem().map(|s| s.to_string_lossy().into_owned()) {
                    if !stem.starts_with('_') {
                        py_modules.push(stem);
                    }
                }
            }
        }
    }
    py_modules.sort();

    // Merge with explicitly requested modules
    let all_modules: Vec<String> = if let Some(names) = module_names {
        let mut merged = py_modules;
        for n in names {
            if !merged.contains(&n.to_string()) {
                merged.push(n.clone());
            }
        }
        merged.sort();
        merged.dedup();
        merged
    } else {
        py_modules
    };

    #[cfg(feature = "python-plugins")]
    {
        _load_plugins_python(project_root, &plugins_dir, &all_modules)
    }

    #[cfg(not(feature = "python-plugins"))]
    {
        let verify_plugins = _static_scan_verify_plugins(&plugins_dir);
        PluginLoadInfo {
            loaded: all_modules
                .into_iter()
                .map(|m| format!("local:{m}"))
                .collect(),
            errors: Vec::new(),
            verify_plugins,
        }
    }
}

/// Execute a registered verify plugin by name.
pub fn run_verify_plugin(
    _project_root: &Path,
    plugin_name: &str,
    ctx: &VerifyPluginContext,
) -> Option<VerifyPluginResult> {
    let _ = plugin_name;
    let _ = ctx;

    #[cfg(feature = "python-plugins")]
    {
        _run_verify_python(plugin_name, ctx)
    }

    #[cfg(not(feature = "python-plugins"))]
    {
        None
    }
}

/// Execute registered before_run hooks (port of plugin_registry.before_run).
pub fn run_before_run_hooks(
    _project_root: &Path,
    _ctx: &BeforeRunContext,
) -> Vec<String> {
    #[cfg(feature = "python-plugins")]
    {
        python_impl::_run_hooks_python("before_run", _project_root, _ctx.scenario_id.as_str(),
            Some(&format!("{{'run_id': '{}', 'run_name': {}}}",
                _ctx.run_id,
                _ctx.run_name.as_deref().map(|s| format!("'{}'", s)).unwrap_or("None".into()),
            )))
    }
    #[cfg(not(feature = "python-plugins"))]
    { Vec::new() }
}

/// Execute registered after_run hooks (port of plugin_registry.after_run).
pub fn run_after_run_hooks(
    _project_root: &Path,
    _ctx: &AfterRunContext,
) -> Vec<String> {
    #[cfg(feature = "python-plugins")]
    {
        python_impl::_run_hooks_python("after_run", _project_root, _ctx.scenario_id.as_str(),
            Some(&format!("{{'run_id': '{}', 'run_name': {}, 'report_dir': '{}', 'status': '{}'}}",
                _ctx.run_id,
                _ctx.run_name.as_deref().map(|s| format!("'{}'", s)).unwrap_or("None".into()),
                _ctx.report_dir.display(), _ctx.status,
            )))
    }
    #[cfg(not(feature = "python-plugins"))]
    { Vec::new() }
}

// ===========================================================================
// Static scan fallback (no Python required)
// ===========================================================================

/// Scan `.py` files for `@verify_plugin("name")` decorators.
fn _static_scan_verify_plugins(plugins_dir: &Path) -> Vec<String> {
    let mut names = Vec::new();
    if let Ok(rd) = std::fs::read_dir(plugins_dir) {
        for entry in rd.flatten() {
            let p = entry.path();
            if p.extension().and_then(|e| e.to_str()) != Some("py") {
                continue;
            }
            let Ok(text) = std::fs::read_to_string(&p) else {
                continue;
            };
            for line in text.lines() {
                let t = line.trim();
                if t.starts_with("@verify_plugin") {
                    if let Some(open) = t.find('(') {
                        let rest = &t[open + 1..];
                        let name = rest
                            .trim()
                            .trim_start_matches('"')
                            .trim_start_matches('\'')
                            .split(['"', '\'', ','])
                            .next()
                            .unwrap_or("")
                            .trim()
                            .to_string();
                        if !name.is_empty() {
                            names.push(name);
                        }
                    }
                }
            }
        }
    }
    names.sort();
    names.dedup();
    names
}

// ===========================================================================
// PyO3 implementations (only compiled with python-plugins feature)
// ===========================================================================

#[cfg(feature = "python-plugins")]
mod python_impl {
    use super::*;
    use pyo3::prelude::*;

    /// Load plugins via embedded Python interpreter.
    pub fn _load_plugins_python(
        project_root: &Path,
        plugins_dir: &Path,
        module_names: &[String],
    ) -> PluginLoadInfo {
        let mut info = PluginLoadInfo::default();

        // Build Python script that loads each plugin module
        let plugins_dir_s = plugins_dir.to_string_lossy();
        let project_root_s = project_root.to_string_lossy();

        let mut load_script = String::from(
            "import sys, importlib\n\
             _lks_loaded = []\n\
             _lks_errors = []\n\
             _lks_verify = []\n",
        );
        load_script.push_str(&format!(
            "sys.path.insert(0, r'{plugins_dir_s}')\n\
             sys.path.insert(1, r'{project_root_s}')\n"
        ));

        for name in module_names {
            let safe = name.replace('-', "_");
            load_script.push_str(&format!(
                "try:\n\
                 \x20   importlib.import_module('{safe}')\n\
                 \x20   _lks_loaded.append('{name}')\n\
                 except Exception as _e:\n\
                 \x20   _lks_errors.append('{name}: ' + type(_e).__name__ + ': ' + str(_e))\n"
            ));
        }

        load_script.push_str(
            "try:\n\
             \x20   from livekit_agent_simulator.plugins.registry import list_verify_plugins\n\
             \x20   _lks_verify = list_verify_plugins()\n\
             except Exception:\n\
             \x20   _lks_verify = []\n",
        );

        let result = Python::attach(|py| -> PyResult<()> {
            let locals = pyo3::types::PyDict::new(py);
            let script_c = std::ffi::CString::new(load_script.clone()).map_err(|e| {
                pyo3::exceptions::PyValueError::new_err(format!("invalid script: {e}"))
            })?;
            py.run(script_c.as_c_str(), None, Some(&locals))?;
            info.loaded = locals
                .get_item("_lks_loaded")?
                .ok_or_else(|| pyo3::exceptions::PyKeyError::new_err("_lks_loaded"))?
                .extract::<Vec<String>>()?;
            info.errors = locals
                .get_item("_lks_errors")?
                .ok_or_else(|| pyo3::exceptions::PyKeyError::new_err("_lks_errors"))?
                .extract::<Vec<String>>()?;
            info.verify_plugins = locals
                .get_item("_lks_verify")?
                .ok_or_else(|| pyo3::exceptions::PyKeyError::new_err("_lks_verify"))?
                .extract::<Vec<String>>()?;
            Ok(())
        });

        if let Err(e) = result {
            info.errors.push(format!("Python init error: {e}"));
            info.verify_plugins = _static_scan_verify_plugins(plugins_dir);
        }

        info
    }

    /// Execute a verify plugin via Python.
    /// Generic hook runner (before_run / after_run) — calls
    /// plugin_registry.{hook_name} for each loaded plugin.
    pub fn _run_hooks_python(
        hook_name: &str,
        _project_root: &Path,
        scenario_id: &str,
        extra_context: Option<&str>,
    ) -> Vec<String> {
        let mut results = Vec::new();
        let extra = extra_context.unwrap_or("{}");
        let script = format!(
            "import json
             from livekit_agent_simulator.plugins.registry import list_verify_plugins, get_verify
             for _name in list_verify_plugins():
                 try:
                     from importlib import import_module as _im
                     _mod = _im('livekit_agent_simulator.plugins.' + _name.replace('-','_'))
                     _fn = getattr(_mod, '{hook_name}', None)
                     if _fn is None:
                         continue
                     class _C: pass
                     _c = _C()
                     _c.scenario_id = '{scenario_id}'
                     _c.project_root = None
                     _fn(_c)
                 except Exception as _e:
                     pass
"
        );
        Python::attach(|py| -> PyResult<()> {
            let locals = pyo3::types::PyDict::new(py);
            let script_c = std::ffi::CString::new(script).map_err(|e|
                pyo3::exceptions::PyValueError::new_err(format!("invalid script: {e}"))
            )?;
            py.run(script_c.as_c_str(), None, Some(&locals))?;
            Ok(())
        }).ok();
        results
    }

    pub fn _run_verify_python(
        plugin_name: &str,
        ctx: &VerifyPluginContext,
    ) -> Option<VerifyPluginResult> {
        let plugin_name = plugin_name.to_string();
        let events_json = serde_json::to_string(&ctx.events).ok()?;
        let scenario_id = ctx.scenario_id.clone();

        let script = format!(
            "import json\n\
             from livekit_agent_simulator.plugins.registry import get_verify\n\
             _fn = get_verify('{plugin_name}')\n\
             if _fn is None:\n\
             \x20   _result = {{'pass': False, 'checks': []}}\n\
             else:\n\
             \x20   class _Ctx:\n\
             \x20   \x20   events = json.loads('{events_json}')\n\
             \x20   \x20   scenario_id = '{scenario_id}'\n\
             \x20   \x20   plugin_name = '{plugin_name}'\n\
             \x20   _raw = _fn(_Ctx())\n\
             \x20   _result = {{'pass': bool(_raw.get('pass')), 'checks': _raw.get('checks', [])}}\n"
        );

        let result = Python::attach(|py| -> PyResult<VerifyPluginResult> {
            let locals = pyo3::types::PyDict::new(py);
            let script_c = std::ffi::CString::new(script.clone()).map_err(|e| {
                pyo3::exceptions::PyValueError::new_err(format!("invalid script: {e}"))
            })?;
            py.run(script_c.as_c_str(), None, Some(&locals))?;
            // Use Python to extract pass + checks as JSON string directly
            let extract_script = std::ffi::CString::new(
                "import json; _out = json.dumps({'pass': bool(_result.get('pass', False)), 'checks': _result.get('checks', [])})"
            )
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("invalid extract script: {e}")))?;
            py.run(extract_script.as_c_str(), None, Some(&locals))?;
            let out_str = locals
                .get_item("_out")?
                .ok_or_else(|| pyo3::exceptions::PyKeyError::new_err("_out"))?
                .extract::<String>()?;
            let out_val: serde_json::Value =
                serde_json::from_str(&out_str).unwrap_or(serde_json::json!({}));
            let pass_val = out_val
                .get("pass")
                .and_then(|v| v.as_bool())
                .unwrap_or(false);
            let checks = out_val
                .get("checks")
                .cloned()
                .and_then(|v| serde_json::from_value(v).ok())
                .unwrap_or_default();
            Ok(VerifyPluginResult {
                pass: pass_val,
                checks,
            })
        });

        result.ok()
    }
}

#[cfg(feature = "python-plugins")]
use python_impl::{_load_plugins_python, _run_verify_python};
