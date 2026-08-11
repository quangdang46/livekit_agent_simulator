"""`lks` CLI — same public ops as the MCP server (see ops module docstring).

Defaults project root to CWD; use `--root` for another target repo.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Optional

import typer


def _ensure_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:
                pass


_ensure_utf8_stdio()

from . import cli_render, ops
from .config import ConfigError
from .scenario import ScenarioError

app = typer.Typer(
    name="lks",
    help="Simulate an AI caller against a LiveKit voice agent (parity with MCP tools).",
)

ROOT_OPTION = typer.Option(None, "--root", help="Project root (default: current directory)")

# Shared --json flag: raw JSON instead of a human-readable rich table.
# The parameter name must be `as_json`, not `json` — cli.py uses the stdlib
# `json` module in function bodies and a literal `json` param would shadow it.
# Typer derives the flag name from the Option string, not the param name.
JSON_OPTION = typer.Option(False, "--json", help="Emit raw JSON instead of a human-readable table")


def _root(root: Optional[Path]) -> Path:
    return (root or Path.cwd()).resolve()


def _print(data: Any) -> None:
    typer.echo(json.dumps(data, ensure_ascii=False, indent=2))


def _emit(data: Any, as_json: bool, render_fn: cli_render.RENDERER) -> None:
    """Emit ``data`` as raw JSON (``--json``) or via a rich table renderer."""
    if as_json:
        _print(data)  # byte-identical JSON path — unchanged
        return
    render_fn(cli_render.make_console(), data)


def _run_failed(result: dict[str, Any], *, strict_judge: bool = False) -> bool:
    """CI gate: hard fails on status/assert/script; judge only if strict_judge."""
    from .suite import evaluate_run_result

    # Suite payload from execute-all
    if result.get("suite") and isinstance(result.get("suite"), dict):
        return not bool(result["suite"].get("ok"))
    return not evaluate_run_result(result, strict_judge=strict_judge)["ok"]


def _run(coro: Any) -> Any:
    try:
        return asyncio.run(coro)
    except KeyboardInterrupt:
        typer.secho("\nInterrupted — stopping.", fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(130)
    except (ConfigError, ScenarioError, RuntimeError) as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(1)


@app.command()
def init(root: Optional[Path] = ROOT_OPTION) -> None:
    """Scaffold .agent-sim/ in the target repo. (MCP: init_project)"""
    _print(ops.init_project(_root(root)))


@app.command()
def guide() -> None:
    """Print setup/ops guide for agents and humans. (MCP: guide)"""
    typer.echo(ops.guide()["text"])


@app.command()
def web(
    run_id: Optional[str] = typer.Argument(
        None,
        help="Run id under .agent-sim/reports/ (default: home list of all runs)",
    ),
    port: int = typer.Option(8765, "--port", "-p"),
    host: str = typer.Option("127.0.0.1", "--host"),
    no_open: bool = typer.Option(False, "--no-open", help="Do not open a browser"),
    root: Optional[Path] = ROOT_OPTION,
) -> None:
    """Local report player: audio + transcript sync while playing. (MCP: web)"""
    try:
        typer.echo("Starting report UI — Ctrl+C to stop")
        info = ops.web(
            _root(root),
            run_id=run_id,
            host=host,
            port=port,
            open_browser=not no_open,
            blocking=True,
        )
        _print({k: v for k, v in info.items() if k not in ("server", "thread")})
    except KeyboardInterrupt:
        typer.echo("\nStopped report UI.")
        raise typer.Exit(0)
    except (ConfigError, FileNotFoundError, OSError) as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(1)


@app.command()
def preflight(
    root: Optional[Path] = ROOT_OPTION,
    no_connectivity: bool = typer.Option(False, "--no-connectivity", help="Skip LiveKit API check"),
    as_json: bool = JSON_OPTION,
) -> None:
    """Check config + LiveKit connectivity without running a scenario. (MCP: preflight)"""
    result = _run(ops.preflight(_root(root), connectivity=not no_connectivity))
    _emit(result, as_json, cli_render.render_preflight)
    if not result.get("ok"):
        raise typer.Exit(1)


@app.command("scenarios")
def scenarios_cmd(root: Optional[Path] = ROOT_OPTION, as_json: bool = JSON_OPTION) -> None:
    """List scenarios. (MCP: list_scenarios)"""
    try:
        _emit(ops.list_scenarios(_root(root)), as_json, cli_render.render_scenarios)
    except ConfigError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(1)


@app.command()
def cues(
    root: Optional[Path] = ROOT_OPTION,
    asset: Optional[str] = typer.Option(
        None,
        "--resolve",
        help="Resolve one asset id/path and print path (builtin:voice.barge_short, builtin:noise.loud, my.wav, …)",
    ),
    as_json: bool = JSON_OPTION,
) -> None:
    """List built-in + target room_pcm cues. (MCP: list_cues)"""
    from .audio.cue_catalog import describe_resolution
    from .config import load_config

    r = _root(root)
    if asset:
        try:
            cfg = load_config(r)
            _print(
                describe_resolution(
                    asset, project_root=cfg.project_root, cues_config=cfg.cues
                )
            )
        except ConfigError:
            _print(describe_resolution(asset, project_root=r if (r / ".agent-sim").is_dir() else None))
        return
    try:
        _emit(ops.list_cues(r), as_json, cli_render.render_cues)
    except Exception as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(1)


@app.command()
def plugins(root: Optional[Path] = ROOT_OPTION, as_json: bool = JSON_OPTION) -> None:
    """List verify plugins. (MCP: list_plugins)"""
    try:
        _emit(ops.list_plugins(_root(root)), as_json, cli_render.render_plugins)
    except ConfigError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(1)


@app.command()
def validate(
    scenario_id: str,
    root: Optional[Path] = ROOT_OPTION,
    as_json: bool = JSON_OPTION,
) -> None:
    """Validate one scenario. (MCP: validate_scenario)"""
    result = ops.validate_scenario(_root(root), scenario_id)
    _emit(result, as_json, cli_render.render_validate)
    if not result.get("valid"):
        raise typer.Exit(1)


@app.command()
def export(scenario_id: str, root: Optional[Path] = ROOT_OPTION) -> None:
    """Export parsed scenario JSON. (MCP: export_scenario)"""
    try:
        _print(ops.export_scenario(_root(root), scenario_id))
    except ConfigError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(1)


@app.command()
def convert(
    scenario_id: str,
    force: bool = typer.Option(False, "--force", help="Overwrite existing .yaml"),
    root: Optional[Path] = ROOT_OPTION,
) -> None:
    """Convert a legacy .jsonl scenario to .yaml (idempotent; keeps the .jsonl)."""
    try:
        _print(ops.convert_scenario(_root(root), scenario_id, force=force))
    except (ConfigError, ScenarioError) as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(1)


@app.command("scenario-init")
def scenario_init_cmd(
    scenario_id: str = typer.Argument(..., help="New scenario id (filename without .yaml)"),
    force: bool = typer.Option(False, "--force", help="Overwrite existing file"),
    root: Optional[Path] = ROOT_OPTION,
) -> None:
    """Scaffold a scenario YAML with # guide comments + example sections. (MCP: init_scenario)"""
    try:
        _print(ops.init_scenario(_root(root), scenario_id, force=force))
    except ConfigError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(1)


@app.command()
def execute(
    scenario_id: str,
    root: Optional[Path] = ROOT_OPTION,
    strict_judge: bool = typer.Option(
        False,
        "--strict-judge",
        help="Also fail CI exit if LLM PassCriteria judge verdict is fail",
    ),
    repeat: int = typer.Option(
        1,
        "--repeat",
        "-n",
        help="Run scenario N times for flake control (pass@k)",
    ),
    pass_at_k: Optional[int] = typer.Option(
        None,
        "--pass-at-k",
        "-k",
        help="Minimum hard-pass iterations (default = repeat). Example: --repeat 5 --pass-at-k 3",
    ),
    name: Optional[str] = typer.Option(
        None,
        "--name",
        help="Override slug after seq prefix (e.g. demo → reports/001-demo/)",
    ),
    agent_name: Optional[str] = typer.Option(
        None,
        "--agent-name",
        help="Override the target LiveKit worker name for this run (no config edit). "
        "Enables parallel worktree workflows.",
    ),
    optimized: Optional[str] = typer.Option(
        None,
        "--optimized",
        help="Apply a saved `lks optimize` artifact (.agent-sim/optimized/<name>/prompt.yaml) "
        "as the persona-prompt override for this run.",
    ),
    as_json: bool = JSON_OPTION,
) -> None:
    """Validate then execute one scenario from .agent-sim/scenarios/. (MCP: execute_scenario)"""
    result = _run(
        ops.execute_scenario(
            _root(root),
            scenario_id,
            repeat=repeat,
            pass_at_k=pass_at_k,
            run_name=name,
            agent_name=agent_name,
            optimized=optimized,
        )
    )
    from .suite import evaluate_run_result

    gate = evaluate_run_result(result, strict_judge=strict_judge)
    result = {**result, "gate": gate}
    _emit(result, as_json, cli_render.render_execute)
    if _run_failed(result, strict_judge=strict_judge):
        raise typer.Exit(1)


@app.command("execute-all")
def execute_all_cmd(
    scenario_ids: Optional[list[str]] = typer.Argument(
        None,
        help="Optional scenario ids; omit to run all valid scenarios",
    ),
    tag: Optional[str] = typer.Option(None, help="Only scenarios with this tag (when ids omitted)"),
    strict_judge: bool = typer.Option(
        False,
        "--strict-judge",
        help="Fail suite if any LLM judge verdict is fail (default: hard gates only)",
    ),
    no_report: bool = typer.Option(
        False,
        "--no-report",
        help="Do not write suite-*.json/md under .agent-sim/reports/",
    ),
    repeat: int = typer.Option(
        1,
        "--repeat",
        "-n",
        help="Repeat each scenario N times for flake control (pass@k)",
    ),
    pass_at_k: Optional[int] = typer.Option(
        None,
        "--pass-at-k",
        "-k",
        help="Minimum hard-pass iterations per scenario (default = repeat)",
    ),
    parallel: int = typer.Option(
        1,
        "--parallel",
        "-p",
        help="Run up to N scenarios at once (default 1 = sequential). "
        "Within each scenario, --repeat stays sequential.",
    ),
    wait: float = typer.Option(
        0.0,
        "--wait",
        help="Cooldown seconds after a scenario finishes before the next starts "
        "on that concurrency slot (sequential: between scenarios). "
        "Default 0. Does not delay the first wave; does not replace agent-join wait.",
    ),
    agent_name: Optional[str] = typer.Option(
        None,
        "--agent-name",
        help="Override the target LiveKit worker name for this run (no config edit). "
        "Enables parallel worktree workflows: each worktree registers its own "
        "agent under a distinct name via VOICE_AI_AGENT_NAME and you point lks "
        "at it per invocation.",
    ),
    root: Optional[Path] = ROOT_OPTION,
    as_json: bool = JSON_OPTION,
) -> None:
    """Execute multiple scenarios; print suite matrix + CI gate. (MCP: execute_scenarios)"""
    result = _run(
        ops.execute_scenarios(
            _root(root),
            scenario_ids=list(scenario_ids) if scenario_ids else None,
            tag=tag,
            strict_judge=strict_judge,
            write_report=not no_report,
            repeat=repeat,
            pass_at_k=pass_at_k,
            parallel=parallel,
            wait_s=wait,
            agent_name=agent_name,
        )
    )
    _emit(result, as_json, cli_render.render_execute_all)
    if _run_failed(result, strict_judge=strict_judge):
        raise typer.Exit(1)


@app.command("execute-dict")
def execute_dict_cmd(
    file: Optional[Path] = typer.Option(
        None,
        "--file",
        "-f",
        help="JSON file with scenario dict; omit to read JSON from stdin",
    ),
    root: Optional[Path] = ROOT_OPTION,
    name: Optional[str] = typer.Option(
        None,
        "--name",
        help="Override slug after seq prefix (e.g. demo → reports/001-demo/)",
    ),
    agent_name: Optional[str] = typer.Option(
        None,
        "--agent-name",
        help="Override the target LiveKit worker name for this run (no config edit).",
    ),
    as_json: bool = JSON_OPTION,
) -> None:
    """Validate then run an in-memory scenario JSON. (MCP: execute_scenario_dict)"""
    try:
        if file is not None:
            scenario = json.loads(file.read_text(encoding="utf-8"))
        else:
            scenario = json.load(sys.stdin)
    except (OSError, json.JSONDecodeError) as e:
        typer.secho(f"Invalid scenario JSON: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    if not isinstance(scenario, dict):
        typer.secho("Scenario JSON must be an object", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    result = _run(
        ops.execute_scenario_dict(_root(root), scenario, run_name=name, agent_name=agent_name)
    )
    from .suite import evaluate_run_result

    gate = evaluate_run_result(result, strict_judge=False)
    result = {**result, "gate": gate}
    _emit(result, as_json, cli_render.render_execute)
    if _run_failed(result):
        raise typer.Exit(1)


@app.command()
def optimize(
    scenario_ids: str = typer.Argument(
        ...,
        help="Comma-separated scenario ids (dataset) to optimize over",
    ),
    held_out: Optional[str] = typer.Option(
        None,
        "--held-out",
        help="Scenario id held out for generalization check (must not regress)",
    ),
    candidates: int = typer.Option(4, "--candidates", "-c", help="Max candidate variants to evaluate"),
    max_candidates: int = typer.Option(6, "--max-candidates", help="Cap on LLM-proposed variants"),
    strict_judge: bool = typer.Option(False, "--strict-judge", help="Treat judge fail as hard fail"),
    repeat: int = typer.Option(1, "--repeat", "-n", help="Run each scenario N times for pass@k"),
    pass_at_k: Optional[int] = typer.Option(None, "--pass-at-k", "-k", help="Min hard-pass iterations"),
    agent_name: Optional[str] = typer.Option(None, "--agent-name", help="Override target worker name"),
    name: Optional[str] = typer.Option(None, "--name", help="Artifact slug (default auto)"),
    root: Optional[Path] = ROOT_OPTION,
    as_json: bool = JSON_OPTION,
) -> None:
    """Run the persona-prompt optimizer over a dataset (live benchmark loop).

    Writes the winning variant to .agent-sim/optimized/<name>/; apply it to a
    run with `lks execute <scenario> --optimized <name>`. (MCP: optimize_persona)
    """
    ids = [s.strip() for s in scenario_ids.split(",") if s.strip()]
    if not ids:
        typer.secho("scenario_ids must be a non-empty comma-separated list", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    result = _run(
        ops.optimize_persona(
            _root(root),
            ids,
            held_out=held_out,
            candidates=candidates,
            max_candidates=max_candidates,
            strict_judge=strict_judge,
            repeat=repeat,
            pass_at_k=pass_at_k,
            agent_name=agent_name,
            name=name,
        )
    )
    _emit(result, as_json, cli_render.render_optimize)
    if result.get("winner") is None:
        typer.secho("No candidate beat baseline — keeping the builtin prompt.", fg=typer.colors.YELLOW)
        raise typer.Exit(0)


@app.command()
def status(run_id: str, root: Optional[Path] = ROOT_OPTION, as_json: bool = JSON_OPTION) -> None:
    """Run status from SQLite. (MCP: get_run_status)"""
    _emit(_run(ops.get_run_status(_root(root), run_id)), as_json, cli_render.render_status)


@app.command()
def log(
    run_id: str,
    kind: Optional[str] = typer.Option(None, help="Event kind, trailing * for prefix (tool.*)"),
    turn: Optional[int] = typer.Option(None),
    source: Optional[str] = typer.Option(None),
    since_mono_ms: Optional[int] = typer.Option(None, help="Only events at/after this mono ms"),
    limit: int = typer.Option(200),
    root: Optional[Path] = ROOT_OPTION,
    as_json: bool = JSON_OPTION,
) -> None:
    """Filtered view of events.jsonl. (MCP: get_run_log)"""
    try:
        _emit(
            ops.get_run_log(
                _root(root),
                run_id,
                kind=kind,
                turn=turn,
                source=source,
                since_mono_ms=since_mono_ms,
                limit=limit,
            ),
            as_json,
            cli_render.render_log,
        )
    except ConfigError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(1)


@app.command()
def report(run_id: str, root: Optional[Path] = ROOT_OPTION, as_json: bool = JSON_OPTION) -> None:
    """Summary + verdict + suspicious turns (includes caller.behavior_summary). (MCP: get_run_report)"""
    _emit(_run(ops.get_run_report(_root(root), run_id)), as_json, cli_render.render_report)


@app.command()
def compare(
    run_id_a: str = typer.Argument(..., help="Baseline run id when --baseline, else run A"),
    run_id_b: str = typer.Argument(..., help="Candidate run id when --baseline, else run B"),
    baseline: bool = typer.Option(
        False,
        "--baseline",
        help="Treat run_id_a as golden baseline; attach hard regression gate (CI exit 1 if fail)",
    ),
    max_ttfw_regression_ms: float = typer.Option(
        1500.0, "--max-ttfw-regression-ms", help="Max allowed TTFW increase vs baseline"
    ),
    max_turn_p95_regression_ms: float = typer.Option(
        2000.0, "--max-turn-p95-regression-ms", help="Max allowed turn p95 increase vs baseline"
    ),
    max_duration_regression_ms: float = typer.Option(
        30000.0, "--max-duration-regression-ms", help="Max allowed duration increase vs baseline"
    ),
    max_barge_recovery_drop: float = typer.Option(
        0.0,
        "--max-barge-recovery-drop",
        help="Max allowed barge_recovery_rate drop vs baseline (0 = no drop)",
    ),
    root: Optional[Path] = ROOT_OPTION,
    as_json: bool = JSON_OPTION,
) -> None:
    """Diff two runs. With --baseline, hard-fail on latency/assert regression. (MCP: compare_runs)"""
    if baseline:
        result = _run(
            ops.compare_runs_with_baseline(
                _root(root),
                run_id_a,
                run_id_b,
                max_ttfw_regression_ms=max_ttfw_regression_ms,
                max_turn_p95_regression_ms=max_turn_p95_regression_ms,
                max_duration_regression_ms=max_duration_regression_ms,
                max_barge_recovery_drop=max_barge_recovery_drop,
            )
        )
        _emit(result, as_json, cli_render.render_compare)
        gate = result.get("gate") if isinstance(result, dict) else None
        if isinstance(gate, dict) and not gate.get("ok", True):
            raise typer.Exit(code=1)
        return
    _emit(_run(ops.compare_runs(_root(root), run_id_a, run_id_b)), as_json, cli_render.render_compare)


@app.command()
def runs(
    limit: int = typer.Option(20),
    scenario_id: Optional[str] = typer.Option(None, "--scenario"),
    root: Optional[Path] = ROOT_OPTION,
    as_json: bool = JSON_OPTION,
) -> None:
    """Run history, newest first. (MCP: list_runs)"""
    _emit(
        _run(ops.list_runs(_root(root), limit=limit, scenario_id=scenario_id)),
        as_json,
        cli_render.render_runs,
    )


@app.command("scenario-from-run")
def scenario_from_run_cmd(
    run_id: str = typer.Argument(..., help="Run ID to promote"),
    scenario_id: Optional[str] = typer.Option(
        None,
        "--id",
        help="Override draft scenario id (default: auto from source)")
        ,
    write: bool = typer.Option(
        False,
        "--write",
        "-w",
        help="Write draft .yaml to .agent-sim/scenarios/",
    ),
    root: Optional[Path] = ROOT_OPTION,
) -> None:
    """Promote a finished run into a draft scenario YAML (fail → golden). (MCP: scenario_from_run)"""
    try:
        _print(ops.scenario_from_run(_root(root), run_id, scenario_id=scenario_id, write=write))
    except (ConfigError, FileNotFoundError) as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(1)


@app.command("mcp")
def mcp_serve() -> None:
    """Start the MCP server (stdio). Same tools as CLI ops — for Cursor / Claude / etc.

    Config example::

        {
          "mcpServers": {
            "livekit-agent-simulator": {
              "command": "lks",
              "args": ["mcp"]
            }
          }
        }
    """
    from .mcp_server import main as mcp_main

    mcp_main()


def main() -> None:
    app()


if __name__ == "__main__":
    main()

