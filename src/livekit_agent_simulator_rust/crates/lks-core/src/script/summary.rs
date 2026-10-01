//! Post-run caller behavior aggregates — byte-parity port of `script/summary.py`.
//! Always safe to call; zeros when the run had no scripted caller behavior.

use serde_json::{json, Map, Value as Json};

use super::counts_for_recovery_barge;

fn ev_kind(e: &Map<String, Json>) -> &str {
    e.get("kind").and_then(|v| v.as_str()).unwrap_or("")
}

fn ev_spec(e: &Map<String, Json>) -> &Map<String, Json> {
    static EMPTY: std::sync::OnceLock<Map<String, Json>> = std::sync::OnceLock::new();
    e.get("spec")
        .and_then(|v| v.as_object())
        .unwrap_or_else(|| EMPTY.get_or_init(Map::new))
}

fn ev_mono(e: &Map<String, Json>) -> i64 {
    e.get("ts_mono_ms").and_then(|v| v.as_i64()).unwrap_or(0)
}

/// Every script action kind that counts as "a cue fired".
///
/// Mirrors Python `script/summary.py:16-21`, which is the reference (owner
/// decision on dtmf-restore 3tv.3). The four kinds are speak cues, DTMF tones,
/// wait holds and hang-ups — a DTMF step emits `sim.script.dtmf`, so a
/// pure-DTMF run must not report 0 cues fired.
///
/// DELIBERATELY NOT the `cues` filter below. `cues` is speak-cues only and
/// feeds `cues_during_agent` and the barge pass as well, so widening it would
/// change those two fields as a side effect and report them against the wrong
/// population. `script_cues_fired` gets its own aggregate for exactly that
/// reason.
const SCRIPT_ACTION_KINDS: [&str; 4] = [
    "sim.script.cue",
    "sim.script.dtmf",
    "sim.script.wait",
    "sim.script.hang_up",
];

/// Aggregate barge / silence / recovery stats for summary.json + report player.
/// Aggregate barge / silence / recovery stats for summary.json + report player.
pub fn build_caller_behavior_summary(events: &[Map<String, Json>]) -> Map<String, Json> {
    let cues: Vec<&Map<String, Json>> = events
        .iter()
        .filter(|e| ev_kind(e) == "sim.script.cue")
        .collect();
    let waits: Vec<&Map<String, Json>> = events
        .iter()
        .filter(|e| ev_kind(e) == "sim.script.wait")
        .collect();

    let mut barges: Vec<&Map<String, Json>> = Vec::new();
    let mut by_class: Map<String, Json> = Map::new();
    for e in &cues {
        let spec = ev_spec(e);
        let cls = spec
            .get("class")
            .or_else(|| spec.get("interrupt_class"))
            .map(|v| v.as_str().unwrap_or(&v.to_string()).to_string())
            .filter(|s| !s.is_empty());
        if let Some(c) = &cls {
            let n = by_class.get(c).and_then(|v| v.as_i64()).unwrap_or(0) + 1;
            by_class.insert(c.clone(), json!(n));
        }
        if counts_for_recovery_barge(
            spec.get("barge_in")
                .and_then(|v| v.as_bool())
                .unwrap_or(false),
            cls.as_deref(),
        ) {
            barges.push(e);
        }
    }
    let barges_during: Vec<&&Map<String, Json>> = barges
        .iter()
        .filter(|e| {
            ev_spec(e)
                .get("during_agent_speech")
                .and_then(|v| v.as_bool())
                .unwrap_or(false)
        })
        .collect();
    let cues_during: Vec<&&Map<String, Json>> = cues
        .iter()
        .filter(|e| {
            ev_spec(e)
                .get("during_agent_speech")
                .and_then(|v| v.as_bool())
                .unwrap_or(false)
        })
        .collect();
    let silence_events: Vec<&Map<String, Json>> = events
        .iter()
        .filter(|e| ev_kind(e) == "silence.detected")
        .collect();
    let interruptions: Vec<&Map<String, Json>> = events
        .iter()
        .filter(|e| ev_kind(e) == "interruption")
        .collect();
    let agent_finals: Vec<&Map<String, Json>> = events
        .iter()
        .filter(|e| ev_kind(e) == "transcript.agent.final")
        .collect();

    let barge_ms = barges.first().map(|e| ev_mono(e));
    let silence_ms = waits.first().map(|e| ev_mono(e));

    let mut agent_after_barge: i64 = 0;
    let mut recovery_ms: Option<i64> = None;
    if let Some(bms) = barge_ms {
        let after: Vec<i64> = agent_finals
            .iter()
            .filter(|e| ev_mono(e) > bms)
            .map(|e| ev_mono(e))
            .collect();
        agent_after_barge = after.len() as i64;
        if let Some(first) = after.first() {
            recovery_ms = Some(first - bms);
        }
    }

    let agent_after_silence = match silence_ms {
        Some(sms) => agent_finals.iter().filter(|e| ev_mono(e) >= sms).count() as i64,
        None => 0,
    };

    let mut assets: Vec<String> = Vec::new();
    for e in events {
        let k = ev_kind(e);
        if k != "sim.script.cue" && k != "sim.script_inject" {
            continue;
        }
        if let Some(a) = ev_spec(e).get("asset") {
            let s = a.as_str().unwrap_or(&a.to_string()).to_string();
            if !s.is_empty() && !assets.contains(&s) {
                assets.push(s);
            }
        }
    }

    let mut out = Map::new();
    let script_actions = events
        .iter()
        .filter(|e| SCRIPT_ACTION_KINDS.contains(&ev_kind(e)))
        .count();
    out.insert(
        "script_cues_fired".into(),
        json!(script_actions),
        // `cues` (speak-cues only) still feeds cues_during_agent and the
        // barge pass. Counting those from the four-kind aggregate would
        // report them against the wrong population.
    );
    // Python emits this key (script/verify.py:268) and `lksr` did not, so a
    // web consumer reading summary.json["dtmf_fired"] got undefined under one
    // port and a number under the other. The web type declared it optional, so
    // nothing CRASHED — which is why this sat unnoticed. Emitting it on both
    // ports removes the divergence rather than relying on the type being
    // optional.
    //
    // Note the overlap with script_cues_fired, which since 3tv.7.2 counts the
    // four-kind aggregate and so already includes dtmf. Whether this key is a
    // second signal or plain redundancy is an open question — see bead t7f — but
    // shipping one port's key and not the other's is not a choice worth making.
    out.insert(
        "dtmf_fired".into(),
        json!(events
            .iter()
            .filter(|e| ev_kind(e) == "sim.script.dtmf")
            .count()),
    );
    out.insert("waits_fired".into(), json!(waits.len()));
    out.insert("barges_fired".into(), json!(barges.len()));
    out.insert("barges_during_agent".into(), json!(barges_during.len()));
    out.insert("cues_during_agent".into(), json!(cues_during.len()));
    out.insert("silences_held".into(), json!(waits.len()));
    out.insert("silence_events".into(), json!(silence_events.len()));
    out.insert("interruptions".into(), json!(interruptions.len()));
    out.insert("agent_finals_after_barge".into(), json!(agent_after_barge));
    out.insert(
        "agent_finals_after_silence".into(),
        json!(agent_after_silence),
    );
    out.insert(
        "recovery_ms".into(),
        recovery_ms.map(|v| json!(v)).unwrap_or(Json::Null),
    );
    out.insert(
        "cue_assets".into(),
        Json::Array(assets.into_iter().map(Json::String).collect()),
    );
    out.insert("by_class".into(), Json::Object(by_class));
    out
}
