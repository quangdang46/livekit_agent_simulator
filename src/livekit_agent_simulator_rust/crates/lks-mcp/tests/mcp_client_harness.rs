//! MCP client harness (P5): spawn the real `lksr mcp` binary over stdio (the
//! exact production entry the installer wires) and drive it with a minimal
//! JSON-RPC client over the process pipes — deterministic, no transport
//! library quirks. Asserts the 21-tool surface + data-plane behavior against a
//! temp `.agent-sim/` root.

use serde_json::{json, Value};
use std::io::{BufRead, BufReader, Write};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};

/// Path to the built `lksr` binary (workspace target dir).
fn lksr_bin() -> PathBuf {
    let mut p = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    p.pop(); // crates
    p.pop(); // livekit_agent_simulator_rust
    p.push("target");
    p.push("debug");
    p.push("lksr");
    p
}

/// Spawn `lksr mcp` and return (child, stdin writer, stdout reader).
fn spawn_mcp() -> (
    Child,
    std::process::ChildStdin,
    BufReader<std::process::ChildStdout>,
) {
    let bin = lksr_bin();
    let mut child = Command::new(&bin)
        .arg("mcp")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .expect("spawn lksr mcp");
    let stdin = child.stdin.take().expect("child stdin");
    let stdout = child.stdout.take().expect("child stdout");
    (child, stdin, BufReader::new(stdout))
}

/// Minimal JSON-RPC client over the pipes.
struct RpcClient {
    _child: Child,
    stdin: std::process::ChildStdin,
    stdout: BufReader<std::process::ChildStdout>,
    next_id: u64,
}

impl RpcClient {
    fn new() -> Self {
        let (child, stdin, stdout) = spawn_mcp();
        let mut c = Self {
            _child: child,
            stdin,
            stdout,
            next_id: 1,
        };
        c.request(
            "initialize",
            json!({"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "harness", "version": "0"}}),
        );
        c.notify("notifications/initialized", Value::Null);
        c
    }

    fn request(&mut self, method: &str, params: Value) -> Value {
        let id = self.next_id;
        self.next_id += 1;
        let msg = json!({"jsonrpc": "2.0", "id": id, "method": method, "params": params});
        self.send(&msg);
        loop {
            let line = self.read_line();
            let v: Value = serde_json::from_str(&line).expect("valid jsonrpc line");
            if v.get("id") == Some(&json!(id)) {
                return v;
            }
            // ignore notifications / other responses
        }
    }

    fn notify(&mut self, method: &str, params: Value) {
        let msg = json!({"jsonrpc": "2.0", "method": method, "params": params});
        self.send(&msg);
    }

    fn send(&mut self, msg: &Value) {
        let line = serde_json::to_string(msg).unwrap();
        self.stdin.write_all(line.as_bytes()).unwrap();
        self.stdin.write_all(b"\n").unwrap();
        self.stdin.flush().unwrap();
    }

    fn read_line(&mut self) -> String {
        let mut line = String::new();
        self.stdout.read_line(&mut line).expect("read jsonrpc line");
        line
    }

    fn list_tools(&mut self) -> Vec<Value> {
        let r = self.request("tools/list", json!({}));
        r["result"]["tools"].as_array().cloned().unwrap_or_default()
    }

    /// Call a tool; returns the parsed JSON text result (tools return JSON text).
    fn call(&mut self, name: &str, args: Value) -> Value {
        let r = self.request("tools/call", json!({"name": name, "arguments": args}));
        let content = r["result"]["content"]
            .as_array()
            .cloned()
            .unwrap_or_default();
        let text: String = content
            .iter()
            .filter_map(|b| b.get("text").and_then(|t| t.as_str()).map(String::from))
            .collect::<Vec<_>>()
            .join("");
        serde_json::from_str(&text).unwrap_or_else(|_| json!({"raw": text}))
    }
}

/// Scaffold a temp `.agent-sim/` root with a minimal config + one scenario.
fn temp_root() -> (tempfile::TempDir, PathBuf) {
    let dir = tempfile::tempdir().expect("tempdir");
    let root = dir.path().to_path_buf();
    let dot = root.join(".agent-sim");
    std::fs::create_dir_all(dot.join("scenarios")).unwrap();
    std::fs::create_dir_all(dot.join("reports")).unwrap();
    std::fs::create_dir_all(dot.join("plugins")).unwrap();
    std::fs::create_dir_all(dot.join("cues")).unwrap();
    std::fs::write(
        dot.join("config.yaml"),
        "livekit:\n  url: ws://localhost:7880\n  api_key: test-key-0123456789abcdef\n  api_secret: test-secret-0123456789abcdef\n  agent_name: test-agent\nsimulator:\n  provider: google\n  mode: realtime\n  api_key: test-sim-key-0123456789abcdef\n",
    )
    .unwrap();
    std::fs::write(
        dot.join("scenarios").join("smoke.yaml"),
        "apiVersion: agent-sim/v1\nkind: Scenario\nmetadata:\n  id: smoke\npersona:\n  brief: Test caller brief\n  goals:\n    - Say hello\n",
    )
    .unwrap();
    (dir, root)
}

#[test]
fn tool_surface_matches_python() {
    let mut c = RpcClient::new();
    let tools = c.list_tools();

    let mut names: Vec<String> = tools
        .iter()
        .filter_map(|t| t["name"].as_str().map(String::from))
        .collect();
    names.sort();
    let mut expected: Vec<String> = [
        "guide",
        "web",
        "init_project",
        "preflight",
        "list_scenarios",
        "list_plugins",
        "list_cues",
        "validate_scenario",
        "export_scenario",
        "init_scenario",
        "convert_scenario",
        "execute_scenario",
        "optimize_persona",
        "execute_scenarios",
        "execute_scenario_dict",
        "scenario_from_run",
        "get_run_status",
        "get_run_log",
        "get_run_report",
        "compare_runs",
        "list_runs",
    ]
    .iter()
    .map(|s| s.to_string())
    .collect();
    expected.sort();
    assert_eq!(names, expected, "tool names must match Python exactly");
    assert_eq!(names.len(), 21, "exactly 21 tools");

    let required: std::collections::HashMap<&str, Vec<String>> = [
        ("guide", vec![]),
        ("web", vec!["project_root"]),
        ("init_project", vec!["project_root"]),
        ("preflight", vec!["project_root"]),
        ("list_scenarios", vec!["project_root"]),
        ("list_plugins", vec!["project_root"]),
        ("list_cues", vec!["project_root"]),
        ("validate_scenario", vec!["project_root", "scenario_id"]),
        ("export_scenario", vec!["project_root", "scenario_id"]),
        ("init_scenario", vec!["project_root", "scenario_id"]),
        ("convert_scenario", vec!["project_root", "scenario_id"]),
        ("execute_scenario", vec!["project_root", "scenario_id"]),
        ("optimize_persona", vec!["project_root", "scenario_ids"]),
        ("execute_scenarios", vec!["project_root"]),
        ("execute_scenario_dict", vec!["project_root", "scenario"]),
        ("scenario_from_run", vec!["project_root", "run_id"]),
        ("get_run_status", vec!["project_root", "run_id"]),
        ("get_run_log", vec!["project_root", "run_id"]),
        ("get_run_report", vec!["project_root", "run_id"]),
        ("compare_runs", vec!["project_root", "run_id_a", "run_id_b"]),
        ("list_runs", vec!["project_root"]),
    ]
    .iter()
    .map(|(n, r)| (*n, r.iter().map(|s| s.to_string()).collect()))
    .collect();

    for tool in &tools {
        let name = tool["name"].as_str().unwrap_or("").to_string();
        let mut req: Vec<String> = tool["inputSchema"]["required"]
            .as_array()
            .map(|r| {
                r.iter()
                    .filter_map(|x| x.as_str().map(String::from))
                    .collect()
            })
            .unwrap_or_default();
        req.sort();
        let exp = required.get(name.as_str()).expect("known tool");
        assert_eq!(&req, exp, "required params for {name}");
    }
}

#[test]
fn data_plane_tools_work_end_to_end() {
    let (_dir, root) = temp_root();
    let root_s = root.to_string_lossy().into_owned();
    let mut c = RpcClient::new();

    // guide (no project_root)
    let g = c.call("guide", json!({}));
    assert!(
        g.get("text").and_then(|v| v.as_str()).is_some(),
        "guide returns text"
    );
    assert!(
        g.get("path").and_then(|v| v.as_str()).is_some(),
        "guide returns path"
    );

    // init_project is idempotent (already scaffolded)
    let init = c.call("init_project", json!({"project_root": root_s}));
    assert!(
        init.get("created").is_some() || init.get("error").is_some(),
        "init_project returns a result"
    );

    // list_scenarios
    let list = c.call("list_scenarios", json!({"project_root": root_s}));
    let arr = list.as_array().expect("list_scenarios is an array");
    assert!(arr
        .iter()
        .any(|s| s.get("id").and_then(|v| v.as_str()) == Some("smoke")));

    // validate_scenario
    let v = c.call(
        "validate_scenario",
        json!({"project_root": root_s, "scenario_id": "smoke"}),
    );
    assert_eq!(
        v.get("valid").and_then(|x| x.as_bool()),
        Some(true),
        "smoke validates: {v}"
    );

    // export_scenario — Python shape: {found, apiVersion, kind, metadata{id,...}, persona, ...}
    let e = c.call(
        "export_scenario",
        json!({"project_root": root_s, "scenario_id": "smoke"}),
    );
    assert_eq!(e.get("found").and_then(|x| x.as_bool()), Some(true));
    assert_eq!(
        e["metadata"]["id"].as_str(),
        Some("smoke"),
        "metadata.id present: {e}"
    );

    // get_run_status on a missing run → found:false (no panic)
    let st = c.call(
        "get_run_status",
        json!({"project_root": root_s, "run_id": "001-nope"}),
    );
    assert_eq!(st.get("found").and_then(|x| x.as_bool()), Some(false));

    // execute_scenario → explicit not-implemented error (fail-loud until P2)
    let ex = c.call(
        "execute_scenario",
        json!({"project_root": root_s, "scenario_id": "smoke"}),
    );
    assert!(
        ex.get("error")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .contains("not available in the Rust build"),
        "execute_scenario fails loud: {ex}"
    );
}

#[test]
fn compare_runs_missing_runs_reports_error() {
    let (_dir, root) = temp_root();
    let root_s = root.to_string_lossy().into_owned();
    let mut c = RpcClient::new();
    let r = c.call(
        "compare_runs",
        json!({"project_root": root_s, "run_id_a": "001-a", "run_id_b": "001-b", "baseline": true}),
    );
    assert_eq!(
        r.get("error").and_then(|v| v.as_str()),
        Some("one or both runs not found")
    );
    assert_eq!(
        r.get("gate")
            .and_then(|v| v.as_object())
            .and_then(|g| g.get("ok"))
            .and_then(|x| x.as_bool()),
        Some(false)
    );
}
