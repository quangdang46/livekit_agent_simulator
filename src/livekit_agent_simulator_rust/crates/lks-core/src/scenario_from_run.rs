//! Promote a finished run into a draft scenario YAML (fail → golden, P1.4/P2 #34).
//!
//! Port of `scenario_from_run.py` (byte-parity output where deterministic).
//! Reads ``reports/<run_id>/{meta,summary,events}`` and synthesizes an
//! agent-sim/v1 draft. Dispatch metadata is copied from the original scenario
//! file when still present on disk; otherwise the draft omits Dispatch and
//! notes that in Context.
//!
//! Extract quality rules (issue #34):
//! - Persona.brief is a short mission statement — never a transcript paste.
//! - Caller intent lands in ``goals[]`` (source persona goals preferred, else
//!   intent-phrased from the first user finals) + ``constraints[]``.
//! - One ``Behavior`` barge/noise stub is reconstructed from ``sim.script.cue``
//!   markers in events.jsonl so a barge-fail replays deterministically.
//! - When ``first_speaker=user``, also emit a minimal Script **open** line (source
//!   Script open preferred, else first user final). Behavior barge-only would
//!   otherwise suppress the Gemini bootstrap and dead-air the call.
//! - Transcript sample + metrics hints live in ``Context.notes`` (author-only).
//! - No full Script reverse-engineer. Humans/agents must review before CI promote.

use std::path::{Path, PathBuf};

use serde_json::{json, Map, Value as Json};

use crate::errors::ConfigError;
use crate::scenario_jsonl::parse_scenario_jsonl;
use crate::scenario_yaml::load_scenario_yaml;

const EMAIL_RE: &str = r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b";
const PHONE_RE: &str = r"\b(?:\+?\d[\d\s().-]{7,}\d)\b";
const CARD_RE: &str = r"\b(?:\d[ -]*?){13,19}\b";

fn redact(text: &str) -> String {
    let t = regex::Regex::new(EMAIL_RE)
        .unwrap()
        .replace_all(text, "[email]");
    let t = regex::Regex::new(CARD_RE)
        .unwrap()
        .replace_all(&t, "[card]");
    let t = regex::Regex::new(PHONE_RE)
        .unwrap()
        .replace_all(&t, "[phone]");
    t.into_owned()
}

fn load_json(path: &Path) -> Map<String, Json> {
    match std::fs::read_to_string(path) {
        Ok(text) => serde_json::from_str(&text).unwrap_or_default(),
        Err(_) => Map::new(),
    }
}

/// Skip blank lines and lines that fail JSON parse (corrupt jsonl tolerated).
fn load_events(path: &Path) -> Vec<Map<String, Json>> {
    let mut out = Vec::new();
    let Ok(text) = std::fs::read_to_string(path) else {
        return out;
    };
    for line in text.lines() {
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        if let Ok(Json::Object(m)) = serde_json::from_str::<Json>(line) {
            out.push(m);
        }
    }
    out
}

fn transcript_finals(events: &[Map<String, Json>], role: &str) -> Vec<String> {
    let kind_target = format!("transcript.{role}.final");
    let mut texts = Vec::new();
    for e in events {
        let kind = e.get("kind").and_then(|v| v.as_str()).unwrap_or("");
        if kind != kind_target {
            continue;
        }
        let t = e
            .get("spec")
            .and_then(|v| v.as_object())
            .and_then(|s| s.get("text"))
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let t = t.trim();
        if !t.is_empty() {
            texts.push(redact(t));
        }
    }
    texts
}

fn slug_id(base: &str, run_id: &str) -> String {
    let mut raw: String = base
        .trim()
        .chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() || c == '-' || c == '_' {
                c
            } else {
                '-'
            }
        })
        .collect();
    raw = raw.trim_matches(|c| c == '-' || c == '_').to_string();
    raw = raw.chars().take(40).collect::<String>().to_lowercase();
    let raw = if raw.is_empty() {
        "from-run".to_string()
    } else {
        raw
    };

    let mut tail: String = run_id
        .rsplit('-')
        .next()
        .unwrap_or("draft")
        .chars()
        .filter(|c| c.is_ascii_alphanumeric())
        .take(6)
        .collect();
    if tail.is_empty() {
        tail = "draft".to_string();
    }
    let cand = format!("from-{raw}-{tail}");
    cand.chars().take(64).collect()
}

fn as_str(v: &Json) -> String {
    match v {
        Json::String(s) => s.clone(),
        Json::Number(n) => n.to_string(),
        Json::Bool(b) => b.to_string(),
        Json::Null => String::new(),
        other => other.to_string(),
    }
}

fn parse_source_scenario(path: &Path) -> Option<crate::scenario::Scenario> {
    if !path.is_file() {
        return None;
    }
    let lower = path
        .extension()
        .and_then(|e| e.to_str())
        .unwrap_or("")
        .to_lowercase();
    if lower == "yaml" || lower == "yml" {
        load_scenario_yaml(&path.to_path_buf()).ok()
    } else {
        parse_scenario_jsonl(&path.to_path_buf()).ok()
    }
}

/// Mirrors `ops._write_yaml_atomic`: write `<dest>.yaml.tmp`, validate it parses
/// as a scenario YAML, then rename over the destination. Broken YAML never lands.
pub fn write_yaml_atomic(dest: &Path, text: &str) -> Result<(), ConfigError> {
    let tmp = dest.with_file_name(format!(
        "{}.yaml.tmp",
        dest.file_name()
            .map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_default()
    ));
    std::fs::write(&tmp, text)
        .map_err(|e| ConfigError(format!("{}: write error — {e}", tmp.display())))?;
    if let Err(e) = load_scenario_yaml(&tmp) {
        let _ = std::fs::remove_file(&tmp);
        return Err(ConfigError(format!("Draft failed validation: {e}")));
    }
    std::fs::rename(&tmp, dest)
        .map_err(|e| ConfigError(format!("{}: rename error — {e}", dest.display())))?;
    Ok(())
}

/// Python float repr: 15563.0 stays "15563.0" (Rust Display drops the .0).
fn py_float_repr(f: f64) -> String {
    if f.fract() == 0.0 && f.abs() < 1e16 {
        format!("{f:.1}")
    } else {
        format!("{f}")
    }
}

/// Python `json.dumps(..., ensure_ascii=False)` — `, ` and `: ` separators
/// (serde_json::to_string is compact; the note text is a user-visible diff).
fn json_dumps_spaced(v: &Json) -> String {
    fn walk(v: &Json, out: &mut String) {
        match v {
            Json::Object(m) => {
                out.push('{');
                let mut first = true;
                for (k, val) in m {
                    if !first {
                        out.push_str(", ");
                    }
                    first = false;
                    out.push('"');
                    out.push_str(k);
                    out.push_str("\": ");
                    walk(val, out);
                }
                out.push('}');
            }
            Json::Array(a) => {
                out.push('[');
                let mut first = true;
                for item in a {
                    if !first {
                        out.push_str(", ");
                    }
                    first = false;
                    walk(item, out);
                }
                out.push(']');
            }
            Json::String(s) => {
                out.push('"');
                out.push_str(s);
                out.push('"');
            }
            Json::Number(n) => out.push_str(&n.to_string()),
            Json::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
            Json::Null => out.push_str("null"),
        }
    }
    let mut s = String::new();
    walk(v, &mut s);
    s
}

/// First explicit Script speak open from the source scenario, if any.
fn script_open_say_from_source(path: &Path) -> Option<String> {
    let scenario = parse_source_scenario(path)?;
    for step in &scenario.script_steps {
        let action = as_str(step.get("action").unwrap_or(&Json::String("speak".into())))
            .trim()
            .to_lowercase();
        if action != "speak" && !action.is_empty() {
            continue;
        }
        let say = as_str(step.get("say").unwrap_or(&Json::Null))
            .trim()
            .to_string();
        if say.is_empty() || say.starts_with('[') {
            continue;
        }
        // Prefer opens that do not require the agent to speak first.
        let need_agent = step
            .get("require_agent_spoke_first")
            .and_then(|v| v.as_bool())
            .unwrap_or(true);
        if need_agent {
            continue;
        }
        if step
            .get("barge_in")
            .and_then(|v| v.as_bool())
            .unwrap_or(false)
        {
            continue;
        }
        return Some(redact(&say).chars().take(200).collect());
    }
    None
}

/// Reconstruct one Behavior barge/noise stub from run markers.
fn behavior_from_events(events: &[Map<String, Json>]) -> Option<Map<String, Json>> {
    let mut fallback_interruption: Option<&Map<String, Json>> = None;
    for e in events {
        let kind = e.get("kind").and_then(|v| v.as_str()).unwrap_or("");
        let spec = e.get("spec").and_then(|v| v.as_object());
        let Some(spec) = spec else { continue };

        if kind == "interruption"
            && as_str(spec.get("by").unwrap_or(&Json::Null)) == "sim"
            && fallback_interruption.is_none()
        {
            fallback_interruption = Some(spec);
            continue;
        }
        if kind != "sim.script.cue" || spec.get("error").is_some() {
            continue;
        }
        let icls = as_str(
            spec.get("class")
                .unwrap_or(&Json::String("correction".into())),
        );
        let barge = spec
            .get("barge_in")
            .and_then(|v| v.as_bool())
            .unwrap_or(false);
        let during = spec
            .get("during_agent_speech")
            .and_then(|v| v.as_bool())
            .unwrap_or(false);
        if !barge && !(icls == "noise" || icls == "backchannel") && !during {
            continue;
        }
        let after_ms = spec
            .get("agent_active_ms")
            .and_then(|v| v.as_i64())
            .unwrap_or(0)
            .max(600);
        let say = redact(as_str(spec.get("say").unwrap_or(&Json::Null)).trim());
        let asset = spec
            .get("asset")
            .and_then(|v| v.as_str())
            .map(|s| s.trim().to_string());

        if icls == "noise" {
            let mut entry = Map::new();
            entry.insert("id".into(), json!("replay-noise-1"));
            entry.insert("after_agent_ms".into(), json!(after_ms));
            entry.insert(
                "say".into(),
                json!(if say.is_empty() {
                    "[noise]"
                } else {
                    say.as_str()
                }),
            );
            if let Some(a) = asset {
                entry.insert("asset".into(), json!(a));
            }
            let mut out = Map::new();
            out.insert("false_interrupts".into(), json!([entry]));
            return Some(out);
        }
        if icls == "backchannel" && !barge {
            let mut entry = Map::new();
            entry.insert("id".into(), json!("replay-backchannel-1"));
            entry.insert("after_agent_ms".into(), json!(after_ms));
            entry.insert(
                "say".into(),
                json!(if say.is_empty() {
                    "uh-huh"
                } else {
                    say.as_str()
                }),
            );
            if let Some(a) = asset {
                entry.insert("asset".into(), json!(a));
            }
            let mut out = Map::new();
            out.insert("backchannels".into(), json!([entry]));
            return Some(out);
        }
        let cls = if icls == "correction"
            || icls == "question"
            || icls == "urgent"
            || icls == "backchannel"
        {
            icls.clone()
        } else {
            "correction".to_string()
        };
        let mut entry = Map::new();
        entry.insert("id".into(), json!("replay-barge-1"));
        entry.insert("after_agent_ms".into(), json!(after_ms));
        entry.insert(
            "say".into(),
            json!(if say.is_empty() {
                "Wait — one second —"
            } else {
                say.as_str()
            }),
        );
        entry.insert("class".into(), json!(cls));
        if let Some(a) = asset {
            entry.insert("asset".into(), json!(a));
        }
        let mut out = Map::new();
        out.insert("barge_ins".into(), json!([entry]));
        return Some(out);
    }
    if let Some(spec) = fallback_interruption {
        let icls = as_str(
            spec.get("class")
                .unwrap_or(&Json::String("correction".into())),
        );
        let cls = if icls == "correction"
            || icls == "question"
            || icls == "urgent"
            || icls == "backchannel"
        {
            icls.clone()
        } else {
            "correction".to_string()
        };
        let say = redact(
            as_str(
                spec.get("say")
                    .unwrap_or(&Json::String("Wait — one second —".into())),
            )
            .as_str(),
        );
        let mut entry = Map::new();
        entry.insert("id".into(), json!("replay-barge-1"));
        entry.insert("after_agent_ms".into(), json!(600));
        entry.insert("say".into(), json!(say));
        entry.insert("class".into(), json!(cls));
        let mut out = Map::new();
        out.insert("barge_ins".into(), json!([entry]));
        return Some(out);
    }
    None
}

/// Minimal Script open so user-first + Behavior barge does not dead-air.
fn script_open_for_user_first(
    first_speaker: &str,
    user_texts: &[String],
    scenario_path: Option<&Path>,
) -> Option<Map<String, Json>> {
    if first_speaker != "user" {
        return None;
    }
    let mut say = scenario_path.and_then(script_open_say_from_source);
    if say.is_none() && !user_texts.is_empty() {
        say = Some(redact(&user_texts[0]).chars().take(200).collect());
    }
    let say = say.unwrap_or_else(|| "Hi — I'm calling about my request.".to_string());
    Some(
        json!({
            "steps": [{
                "id": "open",
                "trigger": "silence",
                "delay_ms": 2200,
                "say": say,
                "once": true,
                "require_agent_spoke_first": false,
            }]
        })
        .as_object()
        .cloned()
        .unwrap(),
    )
}

/// Build a draft scenario dict from a report directory.
///
/// Returns the same keys as Python: scenario_id, source_run_id,
/// source_scenario_id, yaml, kinds, warnings, notes, latency_hint, behavior,
/// script_open, stats.
pub fn build_scenario_draft_from_run(
    report_dir: &Path,
    scenario_id: Option<&str>,
    locale_default: &str,
) -> Result<Map<String, Json>, String> {
    let meta_path = report_dir.join("meta.json");
    let summary_path = report_dir.join("summary.json");
    let events_path = report_dir.join("events.jsonl");
    if !meta_path.exists() || !summary_path.exists() {
        return Err(format!(
            "Report incomplete under {}: need meta.json + summary.json",
            report_dir.display()
        ));
    }

    let meta = load_json(&meta_path);
    let summary = load_json(&summary_path);
    let events = load_events(&events_path);

    let meta_run = as_str(meta.get("run_id").unwrap_or(&Json::Null));
    let summary_run = as_str(summary.get("run_id").unwrap_or(&Json::Null));
    let source_run_id = if !meta_run.is_empty() {
        meta_run
    } else if !summary_run.is_empty() {
        summary_run
    } else {
        report_dir
            .file_name()
            .map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_default()
    };
    let source_scenario = {
        let s = as_str(meta.get("scenario_id").unwrap_or(&Json::Null));
        if s.is_empty() {
            "unknown".to_string()
        } else {
            s
        }
    };
    let scenario_file = meta.get("scenario_file").and_then(|v| v.as_str());
    let scenario_path = scenario_file.map(PathBuf::from).filter(|p| p.is_file());

    let sid_cand = scenario_id.map(str::trim).filter(|s| !s.is_empty());
    let sid = match sid_cand {
        Some(s) => s.to_string(),
        None => slug_id(&source_scenario, &source_run_id),
    };
    let sid_ok = {
        let mut chars = sid.chars();
        match chars.next() {
            Some(c) if c.is_ascii_alphanumeric() => {
                chars.all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_')
                    && sid.chars().count() <= 64
            }
            _ => false,
        }
    };
    if !sid_ok {
        return Err(format!(
            "Invalid scenario_id {sid:?}: use letters/digits/[_-], start with alnum, max 64"
        ));
    }

    let run_spec = meta
        .get("run_spec")
        .and_then(|v| v.as_object())
        .cloned()
        .unwrap_or_default();
    let max_turns = run_spec
        .get("max_turns")
        .and_then(|v| v.as_i64())
        .unwrap_or(
            summary
                .get("turn_count")
                .and_then(|v| v.as_i64())
                .unwrap_or(6),
        );
    let timeout_s = run_spec
        .get("timeout_s")
        .and_then(|v| v.as_i64())
        .unwrap_or(180);
    let mut first_speaker = as_str(
        run_spec
            .get("first_speaker")
            .unwrap_or(&Json::String("agent".into())),
    );
    if first_speaker != "agent" && first_speaker != "user" {
        first_speaker = "agent".to_string();
    }

    // Locale: prefer original scenario metadata if parseable.
    let mut locale = locale_default.to_string();
    if let Some(p) = &scenario_path {
        if let Some(s) = parse_source_scenario(p) {
            let l = s.effective_locale();
            if !l.is_empty() {
                locale = l;
            }
        }
    }

    let mut user_texts = transcript_finals(&events, "user");
    let agent_texts = transcript_finals(&events, "agent");
    // de-dupe consecutive identical user finals (common with multi-source transcripts)
    let mut deduped: Vec<String> = Vec::new();
    for t in &user_texts {
        if deduped.last() != Some(t) {
            deduped.push(t.clone());
        }
    }
    user_texts = deduped;

    let src_persona = scenario_path
        .as_deref()
        .and_then(parse_source_scenario)
        .map(|s| s.persona.clone())
        .unwrap_or_default();
    let name = as_str(
        src_persona
            .get("name")
            .unwrap_or(&Json::String("Caller".into())),
    );
    let language = as_str(
        src_persona
            .get("language")
            .unwrap_or(&Json::String(locale.clone())),
    );
    let traits: Vec<String> = match src_persona.get("traits") {
        Some(Json::Array(a)) => a.iter().map(as_str).collect(),
        _ => vec!["polite".to_string()],
    };
    let traits = if traits.is_empty() {
        vec!["polite".to_string()]
    } else {
        traits
    };
    let constraints: Vec<String> = match src_persona.get("constraints") {
        Some(Json::Array(a)) => a
            .iter()
            .map(as_str)
            .filter(|s| !s.trim().is_empty())
            .collect(),
        _ => Vec::new(),
    };

    // Goals: source persona goals win; else intent-phrased from user finals.
    let mut goals: Vec<String> = match src_persona.get("goals") {
        Some(Json::Array(a)) => a
            .iter()
            .map(as_str)
            .map(|g| g.trim().to_string())
            .filter(|g| !g.is_empty())
            .collect(),
        _ => Vec::new(),
    };
    if goals.is_empty() {
        if !user_texts.is_empty() {
            let mut first = redact(&user_texts[0]);
            let truncated = first.chars().count() > 120;
            first = first.chars().take(120).collect();
            if truncated {
                first.push('…');
            }
            goals.push(format!(
                "Open with the same request as the source run: \"{first}\""
            ));
            if user_texts.len() > 1 {
                goals.push(
                    "Follow up naturally, mirroring the caller path from the source run"
                        .to_string(),
                );
            }
        } else {
            goals.push("Revisit the situation observed in the source run".to_string());
        }
        goals.push("End the call politely".to_string());
    }

    let constraints = if constraints.is_empty() {
        vec!["Stay natural and spoken; never mention being a simulation or a test".to_string()]
    } else {
        constraints
    };

    // Brief: short mission statement only — transcript sample goes to Context.notes.
    let mut brief_bits = vec![
        format!("Promoted from run `{source_run_id}` (source scenario `{source_scenario}`)."),
        "Replay a similar caller path; pursue the listed goals, stay natural and spoken."
            .to_string(),
    ];
    if let Some(b) = src_persona.get("brief").map(as_str) {
        let b = b.trim();
        if !b.is_empty() {
            brief_bits.push(format!(
                "Original brief (reference): {}",
                redact(b).chars().take(300).collect::<String>()
            ));
        }
    }
    let brief = brief_bits.join(" ");

    let metrics = summary
        .get("metrics")
        .and_then(|v| v.as_object())
        .cloned()
        .unwrap_or_default();
    let mut barge_count = metrics
        .get("barge_count")
        .and_then(|v| v.as_i64())
        .unwrap_or(0);
    if barge_count == 0 {
        if let Some(behavior) = summary
            .get("caller")
            .and_then(|v| v.as_object())
            .and_then(|c| c.get("behavior_summary"))
            .and_then(|v| v.as_object())
        {
            barge_count = behavior
                .get("barges_fired")
                .and_then(|v| v.as_i64())
                .unwrap_or(0);
        }
    }

    let mut warnings: Vec<String> = vec![
        "DRAFT — review Persona goals/constraints, Behavior, and Assert before promoting to CI."
            .to_string(),
        "PII redaction is best-effort (email/phone/card patterns only).".to_string(),
    ];

    let behavior_spec = behavior_from_events(&events);
    let has_barge_stub = behavior_spec
        .as_ref()
        .and_then(|b| b.get("barge_ins"))
        .and_then(|v| v.as_array())
        .map(|a| !a.is_empty())
        .unwrap_or(false);
    let script_open =
        script_open_for_user_first(&first_speaker, &user_texts, scenario_path.as_deref());
    if script_open.is_some() {
        warnings.push(
            "Script open added for first_speaker=user (avoids dead-air when Behavior barge suppresses Gemini bootstrap). Review the open line before CI.".to_string(),
        );
    }

    let mut outcomes: Vec<Map<String, Json>> = Vec::new();
    if !agent_texts.is_empty() {
        // weak but useful: agent produced speech
        let mut o = Map::new();
        o.insert("id".into(), json!("agent_spoke"));
        o.insert("type".into(), json!("transcript_contains"));
        o.insert("role".into(), json!("agent"));
        o.insert("phrases".into(), json!(["a", "e", "i", "o", "u"]));
        outcomes.push(o);
    }
    if barge_count > 0 || has_barge_stub {
        let mut o = Map::new();
        o.insert("id".into(), json!("recovered_after_barge"));
        o.insert("type".into(), json!("recovery"));
        o.insert("min_agent_finals_after_barge_in".into(), json!(1));
        o.insert("min_interruptions".into(), json!(0));
        outcomes.push(o);
        if behavior_spec.is_some() {
            warnings.push(format!(
                "Source run had barge_count={barge_count}; Behavior stub + recovery Assert reconstructed from run markers — review timing (after_agent_ms) before CI."
            ));
        } else {
            warnings.push(format!(
                "Source run had barge_count={barge_count} but no sim.script.cue markers to reconstruct; recovery Assert added — re-add Script/Behavior barge cues manually."
            ));
        }
    } else if behavior_spec.is_some() {
        warnings.push(
            "Behavior noise stub reconstructed from run markers — review before CI.".to_string(),
        );
    }

    // optional latency comment values from metrics (not auto-assert — too tight for cold starts)
    let tt = metrics
        .get("turn_taking_ms")
        .and_then(|v| v.as_object())
        .cloned()
        .unwrap_or_default();
    // Preserve the ORIGINAL JSON value type (int stays int, float stays float —
    // Python reads the summary dict raw).
    let ttfw = metrics.get("ttfw_ms").cloned();
    let latency_hint: Option<Map<String, Json>> = {
        let p95 = tt.get("p95").cloned();
        if p95.is_some() || ttfw.is_some() {
            let mut hint = Map::new();
            if let Some(p) = &p95 {
                hint.insert("observed_turn_p95_ms".into(), p.clone());
            }
            if let Some(t) = &ttfw {
                hint.insert("observed_ttfw_ms".into(), t.clone());
            }
            let mut ex = Map::new();
            ex.insert("id".into(), json!("speed"));
            ex.insert("type".into(), json!("latency"));
            let p95f = p95.as_ref().and_then(|v| v.as_f64());
            let ttfwf = ttfw.as_ref().and_then(|v| v.as_f64());
            ex.insert(
                "max_turn_p95_ms".into(),
                json!(p95f.map(|v| (v * 1.5) as i64).unwrap_or(8000)),
            );
            ex.insert(
                "max_ttfw_ms".into(),
                json!(ttfwf.map(|v| (v * 1.5) as i64).unwrap_or(15000)),
            );
            ex.insert("require_turn_samples".into(), json!(1));
            hint.insert("suggested_assert_example".into(), json!(ex));
            Some(hint)
        } else {
            None
        }
    };

    let dispatch_md = scenario_path
        .as_deref()
        .and_then(parse_source_scenario)
        .and_then(|s| s.dispatch)
        .and_then(|d| d.metadata);
    if dispatch_md.is_none() {
        warnings.push(
            "Dispatch.metadata not recovered (source scenario file missing or had no Dispatch). Add Dispatch manually if the agent under test needs opaque metadata.".to_string(),
        );
    }

    let mut criteria: Vec<String> = scenario_path
        .as_deref()
        .and_then(parse_source_scenario)
        .map(|s| s.pass_criteria)
        .unwrap_or_default();
    if criteria.is_empty() {
        criteria = vec![
            "The agent responded to the caller".to_string(),
            "The agent stayed on a helpful path for the caller's goals".to_string(),
        ];
    }
    let verdict = summary
        .get("verdict")
        .and_then(|v| v.as_object())
        .cloned()
        .unwrap_or_default();
    if as_str(verdict.get("verdict").unwrap_or(&Json::Null)).to_lowercase() == "fail" {
        if let Some(note) = verdict.get("notes").map(as_str) {
            if !note.is_empty() {
                criteria.push(format!(
                    "Avoid the failure mode noted in source judge: {}",
                    redact(&note).chars().take(240).collect::<String>()
                ));
            }
        }
    }

    let status = summary.get("status").map(as_str).unwrap_or_default();
    // UTC date string — matches Python's datetime.now(timezone.utc).strftime('%Y-%m-%d').
    let now_date = jiff::Zoned::now().strftime("%Y-%m-%d").to_string();
    let turn_count = summary.get("turn_count").map(as_str).unwrap_or_default();
    let judge_v = as_str(
        verdict
            .get("verdict")
            .unwrap_or(&Json::String("n/a".into())),
    );
    // Python f-string repr: f"{15563.0}" → "15563.0" (JSON float keeps .0);
    // f"{23268}" → "23268" (JSON int plain).
    fn py_fmt(v: &Json) -> String {
        match v {
            Json::Number(n) => {
                if let Some(i) = n.as_i64() {
                    i.to_string()
                } else if let Some(f) = n.as_f64() {
                    py_float_repr(f)
                } else {
                    n.to_string()
                }
            }
            other => other.to_string(),
        }
    }
    let ttfw_str = ttfw
        .as_ref()
        .map(py_fmt)
        .unwrap_or_else(|| "None".to_string());
    let turn_p95_str = tt
        .get("p95")
        .map(py_fmt)
        .unwrap_or_else(|| "None".to_string());
    let mut notes = format!(
        "Promoted {now_date} from run `{source_run_id}` (status={status}, turns={turn_count}, judge={judge_v}). \
         Observed metrics: ttfw_ms={ttfw_str}, turn_p95_ms={turn_p95_str}, barge_count={barge_count}."
    );
    let snippet = user_texts
        .iter()
        .take(4)
        .cloned()
        .collect::<Vec<_>>()
        .join(" | ");
    let snippet: String = snippet.chars().take(400).collect();
    if !snippet.is_empty() {
        notes.push_str(&format!(
            " Caller transcript sample (reference only): {snippet}"
        ));
    }
    if let Some(hint) = &latency_hint {
        if let Some(ex) = hint.get("suggested_assert_example") {
            notes.push_str(&format!(
                " Optional latency Assert (not auto-added): {}",
                json_dumps_spaced(ex)
            ));
        }
    }

    let mut data = Map::new();
    data.insert("apiVersion".into(), json!("agent-sim/v1"));
    data.insert("kind".into(), json!("Scenario"));
    data.insert(
        "metadata".into(),
        json!({
            "id": sid,
            "locale": locale,
            "tags": ["promoted", "from-run", source_scenario.chars().take(32).collect::<String>()],
        }),
    );
    data.insert(
        "persona".into(),
        json!({
            "name": name,
            "language": language,
            "brief": brief,
            "goals": goals,
            "style": as_str(src_persona.get("style").unwrap_or(&Json::String("natural spoken language, concise".into()))),
            "traits": traits,
            "constraints": constraints,
        }),
    );
    data.insert(
        "context".into(),
        json!({
            "notes": notes,
            "fixtures": {"source_run_id": source_run_id, "source_scenario_id": source_scenario},
        }),
    );
    data.insert(
        "execute".into(),
        json!({
            "max_turns": max_turns,
            "timeout_s": timeout_s,
            "first_speaker": first_speaker,
        }),
    );
    if let Some(md) = &dispatch_md {
        data.insert("dispatch".into(), json!({"metadata": md}));
    }
    if let Some(so) = &script_open {
        data.insert("script".into(), Json::Object(so.clone()));
    }
    if let Some(bs) = &behavior_spec {
        data.insert("behavior".into(), Json::Object(bs.clone()));
    }
    if !outcomes.is_empty() {
        data.insert(
            "assert".into(),
            json!({"tools": [], "transcript": [], "outcomes": outcomes}),
        );
    }
    data.insert("pass_criteria".into(), json!({"criteria": criteria}));

    // ---- Serialize to the section-object YAML shape (stable order) ----
    let cleaned =
        crate::yaml_writer::clean(Json::Object(data.clone())).unwrap_or(Json::Object(Map::new()));
    let mut yaml_text = crate::yaml_writer::to_yaml_string(&cleaned);
    yaml_text = format!("# DRAFT from run {source_run_id} — review before CI\n{yaml_text}");

    // Presence-ordered kinds, mirroring the data dict construction above.
    let mut kinds: Vec<String> = vec!["Scenario".to_string()];
    if data.get("persona").is_some() {
        kinds.push("Persona".to_string());
    }
    if data.get("context").is_some() {
        kinds.push("Context".to_string());
    }
    if data.get("execute").is_some() {
        kinds.push("Execute".to_string());
    }
    if data.get("dispatch").is_some() {
        kinds.push("Dispatch".to_string());
    }
    if data.get("script").is_some() {
        kinds.push("Script".to_string());
    }
    if data.get("behavior").is_some() {
        kinds.push("Behavior".to_string());
    }
    if data.get("assert").is_some() {
        kinds.push("Assert".to_string());
    }
    kinds.push("PassCriteria".to_string());

    let mut stats = Map::new();
    stats.insert("user_finals".into(), json!(user_texts.len()));
    stats.insert("agent_finals".into(), json!(agent_texts.len()));
    stats.insert("barge_count".into(), json!(barge_count));
    stats.insert("behavior_stub".into(), json!(behavior_spec.is_some()));
    stats.insert("script_open".into(), json!(script_open.is_some()));
    stats.insert(
        "duration_ms".into(),
        summary.get("duration_ms").cloned().unwrap_or(Json::Null),
    );
    stats.insert("status".into(), json!(status));

    let mut out = Map::new();
    out.insert("scenario_id".into(), json!(sid));
    out.insert("source_run_id".into(), json!(source_run_id));
    out.insert("source_scenario_id".into(), json!(source_scenario));
    out.insert("yaml".into(), json!(yaml_text));
    out.insert("kinds".into(), json!(kinds));
    out.insert("warnings".into(), json!(warnings));
    out.insert("notes".into(), json!(notes));
    out.insert(
        "latency_hint".into(),
        latency_hint.map(Json::Object).unwrap_or(Json::Null),
    );
    out.insert(
        "behavior".into(),
        behavior_spec.map(Json::Object).unwrap_or(Json::Null),
    );
    out.insert(
        "script_open".into(),
        script_open.map(Json::Object).unwrap_or(Json::Null),
    );
    out.insert("stats".into(), Json::Object(stats));
    Ok(out)
}

/// ops.scenario_from_run — promote a finished run; write=True writes the draft.
pub fn scenario_from_run(
    project_root: &Path,
    run_id: &str,
    scenario_id: Option<&str>,
    write: bool,
    locale_default: &str,
) -> Result<Map<String, Json>, String> {
    let cfg = crate::config::load_config(project_root.to_path_buf(), None).map_err(|e| e.0)?;
    let report_dir = cfg.reports_dir().join(run_id);
    if !report_dir.is_dir() {
        return Err(format!(
            "Run report dir not found: {}",
            report_dir.display()
        ));
    }
    let mut draft = build_scenario_draft_from_run(&report_dir, scenario_id, locale_default)?;
    if write {
        let dest = cfg.scenarios_dir().join(format!(
            "{}.yaml",
            as_str(draft.get("scenario_id").unwrap_or(&Json::Null))
        ));
        let yaml_text = as_str(draft.get("yaml").unwrap_or(&Json::Null));
        write_yaml_atomic(&dest, &yaml_text).map_err(|e| e.0)?;
        draft.insert(
            "written_to".into(),
            json!(dest.to_string_lossy().into_owned()),
        );
    }
    Ok(draft)
}
