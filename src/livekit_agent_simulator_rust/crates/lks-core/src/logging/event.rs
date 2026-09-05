//! Canonical event envelope + per-run report folder (port of `event_writer.py`).
//!
//! Every event carries: event_id, seq, run_id, turn, kind, ts (epoch ms),
//! ts_mono_ms (ms since run start), datetime_utc, datetime_local, source,
//! parent_event_id, dialogue snapshot, and a kind-specific spec.
//! Artifacts: events.jsonl (append-only, one envelope per line, flushed every emit).

use std::io::Write;
use std::path::PathBuf;

use serde_json::{json, Map, Value as Json};

use crate::metrics::compute_voice_metrics;

/// Percentile (Python `_percentile`: nearest-rank with round).
fn percentile(values: &[f64], pct: f64) -> Option<f64> {
    if values.is_empty() {
        return None;
    }
    let mut sorted = values.to_vec();
    sorted.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    let idx = (((pct / 100.0) * (sorted.len() as f64 - 1.0)).round()) as usize;
    let idx = idx.clamp(0, sorted.len() - 1);
    Some(sorted[idx])
}

fn epoch_ms(now: &jiff::Zoned) -> i64 {
    now.timestamp().as_millisecond()
}

fn iso_utc(now: &jiff::Zoned) -> String {
    now.strftime("%Y-%m-%dT%H:%M:%S%.3fZ").to_string()
}

/// EventWriter — mirror of event_writer.py. Pure Rust, byte-compatible envelope.
pub struct EventWriter {
    run_id: String,
    report_dir: PathBuf,
    tz_name: String,
    warn_ms: i64,
    seq: i64,
    t0_mono: std::time::Instant,
    events_file: std::fs::File,
    events: Vec<Map<String, Json>>,
    turn: i64,
    dialogue_user: Map<String, Json>,
    dialogue_agent: Map<String, Json>,
}

fn fresh_dialogue(role: &str) -> Map<String, Json> {
    let mut m = Map::new();
    m.insert("text".into(), Json::Null);
    m.insert("final".into(), json!(false));
    m.insert("at_ms".into(), Json::Null);
    let _ = role;
    m
}

impl EventWriter {
    pub fn new(
        run_id: &str,
        report_dir: PathBuf,
        timezone_name: &str,
        turn_taking_warn_ms: i64,
    ) -> Result<Self, std::io::Error> {
        std::fs::create_dir_all(&report_dir)?;
        let events_path = report_dir.join("events.jsonl");
        let events_file = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&events_path)?;
        Ok(EventWriter {
            run_id: run_id.to_string(),
            report_dir,
            tz_name: timezone_name.to_string(),
            warn_ms: turn_taking_warn_ms,
            seq: 0,
            t0_mono: std::time::Instant::now(),
            events_file,
            events: Vec::new(),
            turn: 0,
            dialogue_user: fresh_dialogue("user"),
            dialogue_agent: fresh_dialogue("agent"),
        })
    }

    pub fn t0_mono(&self) -> std::time::Instant {
        self.t0_mono
    }
    pub fn run_start_mono(&self) -> std::time::Instant {
        self.t0_mono
    }
    pub fn events(&self) -> &Vec<Map<String, Json>> {
        &self.events
    }
    pub fn current_turn(&self) -> i64 {
        self.turn
    }

    /// Keep the latest utterance per role.
    pub fn update_dialogue(&mut self, role: &str, text: &str, final_: bool, at_ms: Option<i64>) {
        let at = at_ms.unwrap_or_else(|| jiff::Zoned::now().timestamp().as_millisecond());
        let d = if role == "agent" {
            &mut self.dialogue_agent
        } else {
            &mut self.dialogue_user
        };
        d.insert("text".into(), json!(text));
        d.insert("final".into(), json!(final_));
        d.insert("at_ms".into(), json!(at));
    }

    /// New turn: keep the user utterance, clear stale agent reply.
    pub fn begin_turn(&mut self, turn: i64) {
        self.turn = turn;
        self.dialogue_agent = fresh_dialogue("agent");
    }

    fn dialogue_snapshot(&self) -> Map<String, Json> {
        let user = {
            let mut d = self.dialogue_user.clone();
            if d.get("text").and_then(|v| v.as_str()).is_none() {
                d.insert("note".into(), json!("user has not spoken yet this turn"));
            }
            d
        };
        let agent = {
            let mut d = self.dialogue_agent.clone();
            if d.get("text").and_then(|v| v.as_str()).is_none() {
                d.insert("note".into(), json!("agent has not spoken yet this turn"));
            }
            d
        };
        let mut snap = Map::new();
        snap.insert("user".into(), Json::Object(user));
        snap.insert("agent".into(), Json::Object(agent));
        snap
    }

    /// Emit an event, append + flush to events.jsonl. Returns the event dict.
    #[allow(clippy::too_many_arguments)] // mirrors Python emit(kind, spec, source, turn, parent, include_dialogue, ts_mono_ms)
    pub fn emit(
        &mut self,
        kind: &str,
        spec: Option<&Map<String, Json>>,
        source: &str,
        turn: Option<i64>,
        parent_event_id: Option<&str>,
        include_dialogue: bool,
        ts_mono_ms: Option<i64>,
    ) -> Map<String, Json> {
        self.seq += 1;
        let now = jiff::Zoned::now();
        let mut mono = (self.t0_mono.elapsed().as_millis() as i64).max(0);
        if let Some(t) = ts_mono_ms {
            mono = t.max(0);
        }
        let tz = jiff::tz::TimeZone::get(&self.tz_name).unwrap_or(jiff::tz::TimeZone::UTC);
        let local = now.with_time_zone(tz);

        let mut event = Map::new();
        event.insert("event_id".into(), json!(format!("evt_{}", uuid_v4_hex12())));
        event.insert("seq".into(), json!(self.seq));
        event.insert("run_id".into(), json!(self.run_id));
        event.insert("turn".into(), json!(turn.unwrap_or(self.turn)));
        event.insert("kind".into(), json!(kind));
        event.insert("ts".into(), json!(epoch_ms(&now)));
        event.insert("ts_mono_ms".into(), json!(mono));
        event.insert("datetime_utc".into(), json!(iso_utc(&now)));
        event.insert(
            "datetime_local".into(),
            json!(local.strftime("%Y-%m-%dT%H:%M:%S%.3f%:z").to_string()),
        );
        event.insert("source".into(), json!(source));
        event.insert(
            "parent_event_id".into(),
            parent_event_id.map(|s| json!(s)).unwrap_or(Json::Null),
        );
        if include_dialogue {
            event.insert("dialogue".into(), Json::Object(self.dialogue_snapshot()));
        }
        event.insert(
            "spec".into(),
            spec.cloned()
                .map(Json::Object)
                .unwrap_or(Json::Object(Map::new())),
        );

        let line = serde_json::to_string(&event).unwrap_or_default() + "\n";
        let _ = self.events_file.write_all(line.as_bytes());
        let _ = self.events_file.flush();
        self.events.push(event.clone());
        event
    }

    /// Per-turn rows from the event stream (mirror turn_metrics).
    pub fn turn_metrics(&self) -> Vec<Map<String, Json>> {
        let mut by_turn: std::collections::BTreeMap<i64, Map<String, Json>> = Default::default();
        for e in &self.events {
            let t = e.get("turn").and_then(|v| v.as_i64()).unwrap_or(0);
            if t <= 0 {
                continue;
            }
            let row = by_turn.entry(t).or_insert_with(|| {
                let mut r = Map::new();
                r.insert("turn".into(), json!(t));
                r.insert("user_text".into(), Json::Null);
                r.insert("agent_text".into(), Json::Null);
                r.insert("turn_taking_ms".into(), Json::Null);
                r.insert("tool_count".into(), json!(0));
                r.insert("tool_errors".into(), json!(0));
                r.insert("interrupted".into(), json!(false));
                r
            });
            let kind = e.get("kind").and_then(|v| v.as_str()).unwrap_or("");
            let spec = e.get("spec").and_then(|v| v.as_object());
            match kind {
                "transcript.user.final" => {
                    if let Some(s) = spec {
                        let text = s.get("text").cloned().unwrap_or(Json::Null);
                        let same_turn = s
                            .get("same_turn")
                            .and_then(|v| v.as_bool())
                            .unwrap_or(false);
                        let prior = row.get("user_text").cloned().unwrap_or(Json::Null);
                        match (same_turn, prior.as_str(), text.as_str()) {
                            // Split-utterance continuation — append, don't
                            // overwrite, so the full utterance is visible to
                            // latency/judge checks.
                            (true, Some(p), Some(t)) if !p.is_empty() => {
                                row.insert("user_text".into(), json!(format!("{p} {t}").trim()));
                            }
                            _ => {
                                row.insert("user_text".into(), text);
                            }
                        }
                    }
                }
                "transcript.agent.final" => {
                    if let Some(s) = spec {
                        row.insert(
                            "agent_text".into(),
                            s.get("text").cloned().unwrap_or(Json::Null),
                        );
                        if let Some(ttm) = s.get("turn_taking_ms") {
                            row.insert("turn_taking_ms".into(), ttm.clone());
                        }
                    }
                }
                "tool.start" => {
                    let n = row.get("tool_count").and_then(|v| v.as_i64()).unwrap_or(0) + 1;
                    row.insert("tool_count".into(), json!(n));
                }
                "tool.error" => {
                    let n = row.get("tool_errors").and_then(|v| v.as_i64()).unwrap_or(0) + 1;
                    row.insert("tool_errors".into(), json!(n));
                }
                "interruption" => {
                    row.insert("interrupted".into(), json!(true));
                }
                _ => {}
            }
        }
        by_turn.into_values().collect()
    }

    /// Compute summary + emit run.ended + write summary/meta/timeline/review.
    pub fn finalize(
        &mut self,
        status: &str,
        meta: Option<&Map<String, Json>>,
        verdict: Option<&Map<String, Json>>,
    ) -> Map<String, Json> {
        let turns = self.turn_metrics();
        let turn_taking: Vec<f64> = turns
            .iter()
            .filter_map(|t| t.get("turn_taking_ms").and_then(|v| v.as_f64()))
            .collect();
        let tool_errors = self
            .events
            .iter()
            .filter(|e| e.get("kind").and_then(|v| v.as_str()) == Some("tool.error"))
            .count();
        let tool_calls = self
            .events
            .iter()
            .filter(|e| e.get("kind").and_then(|v| v.as_str()) == Some("tool.start"))
            .count();
        let interruptions = self
            .events
            .iter()
            .filter(|e| e.get("kind").and_then(|v| v.as_str()) == Some("interruption"))
            .count();
        let silences = self
            .events
            .iter()
            .filter(|e| e.get("kind").and_then(|v| v.as_str()) == Some("silence.detected"))
            .count();
        let voice_metrics = compute_voice_metrics(&self.events);
        let tt_block = voice_metrics
            .get("turn_taking_ms")
            .and_then(|v| v.as_object());

        let duration_ms = self.t0_mono.elapsed().as_millis() as i64;
        let turn_count = self
            .events
            .iter()
            .filter_map(|e| e.get("turn").and_then(|v| v.as_i64()))
            .max()
            .unwrap_or(0);

        let mut summary = Map::new();
        summary.insert("run_id".into(), json!(self.run_id));
        summary.insert("status".into(), json!(status));
        summary.insert("duration_ms".into(), json!(duration_ms));
        summary.insert("turn_count".into(), json!(turn_count));
        summary.insert("event_count".into(), json!(self.seq + 1));
        let mut tt = Map::new();
        tt.insert(
            "p50".into(),
            tt_block
                .and_then(|b| b.get("p50"))
                .cloned()
                .or_else(|| percentile(&turn_taking, 50.0).map(|v| json!(v)))
                .unwrap_or(Json::Null),
        );
        tt.insert(
            "p95".into(),
            tt_block
                .and_then(|b| b.get("p95"))
                .cloned()
                .or_else(|| percentile(&turn_taking, 95.0).map(|v| json!(v)))
                .unwrap_or(Json::Null),
        );
        tt.insert(
            "p99".into(),
            tt_block
                .and_then(|b| b.get("p99"))
                .cloned()
                .unwrap_or(Json::Null),
        );
        tt.insert(
            "max".into(),
            tt_block
                .and_then(|b| b.get("max"))
                .cloned()
                .or_else(|| {
                    turn_taking
                        .iter()
                        .cloned()
                        .reduce(f64::max)
                        .map(|v| json!(v))
                })
                .unwrap_or(Json::Null),
        );
        tt.insert(
            "count".into(),
            tt_block
                .and_then(|b| b.get("count"))
                .cloned()
                .unwrap_or(json!(turn_taking.len())),
        );
        summary.insert("turn_taking_ms".into(), Json::Object(tt));
        summary.insert("metrics".into(), Json::Object(voice_metrics));
        summary.insert("tool_calls".into(), json!(tool_calls));
        summary.insert("tool_errors".into(), json!(tool_errors));
        summary.insert("interruptions".into(), json!(interruptions));
        summary.insert("silences".into(), json!(silences));
        summary.insert(
            "verdict".into(),
            verdict
                .map(|v| Json::Object(v.clone()))
                .unwrap_or(Json::Null),
        );
        summary.insert(
            "turns".into(),
            Json::Array(turns.into_iter().map(Json::Object).collect()),
        );

        let digest = json!({
            "turn_count": turn_count,
            "tool_errors": tool_errors,
            "duration_ms": duration_ms,
        });
        let mut rs = Map::new();
        rs.insert("status".into(), json!(status));
        rs.insert("summary_digest".into(), digest);
        self.emit("run.ended", Some(&rs), "mcp", None, None, false, None);

        let _ = std::fs::write(
            self.report_dir.join("summary.json"),
            serde_json::to_string_pretty(&summary).unwrap_or_default(),
        );
        if let Some(m) = meta {
            let _ = std::fs::write(
                self.report_dir.join("meta.json"),
                serde_json::to_string_pretty(m).unwrap_or_default(),
            );
        }
        let _ = std::fs::write(self.report_dir.join("timeline.md"), self.render_timeline());
        // review.md — human-readable judge verdict (port of event_writer._render_review).
        // Verdict is passed in from the summary map after judge resolution.
        // Write handled by caller (run.rs) after verdict is inserted.
        summary
    }

    /// Human-readable timeline (deterministic for golden tests).
    pub fn render_timeline(&self) -> String {
        let mut lines = vec![
            format!("# Timeline — {}", self.run_id),
            String::new(),
            "| local time | +ms | turn | kind | source | detail |".to_string(),
            "|---|---|---|---|---|---|".to_string(),
        ];
        for e in &self.events {
            let local_time = e
                .get("datetime_local")
                .and_then(|v| v.as_str())
                .and_then(|s| s.split('T').nth(1))
                .map(|s| s.chars().take(12).collect::<String>())
                .unwrap_or_default();
            let mono = e.get("ts_mono_ms").and_then(|v| v.as_i64()).unwrap_or(0);
            let turn = e.get("turn").and_then(|v| v.as_i64()).unwrap_or(0);
            let kind = e.get("kind").and_then(|v| v.as_str()).unwrap_or("");
            let source = e.get("source").and_then(|v| v.as_str()).unwrap_or("");
            let mut warn = String::new();
            if kind == "transcript.agent.final" {
                let ttm = e
                    .get("spec")
                    .and_then(|v| v.as_object())
                    .and_then(|s| s.get("turn_taking_ms"))
                    .and_then(|v| v.as_i64());
                if let Some(ttm) = ttm {
                    if ttm > self.warn_ms {
                        warn = format!(" ⚠ slow ({ttm}ms > {}ms)", self.warn_ms);
                    }
                }
            }
            let detail = describe(e);
            lines.push(format!(
                "| {local_time} | {mono} | {turn} | `{kind}` | {source} | {detail}{warn} |"
            ));
        }
        lines.push(String::new());
        lines.join("\n")
    }
}

/// Event detail description (mirror event_writer._describe).
pub fn describe(e: &Map<String, Json>) -> String {
    let kind = e.get("kind").and_then(|v| v.as_str()).unwrap_or("");
    let spec = e.get("spec").and_then(|v| v.as_object());
    let get = |k: &str| {
        spec.and_then(|s| s.get(k))
            .and_then(|v| v.as_str())
            .unwrap_or("")
    };
    let trunc = |s: &str| -> String { s.chars().take(120).collect() };
    if kind.starts_with("transcript.") {
        let text = get("text").replace('|', "\\|").replace('\n', " ");
        trunc(&text)
    } else if kind.starts_with("tool.") {
        let mut parts = vec![if get("name").is_empty() {
            "?".to_string()
        } else {
            get("name").to_string()
        }];
        if let Some(dur) = spec
            .and_then(|s| s.get("duration_ms"))
            .and_then(|v| v.as_i64())
        {
            parts.push(format!("{dur}ms"));
        }
        let err = get("error");
        if !err.is_empty() {
            parts.push(format!("error={err}"));
        }
        parts.join(" ")
    } else if kind == "session.agent_state" || kind == "session.user_state" {
        format!("{} → {}", get("old_state"), get("new_state"))
    } else if kind == "session.error" {
        trunc(get("message"))
    } else if kind == "silence.detected" {
        let d = spec
            .and_then(|s| s.get("duration_ms"))
            .and_then(|v| v.as_i64())
            .unwrap_or(0);
        format!("{d}ms of silence")
    } else {
        let mut keys = vec![];
        for k in [
            "name", "identity", "topic", "status", "room", "node_id", "reason",
        ] {
            let v = get(k);
            if !v.is_empty() {
                keys.push(format!("{k}={v}"));
            }
        }
        trunc(&keys.join(", "))
    }
}

/// Render judge verdict as markdown review.md (port of
/// event_writer._render_review). Returns None when verdict has no
/// renderable content.
pub fn render_review(verdict: &Map<String, Json>) -> Option<String> {
    fn has_content(v: &Map<String, Json>) -> bool {
        [
            "overall_summary",
            "strengths",
            "missing_checks",
            "language_naturalness",
            "final_assessment",
        ]
        .iter()
        .any(|k| {
            v.get(*k)
                .map(|v| !v.as_str().unwrap_or("").is_empty())
                .unwrap_or(false)
        })
    }
    // Multi-judge: verdict.judges array present.
    if let Some(judges) = verdict.get("judges").and_then(|v| v.as_array()) {
        if judges.is_empty() {
            return None;
        }
        let mut lines: Vec<String> = Vec::new();
        let v = verdict
            .get("verdict")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let s = verdict.get("score").and_then(|v| v.as_i64()).unwrap_or(0);
        let c = verdict
            .get("confidence")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let m = verdict
            .get("needs_human_review")
            .and_then(|v| v.as_bool())
            .unwrap_or(false);
        lines.push("# Review".into());
        lines.push(format!(
            "Verdict: {v} | Score: {s} | Confidence: {c} | needs_human_review: {m}\n"
        ));
        for j in judges {
            let jid = j
                .get("judge_id")
                .or_else(|| j.get("id"))
                .and_then(|v| v.as_str())
                .unwrap_or("judge");
            lines.push(format!("## Judge: {jid}"));
            if let Some(s) = j.get("overall_summary").and_then(|v| v.as_str()) {
                if !s.is_empty() {
                    lines.push("### Overall".into());
                    lines.push(s.to_string());
                    lines.push(String::new());
                }
            }
            if let Some(arr) = j.get("strengths").and_then(|v| v.as_array()) {
                if !arr.is_empty() {
                    lines.push("### Strengths".into());
                    for item in arr {
                        lines.push(format!("- {item}"));
                    }
                    lines.push(String::new());
                }
            }
            if let Some(arr) = j.get("issues").and_then(|v| v.as_array()) {
                if !arr.is_empty() {
                    lines.push("### Findings".into());
                    for issue in arr {
                        let title = issue
                            .get("title")
                            .and_then(|v| v.as_str())
                            .unwrap_or("issue");
                        let sev = issue
                            .get("severity")
                            .and_then(|v| v.as_str())
                            .unwrap_or("Minor");
                        let evid = issue
                            .get("evidence")
                            .or_else(|| issue.get("agent_line"))
                            .and_then(|v| v.as_str())
                            .unwrap_or("");
                        let imp = issue.get("impact").and_then(|v| v.as_str()).unwrap_or("");
                        let rec = issue
                            .get("recommendation")
                            .and_then(|v| v.as_str())
                            .unwrap_or("");
                        lines.push(format!("### {title}"));
                        lines.push(format!("Severity: {sev}"));
                        if !evid.is_empty() {
                            lines.push(format!("Evidence: {evid}"));
                        }
                        if !imp.is_empty() {
                            lines.push(format!("Impact: {imp}"));
                        }
                        if !rec.is_empty() {
                            lines.push(format!("Recommendation: {rec}"));
                        }
                        lines.push(String::new());
                    }
                }
            }
            if let Some(arr) = j.get("missing_checks").and_then(|v| v.as_array()) {
                if !arr.is_empty() {
                    lines.push("### Missing or Unclear Information".into());
                    for item in arr {
                        lines.push(format!("- {item}"));
                    }
                    lines.push(String::new());
                }
            }
            if let Some(arr) = j.get("language_naturalness").and_then(|v| v.as_array()) {
                if !arr.is_empty() {
                    lines.push("### Language and Conversation Quality".into());
                    for item in arr {
                        lines.push(format!("- {item}"));
                    }
                    lines.push(String::new());
                }
            }
            if let Some(obj) = j.get("final_assessment").and_then(|v| v.as_object()) {
                if !obj.is_empty() {
                    lines.push("### Final Assessment".into());
                    let cats = [
                        "goal_achievement",
                        "understanding",
                        "conversation_flow",
                        "clarity",
                        "user_experience",
                    ];
                    lines.push("| Category | Assessment |".into());
                    lines.push("|---|---|".into());
                    for cat in cats.iter() {
                        let val = obj.get(*cat).and_then(|v| v.as_str()).unwrap_or("");
                        lines.push(format!("| {cat} | {val} |"));
                    }
                    if let Some(conclusion) = obj.get("conclusion").and_then(|v| v.as_str()) {
                        lines.push(format!("\n**Conclusion:** {conclusion}"));
                    }
                    lines.push(String::new());
                }
            }
            // Failed criteria count.
            let empty_criteria = vec![];
            let criteria = j
                .get("criteria")
                .and_then(|v| v.as_array())
                .unwrap_or(&empty_criteria);
            let total = criteria.len();
            let met = criteria
                .iter()
                .filter(|c| {
                    c.get("met")
                        .or_else(|| c.get("pass"))
                        .and_then(|v| v.as_bool())
                        .unwrap_or(false)
                })
                .count();
            if met < total {
                lines.push(format!("*{met}/{total} criteria met.*\n"));
            }
            if let Some(notes) = j.get("notes").and_then(|v| v.as_str()) {
                if !notes.is_empty() {
                    lines.push("## Notes".into());
                    lines.push(notes.to_string());
                }
            }
        }
        let out = lines.join("\n");
        if out.trim().is_empty() {
            None
        } else {
            Some(out)
        }
    } else {
        // Single-judge mode.
        if !has_content(verdict) {
            return None;
        }
        let mut lines: Vec<String> = Vec::new();
        let v = verdict
            .get("verdict")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let s = verdict.get("score").and_then(|v| v.as_i64()).unwrap_or(0);
        lines.push(format!("# Review\n\nVerdict: {v} | Score: {s}\n"));
        if let Some(s) = verdict.get("overall_summary").and_then(|v| v.as_str()) {
            if !s.is_empty() {
                lines.push(format!("## Overall\n{s}\n"));
            }
        }
        if let Some(arr) = verdict.get("strengths").and_then(|v| v.as_array()) {
            if !arr.is_empty() {
                lines.push("## Strengths".into());
                for item in arr {
                    lines.push(format!("- {item}"));
                }
                lines.push(String::new());
            }
        }
        if let Some(arr) = verdict.get("issues").and_then(|v| v.as_array()) {
            if !arr.is_empty() {
                lines.push("## Findings".into());
                for issue in arr {
                    let title = issue
                        .get("title")
                        .and_then(|v| v.as_str())
                        .unwrap_or("issue");
                    let sev = issue
                        .get("severity")
                        .and_then(|v| v.as_str())
                        .unwrap_or("Minor");
                    let evid = issue
                        .get("evidence")
                        .or_else(|| issue.get("agent_line"))
                        .and_then(|v| v.as_str())
                        .unwrap_or("");
                    let imp = issue.get("impact").and_then(|v| v.as_str()).unwrap_or("");
                    let rec = issue
                        .get("recommendation")
                        .and_then(|v| v.as_str())
                        .unwrap_or("");
                    lines.push(format!("### {title}"));
                    lines.push(format!("Severity: {sev}"));
                    if !evid.is_empty() {
                        lines.push(format!("Evidence: {evid}"));
                    }
                    if !imp.is_empty() {
                        lines.push(format!("Impact: {imp}"));
                    }
                    if !rec.is_empty() {
                        lines.push(format!("Recommendation: {rec}"));
                    }
                    lines.push(String::new());
                }
            }
        }
        if let Some(arr) = verdict.get("missing_checks").and_then(|v| v.as_array()) {
            if !arr.is_empty() {
                lines.push("## Missing or Unclear Information".into());
                for item in arr {
                    lines.push(format!("- {item}"));
                }
                lines.push(String::new());
            }
        }
        if let Some(arr) = verdict
            .get("language_naturalness")
            .and_then(|v| v.as_array())
        {
            if !arr.is_empty() {
                lines.push("## Language and Conversation Quality".into());
                for item in arr {
                    lines.push(format!("- {item}"));
                }
                lines.push(String::new());
            }
        }
        if let Some(obj) = verdict.get("final_assessment").and_then(|v| v.as_object()) {
            if !obj.is_empty() {
                lines.push("## Final Assessment".into());
                let cats = [
                    "goal_achievement",
                    "understanding",
                    "conversation_flow",
                    "clarity",
                    "user_experience",
                ];
                lines.push("| Category | Assessment |".into());
                lines.push("|---|---|".into());
                for cat in cats.iter() {
                    let val = obj.get(*cat).and_then(|v| v.as_str()).unwrap_or("");
                    lines.push(format!("| {cat} | {val} |"));
                }
                if let Some(conclusion) = obj.get("conclusion").and_then(|v| v.as_str()) {
                    lines.push(format!("\n**Conclusion:** {conclusion}"));
                }
                lines.push(String::new());
            }
        }
        if let Some(notes) = verdict.get("notes").and_then(|v| v.as_str()) {
            if !notes.is_empty() {
                lines.push("## Notes".into());
                lines.push(notes.to_string());
            }
        }
        let out = lines.join("\n");
        if out.trim().is_empty() {
            None
        } else {
            Some(out)
        }
    }
}

fn uuid_v4_hex12() -> String {
    // 12 hex chars (same as Python uuid4().hex[:12]).
    let mut buf = [0u8; 6];
    getrandom(&mut buf);
    buf.iter().map(|b| format!("{b:02x}")).collect()
}

fn getrandom(buf: &mut [u8]) {
    // Use std::collections::hash_map::RandomState (no extra dep) — NOT
    // cryptographically strong, but matches the envelope's non-crypto uuid use.
    use std::hash::{BuildHasher, Hasher};
    let mut h = std::collections::hash_map::RandomState::new().build_hasher();
    h.write_u64(
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos() as u64)
            .unwrap_or(0),
    );
    h.write(&std::process::id().to_ne_bytes());
    let x = h.finish().to_ne_bytes();
    for (i, b) in x.iter().take(buf.len()).enumerate() {
        buf[i] = *b;
    }
}
