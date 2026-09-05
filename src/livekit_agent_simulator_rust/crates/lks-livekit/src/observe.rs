//! Data-topic + `lk.agent.session` observation (port of `observer.py`
//! data-topic handling + `agent_session_observer.py` tool/session events).
//!
//! Two independent pipelines, mirroring Python:
//! - [`SessionObserver`] decodes the agent SDK's `lk.agent.session` byte
//!   stream (via `agent-sim-proto`) into `tool.start` / `tool.end` /
//!   `tool.error` / `handoff` / `session.*` events with call pairing.
//! - [`DataRouter`] handles every other data topic: `observe.data_topics`
//!   filtering, `tool_event_patterns` matching (data-plane tool events),
//!   `transcript_payload_types` parsing, and `data.message` / `data.raw`.

use std::collections::{HashMap, HashSet};

use agent_sim_proto::decode_agent_session_message;
use serde_json::{json, Map, Value as Json};

use lks_core::config::ObserveConfig;
use lks_core::logging::event::EventWriter;

pub const TOPIC_SESSION_MESSAGES: &str = "lk.agent.session";
pub const SESSION_SOURCE: &str = "lk.agent.session";

/// Paired tool.start bookkeeping (duration_ms + parent event id).
#[derive(Debug, Clone)]
pub struct OpenTool {
    pub event_id: String,
    pub ts_mono_ms: i64,
    pub name: String,
}

fn now_run_mono_ms(w: &EventWriter) -> i64 {
    (std::time::Instant::now() - w.run_start_mono()).as_millis() as i64
}

fn tool_key(call_id: Option<&str>, item_id: Option<&str>) -> Option<String> {
    call_id
        .filter(|s| !s.is_empty())
        .or(item_id.filter(|s| !s.is_empty()))
        .map(String::from)
}

/// Port of `agent_session_observer.py` — tool/session events from the agent's
/// `lk.agent.session` byte stream, with start/output call pairing.
#[derive(Default)]
pub struct SessionObserver {
    started_call_ids: HashSet<String>,
    open_tools: HashMap<String, OpenTool>,
    completed_call_ids: HashSet<String>,
    last_usage: Json,
}

impl SessionObserver {
    pub fn new() -> Self {
        Self::default()
    }

    /// Route one decoded AgentSessionMessage event map (proto output).
    pub fn handle_event(&mut self, event: &Map<String, Json>, w: &mut EventWriter) {
        // Oneof — exactly one of these keys is present.
        if let Some(started) = event.get("function_tools_started").and_then(|v| v.as_object()) {
            for call in started
                .get("function_calls")
                .and_then(|v| v.as_array())
                .into_iter()
                .flatten()
            {
                if let Some(call) = call.as_object() {
                    self.emit_tool_start(call, w);
                }
            }
            return;
        }
        if let Some(executed) = event.get("function_tools_executed").and_then(|v| v.as_object()) {
            self.handle_tools_executed(executed, w);
            return;
        }
        if let Some(update) = event.get("tool_execution_updated").and_then(|v| v.as_object()) {
            self.handle_tool_execution_updated(update, w);
            return;
        }
        if let Some(added) = event.get("conversation_item_added").and_then(|v| v.as_object()) {
            self.handle_conversation_item_added(added, w);
            return;
        }
        if let Some(changed) = event.get("agent_state_changed").and_then(|v| v.as_object()) {
            self.emit_session(
                "session.agent_state",
                serde_json::Value::Object(changed.clone()),
                w,
            );
            return;
        }
        if let Some(changed) = event.get("user_state_changed").and_then(|v| v.as_object()) {
            self.emit_session(
                "session.user_state",
                serde_json::Value::Object(changed.clone()),
                w,
            );
            return;
        }
        if event.get("session_usage_updated").is_some() {
            // Usage payloads are not decoded field-by-field; empty diff-guard
            // mirrors Python's _last_usage dedupe (nothing to compare → emit once).
            let usage = json!({});
            if usage == self.last_usage {
                return;
            }
            self.last_usage = usage.clone();
            self.emit_session("session.usage", usage, w);
            return;
        }
        if let Some(err) = event.get("error").and_then(|v| v.as_object()) {
            let msg = err
                .get("message")
                .cloned()
                .unwrap_or(Json::Null);
            self.emit_session("session.error", json!({"message": msg}), w);
        }
    }

    fn emit_session(&self, kind: &str, spec: Json, w: &mut EventWriter) {
        let spec_map = spec.as_object().cloned().unwrap_or_default();
        w.emit(kind, Some(&spec_map), SESSION_SOURCE, None, None, false, None);
    }

    /// Port of `_handle_tools_executed`: pair calls with outputs by index.
    fn handle_tools_executed(&mut self, executed: &Map<String, Json>, w: &mut EventWriter) {
        let calls = executed
            .get("function_calls")
            .and_then(|v| v.as_array())
            .cloned()
            .unwrap_or_default();
        let outputs = executed
            .get("function_call_outputs")
            .and_then(|v| v.as_array())
            .cloned()
            .unwrap_or_default();
        for (index, call_v) in calls.iter().enumerate() {
            let Some(call) = call_v.as_object() else { continue };
            let start = self.emit_tool_start(call, w);
            if index >= outputs.len() {
                continue;
            }
            let Some(output) = outputs[index].as_object() else { continue };
            let call_id = call.get("call_id").and_then(|v| v.as_str()).unwrap_or("");
            let output_call_id = output.get("call_id").and_then(|v| v.as_str()).unwrap_or("");
            if !call_id.is_empty() && !output_call_id.is_empty() && call_id != output_call_id {
                let spec = json!({
                    "where": "function_tools_executed",
                    "message": "call/output call_id mismatch; paired by array index",
                    "call_id": call_id,
                    "output_call_id": output_call_id,
                    "index": index,
                });
                w.emit(
                    "observer.warning",
                    Some(spec.as_object().unwrap()),
                    SESSION_SOURCE,
                    None,
                    None,
                    false,
                    None,
                );
            }
            let paired_key = tool_key(
                call.get("call_id").and_then(|v| v.as_str()),
                call.get("id").and_then(|v| v.as_str()),
            );
            self.emit_tool_output(output, start, paired_key.as_deref(), w);
        }
        for output_v in outputs.iter().skip(calls.len()) {
            if let Some(output) = output_v.as_object() {
                self.emit_tool_output(output, None, None, w);
            }
        }
    }

    /// Port of `_handle_tool_execution_updated`: promote progress events to
    /// tool.start/end so teardown races still record tools.
    fn handle_tool_execution_updated(&mut self, update: &Map<String, Json>, w: &mut EventWriter) {
        let update_map = serde_json::Value::Object(update.clone());
        self.emit_session("session.tool_execution", update_map, w);
        if let Some(started) = update.get("started").and_then(|v| v.as_object()) {
            if let Some(call) = started.get("function_call").and_then(|v| v.as_object()) {
                let name = call.get("name").and_then(|v| v.as_str()).unwrap_or("");
                let call_id = call.get("call_id").and_then(|v| v.as_str()).unwrap_or("");
                let id = call.get("id").and_then(|v| v.as_str()).unwrap_or("");
                if !name.is_empty() || !call_id.is_empty() || !id.is_empty() {
                    self.emit_tool_start(call, w);
                }
            }
            return;
        }
        if let Some(ended) = update.get("ended").and_then(|v| v.as_object()) {
            let call_id = ended.get("call_id").and_then(|v| v.as_str()).unwrap_or("");
            let item_id = ended.get("id").and_then(|v| v.as_str()).unwrap_or("");
            let key = tool_key(
                Some(call_id).filter(|s| !s.is_empty()),
                Some(item_id).filter(|s| !s.is_empty()),
            );
            if let Some(k) = &key {
                if self.completed_call_ids.contains(k) {
                    return;
                }
            }
            let start = key.as_ref().and_then(|k| self.open_tools.get(k).cloned());
            let name = start.as_ref().map(|s| s.name.clone()).unwrap_or_default();
            let status = ended.get("status").and_then(|v| v.as_str()).unwrap_or("");
            let is_error = status == "TC_ERROR" || status == "TC_CANCELLED";
            let output = json!({
                "id": ended.get("id").cloned().unwrap_or(json!("")),
                "call_id": ended.get("call_id").cloned().unwrap_or(json!("")),
                "name": if name.is_empty() { Json::Null } else { json!(name) },
                "output": ended
                    .get("message")
                    .cloned()
                    .filter(|v| !v.is_null())
                    .unwrap_or_else(|| json!(status)),
                "is_error": is_error,
            });
            self.emit_tool_output(
                output.as_object().unwrap(),
                start,
                key.as_deref(),
                w,
            );
        }
    }

    /// Port of `_handle_conversation_item_added`: function_call chat items as
    /// tool.start fallback; agent_handoff items as handoff events.
    fn handle_conversation_item_added(&mut self, added: &Map<String, Json>, w: &mut EventWriter) {
        let Some(item) = added.get("item").and_then(|v| v.as_object()) else {
            return;
        };
        match item.get("type").and_then(|v| v.as_str()) {
            Some("function_call") => {
                self.emit_tool_start(item, w);
            }
            Some("agent_handoff") => {
                self.emit_handoff(item, w);
            }
            _ => {}
        }
    }

    /// Port of `_emit_handoff`: agent→agent transfer; skip session bootstrap
    /// (empty old_agent_id) and no-op handoffs.
    fn emit_handoff(&self, handoff: &Map<String, Json>, w: &mut EventWriter) {
        let old_id = handoff.get("old_agent_id").and_then(|v| v.as_str()).unwrap_or("");
        let new_id = handoff.get("new_agent_id").and_then(|v| v.as_str()).unwrap_or("");
        if old_id.is_empty() || old_id == new_id {
            return;
        }
        let spec = json!({
            "id": handoff.get("id").cloned().unwrap_or(Json::Null),
            "old_agent_id": old_id,
            "new_agent_id": new_id,
            "created_at": Json::Null,
        });
        self.emit_session("handoff", spec, w);
    }

    /// Port of `_emit_tool_start` — dedupe by call key, track open tool.
    fn emit_tool_start(&mut self, call: &Map<String, Json>, w: &mut EventWriter) -> Option<OpenTool> {
        let spec = json!({
            "id": call.get("id").cloned().unwrap_or(Json::Null),
            "call_id": call.get("call_id").cloned().unwrap_or(Json::Null),
            "name": call.get("name").cloned().unwrap_or(Json::Null),
            "arguments": call.get("arguments").cloned().unwrap_or(json!("")),
        });
        let key = tool_key(
            call.get("call_id").and_then(|v| v.as_str()),
            call.get("id").and_then(|v| v.as_str()),
        );
        if let Some(k) = &key {
            if self.started_call_ids.contains(k) {
                return self.open_tools.get(k).cloned();
            }
        }
        let event = w.emit("tool.start", Some(spec.as_object().unwrap()), SESSION_SOURCE, None, None, false, None);
        let open = OpenTool {
            event_id: event.get("event_id").and_then(|v| v.as_str()).unwrap_or("").to_string(),
            ts_mono_ms: event.get("ts_mono_ms").and_then(|v| v.as_i64()).unwrap_or(0),
            name: call.get("name").and_then(|v| v.as_str()).unwrap_or("").to_string(),
        };
        if let Some(k) = &key {
            self.started_call_ids.insert(k.clone());
            self.open_tools.insert(k.clone(), open.clone());
        }
        Some(open)
    }

    /// Port of `_emit_tool_output` — tool.end / tool.error with duration + parent.
    fn emit_tool_output(
        &mut self,
        output: &Map<String, Json>,
        paired_start: Option<OpenTool>,
        paired_key: Option<&str>,
        w: &mut EventWriter,
    ) {
        let out_call_id = output.get("call_id").and_then(|v| v.as_str()).unwrap_or("");
        let out_id = output.get("id").and_then(|v| v.as_str()).unwrap_or("");
        let key = tool_key(
            Some(out_call_id).filter(|s| !s.is_empty()),
            Some(out_id).filter(|s| !s.is_empty()),
        );
        if let Some(k) = &key {
            if self.completed_call_ids.contains(k) {
                return;
            }
        }
        if let Some(pk) = paired_key {
            if self.completed_call_ids.contains(pk) {
                return;
            }
        }

        let start = paired_start.or_else(|| key.as_ref().and_then(|k| self.open_tools.remove(k)));
        if let Some(pk) = paired_key {
            self.open_tools.remove(pk);
        }
        let mut spec = json!({
            "id": output.get("id").cloned().unwrap_or(Json::Null),
            "call_id": output.get("call_id").cloned().unwrap_or(Json::Null),
            "name": output.get("name").cloned().unwrap_or(Json::Null),
            "output": output.get("output").cloned().unwrap_or(json!("")),
            "is_error": output.get("is_error").cloned().unwrap_or(json!(false)),
        });
        let is_error = output
            .get("is_error")
            .and_then(|v| v.as_bool())
            .unwrap_or(false);
        if spec["name"].as_str().map(str::is_empty).unwrap_or(true) {
            if let Some(s) = &start {
                spec["name"] = json!(s.name);
            }
        }
        let mut parent_id: Option<&str> = None;
        if let Some(s) = &start {
            spec["duration_ms"] = json!((now_run_mono_ms(w) - s.ts_mono_ms).max(0));
            parent_id = Some(s.event_id.as_str());
        }
        if is_error {
            spec["error"] = output.get("output").cloned().unwrap_or(Json::Null);
        }
        let kind = if is_error { "tool.error" } else { "tool.end" };
        w.emit(kind, Some(spec.as_object().unwrap()), SESSION_SOURCE, None, parent_id, false, None);
        if let Some(k) = &key {
            self.completed_call_ids.insert(k.clone());
        }
        if let Some(pk) = paired_key {
            self.completed_call_ids.insert(pk.to_string());
        }
    }
}

/// Dotted-path lookup (port of observer._lookup_path): "a.b" → payload["a"]["b"].
fn lookup_path<'a>(payload: &'a Json, path: &str) -> &'a Json {
    static NULL: std::sync::OnceLock<Json> = std::sync::OnceLock::new();
    let mut cur = payload;
    for part in path.split('.') {
        match cur.get(part) {
            Some(next) => cur = next,
            None => return NULL.get_or_init(|| Json::Null),
        }
    }
    cur
}

/// Port of `observer.py` data-topic handling.
pub struct DataRouter {
    observe: ObserveConfig,
    /// Open tool.start events from data-plane patterns (keyed by call_id).
    open_tools: HashMap<String, OpenTool>,
    /// Optional transcript sink (role, text, source) — wired by the caller
    /// bridges to their transcript pipeline. When absent, parsed transcript
    /// payloads are consumed (not emitted as data.message), mirroring Python.
    pub on_transcript: Option<Box<dyn Fn(&str, &str, &str) + Send>>,
}

impl DataRouter {
    pub fn new(observe: ObserveConfig) -> Self {
        Self {
            observe,
            open_tools: HashMap::new(),
            on_transcript: None,
        }
    }

    /// Handle one room data packet. Returns false when the topic was dropped
    /// by the `observe.data_topics` filter (Python drops silently).
    pub fn handle_data(
        &mut self,
        topic: &str,
        data: &[u8],
        sender: Option<&str>,
        w: &mut EventWriter,
    ) -> bool {
        if !self.observe.data_topics.is_empty() && !self.observe.data_topics.iter().any(|t| t == topic) {
            return false;
        }
        let source = if topic.is_empty() { "data" } else { topic };
        let payload: Json = match std::str::from_utf8(data)
            .ok()
            .and_then(|s| serde_json::from_str(s).ok())
        {
            Some(p) => p,
            None => {
                let spec = json!({"topic": topic, "bytes": data.len(), "sender": sender.map(String::from)});
                w.emit("data.raw", Some(spec.as_object().unwrap()), source, None, None, false, None);
                return true;
            }
        };

        if payload.is_object() && self.match_tool_patterns(topic, &payload, w) {
            return true;
        }
        if let Some((role, text)) = self.parse_transcript_payload(&payload) {
            if let Some(cb) = &self.on_transcript {
                cb(&role, &text, source);
            }
            return true;
        }
        let spec = json!({
            "topic": topic,
            "sender": sender.map(String::from),
            "payload": payload,
        });
        w.emit("data.message", Some(spec.as_object().unwrap()), source, None, None, true, None);
        true
    }

    /// Port of `_parse_transcript_payload` — generic transcript_turn shape.
    fn parse_transcript_payload(&self, payload: &Json) -> Option<(String, String)> {
        let ptype = payload.get("type").and_then(|v| v.as_str())?;
        if !self.observe.transcript_payload_types.iter().any(|t| t == ptype) {
            return None;
        }
        if payload.get("interim").and_then(|v| v.as_bool()).unwrap_or(false) {
            return None;
        }
        let turn = payload.get("turn").and_then(|v| v.as_object())?;
        let role = turn.get("role").and_then(|v| v.as_str())?;
        if role != "user" && role != "agent" {
            return None;
        }
        let text = turn.get("text").and_then(|v| v.as_str())?;
        let text = text.trim();
        if text.is_empty() {
            return None;
        }
        Some((role.to_string(), text.to_string()))
    }

    /// Port of `_match_tool_patterns` — first matching pattern wins.
    fn match_tool_patterns(&mut self, topic: &str, payload: &Json, w: &mut EventWriter) -> bool {
        for pattern in &self.observe.tool_event_patterns {
            if self.pattern_matches(pattern, topic, payload) {
                let emit = pattern.emit.clone();
                self.emit_tool_event(&emit, topic, payload, w);
                return true;
            }
        }
        false
    }

    fn pattern_matches(
        &self,
        pattern: &lks_core::config::ToolEventPattern,
        topic: &str,
        payload: &Json,
    ) -> bool {
        for (key, expected) in &pattern.mat {
            if key == "topic" {
                if topic != expected.as_str().unwrap_or("") {
                    return false;
                }
                continue;
            }
            if lookup_path(payload, key) != expected {
                return false;
            }
        }
        true
    }

    /// Port of `_emit_tool_event` — data-plane tool events with pairing.
    fn emit_tool_event(&mut self, emit_kind: &str, topic: &str, payload: &Json, w: &mut EventWriter) {
        let obj = payload.as_object();
        let name = obj
            .and_then(|o| o.get("tool").or_else(|| o.get("name")).cloned())
            .filter(|v| !v.is_null())
            .unwrap_or_else(|| lookup_path(payload, "spec.name").clone());
        let call_id = obj
            .and_then(|o| o.get("call_id").or_else(|| o.get("toolCallId")).cloned())
            .filter(|v| !v.is_null())
            .unwrap_or_else(|| lookup_path(payload, "spec.call_id").clone());
        let mut spec = json!({
            "name": name,
            "call_id": call_id,
            "payload": payload,
        });

        if emit_kind == "tool.start" {
            let event = w.emit("tool.start", Some(spec.as_object().unwrap()), topic, None, None, false, None);
            if let Some(cid) = call_id.as_str().filter(|s| !s.is_empty()) {
                self.open_tools.insert(
                    cid.to_string(),
                    OpenTool {
                        event_id: event.get("event_id").and_then(|v| v.as_str()).unwrap_or("").to_string(),
                        ts_mono_ms: event.get("ts_mono_ms").and_then(|v| v.as_i64()).unwrap_or(0),
                        name: name.as_str().unwrap_or("").to_string(),
                    },
                );
            }
            return;
        }

        let mut parent_id: Option<String> = None;
        if let Some(cid) = call_id.as_str().filter(|s| !s.is_empty()) {
            if let Some(start) = self.open_tools.remove(cid) {
                parent_id = Some(start.event_id);
                spec["duration_ms"] = json!((now_run_mono_ms(w) - start.ts_mono_ms).max(0));
            }
        }
        if emit_kind == "tool.error" {
            let err = obj
                .and_then(|o| o.get("error").or_else(|| o.get("message")).cloned())
                .filter(|v| !v.is_null())
                .unwrap_or_else(|| lookup_path(payload, "spec.error").clone());
            spec["error"] = err;
        }
        let parent_ref = parent_id.as_deref();
        w.emit(emit_kind, Some(spec.as_object().unwrap()), topic, None, parent_ref, false, None);
    }
}

/// Decode + route a room data packet that may be an `lk.agent.session` byte
/// stream. Returns true when the packet was consumed as a session message.
pub fn handle_session_bytes(
    session: &mut SessionObserver,
    data: &[u8],
    w: &mut EventWriter,
) -> bool {
    match decode_agent_session_message(data) {
        Ok(msg) => {
            if let Some(event) = msg.get("event").and_then(|v| v.as_object()) {
                session.handle_event(event, w);
            }
            true
        }
        Err(_) => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn tmp_writer() -> (tempfile::TempDir, EventWriter) {
        let dir = tempfile::tempdir().unwrap();
        let w = EventWriter::new("r", dir.path().to_path_buf(), "UTC", 2500).unwrap();
        (dir, w)
    }

    fn observe_cfg() -> ObserveConfig {
        let mut c = ObserveConfig::default();
        c.transcript_payload_types = vec!["transcript_turn".into()];
        c
    }

    #[test]
    fn data_router_topic_filter_and_message() {
        let (dir, mut w) = tmp_writer();
        let mut cfg = observe_cfg();
        cfg.data_topics = vec!["flow.events".into()];
        let mut router = DataRouter::new(cfg);
        // Dropped topic.
        assert!(!router.handle_data("other", br#"{"a":1}"#, Some("agent"), &mut w));
        // Accepted topic → data.message with payload.
        assert!(router.handle_data("flow.events", br#"{"node":"booking"}"#, Some("agent"), &mut w));
        let kinds: Vec<&str> = w.events().iter().filter_map(|e| e.get("kind").and_then(|v| v.as_str())).collect();
        assert!(kinds.contains(&"data.message"), "{kinds:?}");
        let msg = w.events().iter().find(|e| e.get("kind").and_then(|v| v.as_str()) == Some("data.message")).unwrap();
        assert_eq!(msg["source"], json!("flow.events"));
        assert_eq!(msg["spec"]["payload"]["node"], json!("booking"));
        drop(dir);
    }

    #[test]
    fn data_router_raw_fallback() {
        let (dir, mut w) = tmp_writer();
        let mut router = DataRouter::new(observe_cfg());
        assert!(router.handle_data("t", &[0xff, 0xfe], None, &mut w));
        assert!(w.events().iter().any(|e| e.get("kind").and_then(|v| v.as_str()) == Some("data.raw")));
        drop(dir);
    }

    #[test]
    fn data_router_transcript_payload_goes_to_sink_not_data_message() {
        let (dir, mut w) = tmp_writer();
        let mut router = DataRouter::new(observe_cfg());
        let seen = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
        let sink = seen.clone();
        router.on_transcript = Some(Box::new(move |role, text, _src| {
            sink.lock().unwrap().push((role.to_string(), text.to_string()));
        }));
        let payload = br#"{"type":"transcript_turn","turn":{"role":"agent","text":"hello there"}}"#;
        assert!(router.handle_data("t", payload, None, &mut w));
        assert_eq!(seen.lock().unwrap().len(), 1);
        assert!(!w.events().iter().any(|e| e.get("kind").and_then(|v| v.as_str()) == Some("data.message")));
        // Interim payloads are ignored entirely.
        let interim = br#"{"type":"transcript_turn","interim":true,"turn":{"role":"agent","text":"x"}}"#;
        assert!(router.handle_data("t", interim, None, &mut w));
        assert_eq!(seen.lock().unwrap().len(), 1);
        drop(dir);
    }

    #[test]
    fn data_router_tool_pattern_pairing() {
        let (dir, mut w) = tmp_writer();
        let mut cfg = observe_cfg();
        let mut m = Map::new();
        m.insert("type".into(), json!("tool_started"));
        cfg.tool_event_patterns = vec![lks_core::config::ToolEventPattern {
            mat: m.clone(),
            emit: "tool.start".into(),
        }];
        let mut m2 = Map::new();
        m2.insert("type".into(), json!("tool_finished"));
        cfg.tool_event_patterns.push(lks_core::config::ToolEventPattern {
            mat: m2,
            emit: "tool.end".into(),
        });
        let mut router = DataRouter::new(cfg);
        router.handle_data("tools", br#"{"type":"tool_started","name":"search","call_id":"c9"}"#, None, &mut w);
        router.handle_data("tools", br#"{"type":"tool_finished","name":"search","call_id":"c9"}"#, None, &mut w);
        let kinds: Vec<String> = w
            .events()
            .iter()
            .filter_map(|e| e.get("kind").and_then(|v| v.as_str()).map(String::from))
            .collect();
        assert!(kinds.contains(&"tool.start".to_string()) && kinds.contains(&"tool.end".to_string()), "{kinds:?}");
        let end = w.events().iter().find(|e| e.get("kind").and_then(|v| v.as_str()) == Some("tool.end")).unwrap();
        assert_eq!(end["spec"]["name"], json!("search"));
        assert!(end["spec"]["duration_ms"].as_i64().is_some());
        assert!(!end["parent_event_id"].as_str().unwrap_or("").is_empty());
        drop(dir);
    }

    #[test]
    fn session_observer_emits_tool_events_with_pairing() {
        let (dir, mut w) = tmp_writer();
        let mut session = SessionObserver::new();
        // function_tools_started with one call.
        let started = json!({"function_tools_started": {"function_calls": [
            {"id": "c1", "call_id": "call-1", "name": "get_weather", "arguments": "{}"}
        ]}});
        session.handle_event(started.as_object().unwrap(), &mut w);
        // function_tools_executed with the output.
        let executed = json!({"function_tools_executed": {
            "function_calls": [{"id": "c1", "call_id": "call-1", "name": "get_weather", "arguments": "{}"}],
            "function_call_outputs": [{"id": "c1", "call_id": "call-1", "name": "", "output": "22C", "is_error": false}],
        }});
        session.handle_event(executed.as_object().unwrap(), &mut w);
        let kinds: Vec<String> = w
            .events()
            .iter()
            .filter_map(|e| e.get("kind").and_then(|v| v.as_str()).map(String::from))
            .collect();
        assert!(kinds.contains(&"tool.start".to_string()));
        assert!(kinds.contains(&"tool.end".to_string()));
        assert_eq!(kinds.iter().filter(|k| **k == "tool.start".to_string()).count(), 1, "no duplicate start");
        let end = w.events().iter().find(|e| e.get("kind").and_then(|v| v.as_str()) == Some("tool.end")).unwrap();
        assert_eq!(end["spec"]["output"], json!("22C"));
        assert!(end["spec"]["duration_ms"].as_i64().is_some());
        assert_eq!(end["source"], json!("lk.agent.session"));
        drop(dir);
    }

    #[test]
    fn session_observer_handoff_skips_bootstrap() {
        let (dir, mut w) = tmp_writer();
        let mut session = SessionObserver::new();
        // Bootstrap: empty old_agent_id → skipped.
        let boot = json!({"conversation_item_added": {"item": {"type": "agent_handoff", "id": "h", "old_agent_id": "", "new_agent_id": "b"}}});
        session.handle_event(boot.as_object().unwrap(), &mut w);
        assert!(!w.events().iter().any(|e| e.get("kind").and_then(|v| v.as_str()) == Some("handoff")));
        // Real transfer.
        let real = json!({"conversation_item_added": {"item": {"type": "agent_handoff", "id": "h", "old_agent_id": "a", "new_agent_id": "b"}}});
        session.handle_event(real.as_object().unwrap(), &mut w);
        let h = w.events().iter().find(|e| e.get("kind").and_then(|v| v.as_str()) == Some("handoff")).unwrap();
        assert_eq!(h["spec"]["old_agent_id"], json!("a"));
        assert_eq!(h["spec"]["new_agent_id"], json!("b"));
        drop(dir);
    }

    #[test]
    fn session_observer_tool_execution_updated_ended_error() {
        let (dir, mut w) = tmp_writer();
        let mut session = SessionObserver::new();
        // ended without a paired start → still emits tool.error with status text.
        let upd = json!({"tool_execution_updated": {"ended": {"id": "i1", "call_id": "cx", "status": "TC_ERROR"}}});
        session.handle_event(upd.as_object().unwrap(), &mut w);
        let e = w.events().iter().find(|e| e.get("kind").and_then(|v| v.as_str()) == Some("tool.error")).unwrap();
        assert_eq!(e["spec"]["output"], json!("TC_ERROR"));
        assert_eq!(e["spec"]["error"], json!("TC_ERROR"));
        drop(dir);
    }
}
