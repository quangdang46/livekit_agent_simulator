//! Execute-family ops (async, needs the livekit runner) — port of
//! `ops.execute_scenarios` / `ops.execute_scenario_dict` / `ops.optimize_persona`.
//! Lives in lks-livekit (not lks-core) because it drives run.rs bridges.

use std::path::Path;
use std::sync::Arc;

use serde_json::{json, Map, Value as Json};
use tokio::sync::Mutex;

use lks_core::config::load_config;
use lks_core::errors::RunError;
use lks_core::optimize::{variant_from_dict, PromptVariant};
use lks_core::scenario_ops::list_scenarios;

use crate::run::{execute_scenario, execute_scenario_parsed, ExecuteOptions};

/// Suite-run options (port of `ops.execute_scenarios` kwargs).
#[derive(Debug, Clone, Default)]
pub struct SuiteOptions {
    pub scenario_ids: Option<Vec<String>>,
    pub tag: Option<String>,
    pub strict_judge: bool,
    pub write_report: bool,
    pub repeat: i64,
    pub pass_at_k: Option<i64>,
    pub parallel: i64,
    pub wait_s: f64,
    pub agent_name: Option<String>,
    pub profile: Option<String>,
    pub environment: Option<String>,
}

/// Run multiple scenarios + suite matrix / CI gate (port of
/// `ops.execute_scenarios`). `parallel` workers with `wait_s` cooldown;
/// order preserved; errors become executed=false rows.
pub async fn op_execute_scenarios(
    project_root: &Path,
    opts: &SuiteOptions,
) -> Result<Map<String, Json>, RunError> {
    let parallel = opts.parallel;
    let wait_s = opts.wait_s;
    if parallel < 1 {
        return Err(RunError(format!("parallel must be >= 1, got {parallel}")));
    }
    if wait_s < 0.0 {
        return Err(RunError(format!("wait_s must be >= 0, got {wait_s}")));
    }

    let cfg = load_config(
        project_root.to_path_buf(),
        opts.profile.as_deref(),
        opts.environment.as_deref(),
    )
    .map_err(|e| RunError(e.0))?;
    let listed = list_scenarios(&cfg.scenarios_dir());
    let targets: Vec<String> = if let Some(ids) = opts.scenario_ids.as_deref() {
        ids.to_vec()
    } else {
        listed
            .iter()
            .filter(|item| {
                item.get("id").and_then(|v| v.as_str()).is_some()
                    && item.get("error").is_none()
                    && (opts.tag.is_none()
                        || item
                            .get("tags")
                            .and_then(|t| t.as_array())
                            .map(|a| {
                                a.iter()
                                    .any(|x| x.as_str() == Some(opts.tag.as_deref().unwrap_or("")))
                            })
                            .unwrap_or(false))
            })
            .filter_map(|item| item.get("id").and_then(|v| v.as_str()).map(String::from))
            .collect()
    };

    // One scenario run (wrapped so errors become executed=false rows).
    async fn one(
        project_root: &Path,
        sid: &str,
        repeat: i64,
        pass_at_k: Option<i64>,
        agent_name: Option<&str>,
        profile: Option<&str>,
        environment: Option<&str>,
    ) -> Map<String, Json> {
        let opts = ExecuteOptions {
            repeat,
            pass_at_k,
            agent_name: agent_name.map(String::from),
            profile: profile.map(String::from),
            environment: environment.map(String::from),
            ..Default::default()
        };
        match execute_scenario(project_root, sid, &opts).await {
            Ok(r) => r,
            Err(e) => {
                let mut m = Map::new();
                m.insert("executed".into(), json!(false));
                m.insert("scenario_id".into(), json!(sid));
                m.insert("error".into(), json!(e.to_string()));
                m
            }
        }
    }

    type ResultRow = (usize, Map<String, Json>);
    let cooldown = wait_s;
    let results: Vec<Map<String, Json>> = if parallel == 1 || targets.len() <= 1 {
        let mut out = Vec::new();
        for (i, sid) in targets.iter().enumerate() {
            if i > 0 && cooldown > 0.0 {
                tokio::time::sleep(std::time::Duration::from_secs_f64(cooldown)).await;
            }
            out.push(
                one(
                    project_root,
                    sid,
                    opts.repeat,
                    opts.pass_at_k,
                    opts.agent_name.as_deref(),
                    opts.profile.as_deref(),
                    opts.environment.as_deref(),
                )
                .await,
            );
        }
        out
    } else {
        // Worker-queue with None sentinels (cancel-safe; no semaphore — the
        // legacy Python bug was releasing slots to unseen-waiter on cancel).
        let results: Arc<Mutex<Vec<ResultRow>>> = Arc::new(Mutex::new(Vec::new()));
        let queue: Arc<tokio::sync::Mutex<std::collections::VecDeque<Option<String>>>> =
            Arc::new(tokio::sync::Mutex::new(
                targets
                    .iter()
                    .cloned()
                    .map(Some)
                    .chain(std::iter::repeat_n(None, parallel as usize))
                    .collect(),
            ));
        let mut handles = Vec::new();
        let repeat = opts.repeat;
        let pass_at_k = opts.pass_at_k;
        for _ in 0..parallel {
            let queue = queue.clone();
            let results = results.clone();
            let project_root = project_root.to_path_buf();
            let agent_name = opts.agent_name.clone();
            let profile = opts.profile.clone();
            let environment = opts.environment.clone();
            handles.push(tokio::spawn(async move {
                loop {
                    let next = {
                        let mut q = queue.lock().await;
                        q.pop_front()
                    };
                    let Some(Some(sid)) = next else { return };
                    let idx = results.lock().await.len();
                    let out = one(
                        &project_root,
                        &sid,
                        repeat,
                        pass_at_k,
                        agent_name.as_deref(),
                        profile.as_deref(),
                        environment.as_deref(),
                    )
                    .await;
                    results.lock().await.push((idx, out));
                    if cooldown > 0.0 {
                        tokio::time::sleep(std::time::Duration::from_secs_f64(cooldown)).await;
                    }
                }
            }));
        }
        for h in handles {
            let _ = h.await;
        }
        let mut by_idx: Vec<Option<Map<String, Json>>> = vec![None; targets.len()];
        for (idx, r) in results.lock().await.iter() {
            if *idx < by_idx.len() {
                by_idx[*idx] = Some(r.clone());
            }
        }
        by_idx
            .into_iter()
            .map(|r| {
                r.unwrap_or_else(|| {
                    let mut m = Map::new();
                    m.insert("executed".into(), json!(false));
                    m.insert("scenario_id".into(), Json::Null);
                    m.insert(
                        "error".into(),
                        json!("suite stopped before this scenario ran"),
                    );
                    m
                })
            })
            .collect()
    };

    let suite =
        lks_core::suite::build_suite_report(&results, opts.strict_judge, opts.tag.as_deref());
    let ok = suite.get("ok").and_then(|v| v.as_bool()).unwrap_or(false);
    let exit_code = suite.get("exit_code").and_then(|v| v.as_i64()).unwrap_or(1);
    let mut out = Map::new();
    out.insert("count".into(), json!(results.len()));
    out.insert(
        "results".into(),
        Json::Array(results.into_iter().map(Json::Object).collect()),
    );
    out.insert("suite".into(), Json::Object(suite));
    out.insert("ok".into(), json!(ok));
    out.insert("exit_code".into(), json!(exit_code));
    out.insert("parallel".into(), json!(parallel));
    out.insert("wait_s".into(), json!(wait_s));
    if opts.write_report {
        let paths = lks_core::suite::write_suite_report(
            out.get("suite").and_then(|v| v.as_object()).unwrap(),
            &cfg.reports_dir(),
            None,
        )
        .map_err(|e| RunError(format!("suite report write: {e}")))?;
        out.insert("suite_report".into(), Json::Object(paths));
    }
    Ok(out)
}

/// Validate then run an in-memory scenario dict (port of
/// `ops.execute_scenario_dict` — no JSONL file on disk required).
pub async fn op_execute_scenario_dict(
    project_root: &Path,
    scenario: &Map<String, Json>,
    run_name: Option<&str>,
    agent_name: Option<&str>,
    profile: Option<&str>,
    environment: Option<&str>,
) -> Result<Map<String, Json>, RunError> {
    // scenario_from_dict parses the same shape as export_scenario.
    let parsed = match lks_core::scenario::scenario_from_dict(scenario, None, "scenario_dict") {
        Ok(s) => s,
        Err(e) => {
            let mut validation = Map::new();
            validation.insert("valid".into(), json!(false));
            validation.insert("error".into(), json!(e.to_string()));
            let mut m = Map::new();
            m.insert("executed".into(), json!(false));
            m.insert("validation".into(), Json::Object(validation));
            return Ok(m);
        }
    };
    let opts = ExecuteOptions {
        run_name: run_name.map(String::from),
        agent_name: agent_name.map(String::from),
        profile: profile.map(String::from),
        environment: environment.map(String::from),
        ..Default::default()
    };
    let mut result = execute_scenario_parsed(project_root, &parsed, &opts).await?;
    result.insert("executed".into(), json!(true));
    let mut validation = Map::new();
    validation.insert("valid".into(), json!(true));
    result.insert("validation".into(), Json::Object(validation));
    Ok(result)
}

/// Optimizer options (port of `ops.optimize_persona` kwargs).
#[derive(Debug, Clone, Default)]
pub struct OptimizeOptions {
    pub scenario_ids: Vec<String>,
    pub held_out: Option<String>,
    pub candidates: i64,
    pub max_candidates: i64,
    pub strict_judge: bool,
    pub repeat: i64,
    pub pass_at_k: Option<i64>,
    pub agent_name: Option<String>,
    pub name: Option<String>,
    pub profile: Option<String>,
    pub environment: Option<String>,
}

/// Run the persona-prompt optimizer over a dataset (live benchmark loop — port
/// of `optimize/optimize.py:optimize_persona` + `eval.py` + `gen.py`).
///
/// Runs the baseline + deterministic candidates over the train scenarios,
/// selects a winner that strictly beats baseline AND passes held-out, and
/// writes `.agent-sim/optimized/<name>/` artifacts (prompt.yaml, baseline.json,
/// diff.txt, candidates/). The LLM proposer reuses the configured judge backend
/// (never fails the run on proposer noise — deterministic set stands).
pub async fn op_optimize_persona(
    project_root: &Path,
    opts: &OptimizeOptions,
) -> Result<Map<String, Json>, RunError> {
    let scenario_ids = &opts.scenario_ids;
    let held_out = opts.held_out.as_deref();
    let candidates = opts.candidates;
    let max_candidates = opts.max_candidates;
    let strict_judge = opts.strict_judge;
    let repeat = opts.repeat;
    let pass_at_k = opts.pass_at_k;
    let agent_name = opts.agent_name.as_deref();
    let name = opts.name.as_deref();
    let profile = opts.profile.as_deref();
    let environment = opts.environment.as_deref();
    use lks_core::optimize::{deterministic_candidates, variant_to_dict, write_variant};

    let cfg = load_config(project_root.to_path_buf(), profile, environment)
        .map_err(|e| RunError(e.0))?;
    let heldout_ids: Vec<String> = held_out
        .filter(|h| !scenario_ids.iter().any(|s| s == h))
        .map(|h| vec![h.to_string()])
        .unwrap_or_default();
    let train_ids: Vec<String> = scenario_ids
        .iter()
        .filter(|s| held_out.map(|h| *s != h).unwrap_or(true))
        .cloned()
        .collect();
    if train_ids.is_empty() {
        return Err(RunError(
            "scenario_ids must be a non-empty comma-separated list".into(),
        ));
    }

    // Evaluate one variant over the train set → dataset metric (port of
    // `eval.py:evaluate_variant`).
    struct EvalCtx<'a> {
        project_root: &'a Path,
        strict_judge: bool,
        repeat: i64,
        pass_at_k: Option<i64>,
        agent_name: Option<&'a str>,
        profile: Option<&'a str>,
        environment: Option<&'a str>,
    }
    async fn evaluate_variant(
        ctx: &EvalCtx<'_>,
        variant: Option<&PromptVariant>,
        scenario_ids: &[String],
        optimize: Option<&str>,
    ) -> Map<String, Json> {
        let mut per_scenario: Vec<Json> = Vec::new();
        for sid in scenario_ids {
            let opts = ExecuteOptions {
                repeat: ctx.repeat,
                pass_at_k: ctx.pass_at_k,
                agent_name: ctx.agent_name.map(String::from),
                optimized: optimize.map(String::from),
                profile: ctx.profile.map(String::from),
                environment: ctx.environment.map(String::from),
                ..Default::default()
            };
            let result = execute_scenario(ctx.project_root, sid, &opts).await;
            let result_map = match result {
                Ok(m) => m,
                Err(e) => {
                    let mut m = Map::new();
                    m.insert("executed".into(), json!(false));
                    m.insert("scenario_id".into(), json!(sid));
                    m.insert("error".into(), json!(e.to_string()));
                    m
                }
            };
            let gate = lks_core::suite::evaluate_run_result(&result_map, ctx.strict_judge);
            let mut row = Map::new();
            row.insert("scenario_id".into(), json!(sid));
            row.insert("ok".into(), gate.get("ok").cloned().unwrap_or(json!(false)));
            row.insert(
                "gate".into(),
                gate.get("gate").cloned().unwrap_or(json!("?")),
            );
            row.insert(
                "hard_reasons".into(),
                gate.get("hard_reasons").cloned().unwrap_or(json!([])),
            );
            row.insert(
                "soft_reasons".into(),
                gate.get("soft_reasons").cloned().unwrap_or(json!([])),
            );
            per_scenario.push(Json::Object(row));
        }
        // dataset_pass_rate aggregation
        let total = per_scenario.len();
        let passed = per_scenario
            .iter()
            .filter(|r| r.get("ok").and_then(|v| v.as_bool()).unwrap_or(false))
            .count();
        let pass_rate = if total > 0 {
            passed as f64 / total as f64
        } else {
            0.0
        };
        let mut m = Map::new();
        m.insert(
            "variant_id".into(),
            json!(variant
                .map(|v| v.id.clone())
                .unwrap_or_else(|| "baseline".into())),
        );
        m.insert("pass_rate".into(), json!(pass_rate));
        m.insert("ok".into(), json!(passed == total));
        m.insert("total".into(), json!(total));
        m.insert("passed_gate".into(), json!(passed));
        m.insert("per_scenario".into(), Json::Array(per_scenario));
        m
    }

    // Compose the SI a variant would produce (for diffs; port of
    // `optimize.py:_compose_instruction` — uses the first train scenario's persona).
    let first_scenario = find_scenario_parsed(project_root, &train_ids[0], environment).await?;
    let compose_si = |v: Option<&PromptVariant>| -> String {
        match v {
            Some(variant) => lks_core::optimize::render_variant_prompt_for_persona(
                variant,
                &first_scenario.persona,
                &first_scenario.effective_locale(),
                &first_scenario.context,
                &first_scenario.script_steps,
                &first_scenario.run_spec().first_speaker,
            ),
            None => build_persona_si(&first_scenario),
        }
    };

    let eval_ctx = EvalCtx {
        project_root,
        strict_judge,
        repeat,
        pass_at_k,
        agent_name,
        profile,
        environment,
    };
    let baseline = evaluate_variant(&eval_ctx, None, &train_ids, None).await;
    let current_si = compose_si(None);
    let baseline_rate = baseline
        .get("pass_rate")
        .and_then(|v| v.as_f64())
        .unwrap_or(0.0);

    // Deterministic set + LLM-proposed (capped; proposer failure never fails run).
    let mut variants: Vec<PromptVariant> = deterministic_candidates();
    let llm_count = (max_candidates as usize).saturating_sub(variants.len());
    if llm_count > 0 {
        if let Ok(Some(text)) = propose_llm(&cfg, &current_si, llm_count).await {
            let mut seen: std::collections::HashSet<String> =
                variants.iter().map(|v| v.id.clone()).collect();
            for parsed in parse_llm_candidates(&text, llm_count) {
                if !seen.contains(&parsed.id) && variants.len() < max_candidates as usize {
                    seen.insert(parsed.id.clone());
                    variants.push(parsed);
                }
            }
        }
    }

    // Stage + evaluate each of the first `candidates` variants.
    let mut evaluated: Vec<Map<String, Json>> = Vec::new();
    for v in variants.iter().take(candidates.max(0) as usize) {
        let stage = format!("__candidate__{}", v.id);
        let stage_dir = cfg.optimized_dir().join(&stage);
        let _ = std::fs::create_dir_all(&stage_dir);
        if let Err(e) = write_variant(v, &stage_dir.join("prompt.yaml")) {
            return Err(RunError(format!("stage variant {}: {e}", v.id)));
        }
        let ev = evaluate_variant(&eval_ctx, Some(v), &train_ids, Some(&stage)).await;
        let _ = std::fs::remove_dir_all(&stage_dir);
        let mut ev_map = ev;
        ev_map.insert("variant".into(), json!(variant_to_dict(v)));
        ev_map.insert(
            "diff".into(),
            json!(diff_si(&compose_si(None), &compose_si(Some(v)))),
        );
        evaluated.push(ev_map);
    }

    // Held-out gate.
    let heldout_metric = if heldout_ids.is_empty() {
        None
    } else {
        Some(evaluate_variant(&eval_ctx, None, &heldout_ids, None).await)
    };

    // select_winner — strictly beats baseline AND passes held-out (>= 1.0).
    let mut winner: Option<Map<String, Json>> = None;
    let mut best_rate = baseline_rate;
    for ev in &evaluated {
        let rate = ev.get("pass_rate").and_then(|v| v.as_f64()).unwrap_or(0.0);
        if rate <= best_rate {
            continue;
        }
        if let Some(ho) = &heldout_metric {
            let ho_rate = ho.get("pass_rate").and_then(|v| v.as_f64()).unwrap_or(0.0);
            if ho_rate < 1.0 || !ho.get("ok").and_then(|v| v.as_bool()).unwrap_or(false) {
                continue;
            }
        }
        if winner.is_none()
            || rate
                > winner
                    .as_ref()
                    .and_then(|w| w.get("pass_rate"))
                    .and_then(|v| v.as_f64())
                    .unwrap_or(0.0)
        {
            winner = Some(ev.clone());
            best_rate = rate;
        }
    }

    // Write artifacts (port of `optimize.py:_write_artifacts`).
    let slug = name
        .map(String::from)
        .unwrap_or_else(|| format!("optimize-{}", jiff::Zoned::now().strftime("%Y%m%d-%H%M%S")));
    let out_dir = cfg.optimized_dir().join(&slug);
    let _ = std::fs::create_dir_all(&out_dir);
    let candidate_dir = out_dir.join("candidates");
    let _ = std::fs::create_dir_all(&candidate_dir);
    for ev in &evaluated {
        if let Some(v) = ev.get("variant") {
            let variant = variant_from_dict(v.as_object().unwrap_or(&Map::new()));
            let _ = write_variant(
                &variant,
                &candidate_dir.join(format!("{}.yaml", variant.id)),
            );
        }
    }

    let mut baseline_json = Map::new();
    baseline_json.insert("name".into(), json!(slug));
    baseline_json.insert(
        "created_utc".into(),
        json!(jiff::Zoned::now()
            .strftime("%Y-%m-%dT%H:%M:%S%.f%:z")
            .to_string()),
    );
    baseline_json.insert("dataset_scenario_ids".into(), json!(scenario_ids));
    let mut b = Map::new();
    b.insert(
        "pass_rate".into(),
        baseline.get("pass_rate").cloned().unwrap_or(json!(0.0)),
    );
    b.insert(
        "ok".into(),
        baseline.get("ok").cloned().unwrap_or(json!(false)),
    );
    b.insert(
        "total".into(),
        baseline.get("total").cloned().unwrap_or(json!(0)),
    );
    b.insert(
        "passed_gate".into(),
        baseline.get("passed_gate").cloned().unwrap_or(json!(0)),
    );
    b.insert(
        "per_scenario".into(),
        baseline.get("per_scenario").cloned().unwrap_or(json!([])),
    );
    baseline_json.insert("baseline".into(), Json::Object(b));
    let cands: Vec<Json> = evaluated
        .iter()
        .map(|ev| {
            let mut c = Map::new();
            let vid = ev
                .get("variant")
                .and_then(|v| v.as_object())
                .and_then(|m| m.get("id"))
                .and_then(|v| v.as_str())
                .unwrap_or("?");
            c.insert("id".into(), json!(vid));
            c.insert(
                "pass_rate".into(),
                ev.get("pass_rate").cloned().unwrap_or(json!(0.0)),
            );
            c.insert("ok".into(), ev.get("ok").cloned().unwrap_or(json!(false)));
            c.insert(
                "per_scenario".into(),
                ev.get("per_scenario").cloned().unwrap_or(json!([])),
            );
            Json::Object(c)
        })
        .collect();
    baseline_json.insert("candidates".into(), Json::Array(cands));
    if let Some(ho) = &heldout_metric {
        let mut ho_map = Map::new();
        ho_map.insert("scenario_ids".into(), json!(heldout_ids));
        ho_map.insert(
            "pass_rate".into(),
            ho.get("pass_rate").cloned().unwrap_or(json!(0.0)),
        );
        ho_map.insert("ok".into(), ho.get("ok").cloned().unwrap_or(json!(false)));
        ho_map.insert(
            "per_scenario".into(),
            ho.get("per_scenario").cloned().unwrap_or(json!([])),
        );
        baseline_json.insert("held_out".into(), Json::Object(ho_map));
    }
    let _ = std::fs::write(
        out_dir.join("baseline.json"),
        serde_json::to_string_pretty(&baseline_json).unwrap_or_default(),
    );

    let mut diff_parts: Vec<String> = Vec::new();
    for ev in &evaluated {
        let vid = ev
            .get("variant")
            .and_then(|v| v.as_object())
            .and_then(|m| m.get("id"))
            .and_then(|v| v.as_str())
            .unwrap_or("?");
        let rate = ev.get("pass_rate").and_then(|v| v.as_f64()).unwrap_or(0.0);
        diff_parts.push(format!(
            "=== candidate {vid} (pass {:.0}%) ===",
            rate * 100.0
        ));
        diff_parts.push(
            ev.get("diff")
                .and_then(|v| v.as_str())
                .map(String::from)
                .unwrap_or_else(|| "(no diff)".to_string()),
        );
        diff_parts.push(String::new());
    }
    let _ = std::fs::write(out_dir.join("diff.txt"), diff_parts.join("\n"));

    if let Some(w) = &winner {
        if let Some(v) = w.get("variant") {
            let variant = variant_from_dict(v.as_object().unwrap_or(&Map::new()));
            let _ = write_variant(&variant, &out_dir.join("prompt.yaml"));
        }
    }

    let mut out = Map::new();
    out.insert("name".into(), json!(slug));
    out.insert("dir".into(), json!(out_dir.to_string_lossy().into_owned()));
    out.insert(
        "winner".into(),
        match &winner {
            Some(w) => {
                let mut wm = Map::new();
                let vid = w
                    .get("variant")
                    .and_then(|v| v.as_object())
                    .and_then(|m| m.get("id"))
                    .and_then(|v| v.as_str())
                    .unwrap_or("?");
                wm.insert("id".into(), json!(vid));
                wm.insert(
                    "pass_rate".into(),
                    w.get("pass_rate").cloned().unwrap_or(json!(0.0)),
                );
                wm.insert("baseline_pass_rate".into(), json!(baseline_rate));
                Json::Object(wm)
            }
            None => Json::Null,
        },
    );
    out.insert("baseline_pass_rate".into(), json!(baseline_rate));
    out.insert(
        "candidate_pass_rates".into(),
        Json::Array(
            evaluated
                .iter()
                .map(|ev| {
                    let mut c = Map::new();
                    let vid = ev
                        .get("variant")
                        .and_then(|v| v.as_object())
                        .and_then(|m| m.get("id"))
                        .and_then(|v| v.as_str())
                        .unwrap_or("?");
                    c.insert("id".into(), json!(vid));
                    c.insert(
                        "pass_rate".into(),
                        ev.get("pass_rate").cloned().unwrap_or(json!(0.0)),
                    );
                    Json::Object(c)
                })
                .collect(),
        ),
    );
    let mut files = Map::new();
    files.insert(
        "prompt.yaml".into(),
        json!(out_dir.join("prompt.yaml").exists()),
    );
    files.insert(
        "baseline.json".into(),
        json!(out_dir.join("baseline.json").exists()),
    );
    files.insert("diff.txt".into(), json!(out_dir.join("diff.txt").exists()));
    out.insert("files".into(), Json::Object(files));
    out.insert(
        "held_out".into(),
        match &heldout_metric {
            Some(ho) => {
                let mut hm = Map::new();
                hm.insert(
                    "pass_rate".into(),
                    ho.get("pass_rate").cloned().unwrap_or(json!(0.0)),
                );
                hm.insert("ok".into(), ho.get("ok").cloned().unwrap_or(json!(false)));
                Json::Object(hm)
            }
            None => Json::Null,
        },
    );
    Ok(out)
}

async fn find_scenario_parsed(
    project_root: &Path,
    sid: &str,
    environment: Option<&str>,
) -> Result<lks_core::scenario::Scenario, RunError> {
    let cfg = load_config(project_root.to_path_buf(), None, environment)
        .map_err(|e| RunError(e.0))?;
    // Python optimize._compose_instruction parses `<scenarios_dir>/<id>.yaml`
    // DIRECTLY — the error is `Scenario file not found: <path>` (no fallback
    // scan). Match that for the optimizer compose path.
    let direct = cfg.scenarios_dir().join(format!("{sid}.yaml"));
    if direct.is_file() {
        lks_core::scenario_yaml::load_scenario_yaml(&direct).map_err(|e| RunError(e.0))
    } else {
        Err(RunError(format!(
            "Scenario file not found: {}",
            direct.display()
        )))
    }
}

fn build_persona_si(scenario: &lks_core::scenario::Scenario) -> String {
    crate::run::build_persona_prompt(scenario)
}

fn diff_si(base: &str, cand: &str) -> String {
    // Minimal unified-diff (port of optimize.py:_diff_instruction via
    // difflib.unified_diff with lineterm="").
    let base_lines: Vec<&str> = base.lines().collect();
    let cand_lines: Vec<&str> = cand.lines().collect();
    let mut out: Vec<String> = Vec::new();
    let mut i = 0usize;
    let mut j = 0usize;
    while i < base_lines.len() || j < cand_lines.len() {
        if i < base_lines.len() && j < cand_lines.len() && base_lines[i] == cand_lines[j] {
            out.push(format!(" {}", base_lines[i]));
            i += 1;
            j += 1;
        } else {
            let mut ci = i;
            let mut cj = j;
            while ci < base_lines.len() && !cand_lines[j..].contains(&base_lines[ci]) {
                ci += 1;
            }
            while cj < cand_lines.len() && !base_lines[i..].contains(&cand_lines[cj]) {
                cj += 1;
            }
            if ci == i && cj == j {
                if i < base_lines.len() {
                    out.push(format!("-{}", base_lines[i]));
                    i += 1;
                }
                if j < cand_lines.len() {
                    out.push(format!("+{}", cand_lines[j]));
                    j += 1;
                }
            } else {
                out.extend(base_lines[i..ci].iter().map(|l| format!("-{l}")));
                out.extend(cand_lines[j..cj].iter().map(|l| format!("+{l}")));
                i = ci;
                j = cj;
            }
        }
    }
    out.join("\n")
}

/// LLM proposer via the configured judge backend (port of
/// `optimize/_backend.py:proposer_for` — never fails the run).
async fn propose_llm(
    cfg: &lks_core::config::SimConfig,
    current_si: &str,
    llm_count: usize,
) -> Result<Option<String>, String> {
    use lks_core::judge::{resolve_judge, HttpOpenAIBackend};
    let resolved = resolve_judge(cfg.judge.as_ref(), Some(&cfg.simulator.api_key));
    if !resolved.ready {
        return Ok(None); // deterministic set stands
    }
    let backend = HttpOpenAIBackend {
        base_url: resolved.base_url.clone().unwrap_or_default(),
        api_key: resolved.api_key.clone().unwrap_or_default(),
        model: resolved.model.clone(),
        temperature: resolved.temperature,
        timeout_s: 60,
    };
    let system = "You are a prompt-optimization assistant for a simulated-caller \
persona-prompt composer. You propose SMALL, STRUCTURAL mutations to improve how \
naturally the simulated human caller pursues their goals.\n\n\
You may change:\n\
- verbosity: \"quiet\" | \"natural\" | \"chatty\" (the utterance-length band)\n\
- section_order: a reordering/subset of these section names:\n\
  Role, Goals, StyleTraits, NaturalSpeech, Constraints, SpeechConditions,\n\
  Context, ScriptTiming, FirstSpeaker, Guardrails\n\
- extra_guardrails: short generic lines appended to the guardrails block\n\
- extra_lines: {section_name: [lines]} appended to a named section\n\n\
You must NOT invent business facts, phone numbers, or goal text. Mutate structure \
only. Respond with a JSON array of variant objects, each like:\n\
{\"id\":\"...\",\"description\":\"...\",\"verbosity\":\"chatty\"}\n\
or {\"id\":\"...\",\"description\":\"...\",\"extra_guardrails\":[\"...\"]}.";
    let user = format!(
        "Here is the current composed caller instruction. Propose up to {llm_count} \
distinct structural mutations (JSON array):\n\n---\n{current_si}\n---"
    );
    match backend.complete_json(system, &user).await {
        Ok(text) => Ok(Some(text)),
        Err(_) => Ok(None),
    }
}

/// Tolerant parse of the proposer's JSON array (port of `gen.py:_parse_candidates`).
fn parse_llm_candidates(text: &str, max: usize) -> Vec<PromptVariant> {
    use lks_core::optimize::{validate_variant, PromptVariant};
    let stripped = text.trim();
    let stripped = if let Some(rest) = stripped.strip_prefix("```") {
        if let Some(end) = rest.find("```") {
            &rest[..end]
        } else {
            stripped
        }
    } else {
        stripped
    };
    let start = stripped.find('[');
    let end = stripped.rfind(']');
    let (Some(start), Some(end)) = (start, end) else {
        return Vec::new();
    };
    if end <= start {
        return Vec::new();
    }
    let raw: Json = match serde_json::from_str(&stripped[start..=end]) {
        Ok(v) => v,
        Err(_) => return Vec::new(),
    };
    let Some(list) = raw.as_array() else {
        return Vec::new();
    };
    let mut out: Vec<PromptVariant> = Vec::new();
    for (i, item) in list.iter().enumerate() {
        let Some(m) = item.as_object() else { continue };
        let mut m = m.clone();
        m.entry("id".to_string())
            .or_insert(json!(format!("llm-{i}")));
        let v = lks_core::optimize::variant_from_dict(&m);
        if validate_variant(&v).is_empty() {
            out.push(v);
        }
        if out.len() >= max {
            break;
        }
    }
    out
}
