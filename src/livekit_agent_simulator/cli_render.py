"""Rich table renderers for `lks` CLI list/table-shaped commands.

Every renderer takes ``(console, data)`` and prints to the console. The pure
``render_text`` helper re-renders through a recorded, color-less Console and
returns plain text — used by tests and available for any non-TTY embedding.

``--json`` remains the machine/CI path: it bypasses these renderers entirely
and prints the raw result dict (see ``cli._emit``).
"""

from __future__ import annotations

import io
from typing import Any, Callable

from rich.console import Console
from rich.style import Style
from rich.table import Table

RENDERER = Callable[[Console, Any], None]


def make_console(
    *,
    record: bool = False,
    force_terminal: bool | None = None,
    width: int | None = None,
) -> Console:
    """Fresh Console per call — never a module-level singleton.

    Rich binds ``file=sys.stdout`` at construction; the CLI reconfigures stdout
    to UTF-8 at import and typer's CliRunner swaps it per invocation, so a
    cached console would write to the wrong stream. ``highlight=False`` keeps
    cell values (run_ids, paths, filenames) free of incidental ANSI styling.
    """
    return Console(
        record=record,
        force_terminal=force_terminal,
        width=width,
        highlight=False,
    )


def render_text(fn: RENDERER, data: Any, *, width: int = 180) -> str:
    """Render ``fn`` through a recorded, color-less Console and return text.

    Renders into an explicit ``io.StringIO`` buffer (never the real stdout), so
    the ✓/✗ glyphs and table box-drawing characters survive on Windows where
    the inherited console codepage may be cp1252.
    """
    buf = io.StringIO()
    c = Console(file=buf, force_terminal=False, width=width, highlight=False)
    fn(c, data)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# formatting helpers (every one maps None -> "−")
# ---------------------------------------------------------------------------

_DASH = "−"


def fmt_bool(v: Any) -> str:
    if v is None:
        return _DASH
    return "✓" if bool(v) else "✗"


def fmt_tristate(v: Any) -> str:
    if v is None or (isinstance(v, str) and v.lower() in ("skipped", "none", "n/a")):
        return _DASH
    if isinstance(v, bool):
        return "✓" if v else "✗"
    s = str(v).lower()
    return "✓" if s in ("pass", "passed", "ok", "done", "true") else "✗"


def fmt_ms(v: Any) -> str:
    if v is None:
        return _DASH
    try:
        ms = float(v)
    except (TypeError, ValueError):
        return _DASH
    if ms >= 1000:
        return f"{ms / 1000:.1f}s"
    return f"{int(ms)}ms"


def fmt_pct(v: Any) -> str:
    if v is None:
        return _DASH
    try:
        f = float(v)
    except (TypeError, ValueError):
        return _DASH
    return f"{f * 100:.0f}%"


def fmt_list(v: Any) -> str:
    if not v:
        return _DASH
    return ", ".join(str(x) for x in v)


def truncate(v: Any, n: int = 80) -> str:
    if v is None:
        return _DASH
    s = str(v).replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def fmt_utc(v: Any) -> str:
    """ISO utc -> 'YYYY-MM-DD HH:MM:SS'; strips fractional seconds / tz suffix."""
    if not v:
        return _DASH
    s = str(v)
    # tolerate trailing 'Z' or '+00:00' / fractional seconds
    t = s.split("T", 1)[-1] if "T" in s else s
    t = t.split("+")[0].split("Z")[0].split(".")[0]
    if "T" in s:
        return s.split("T")[0] + " " + t
    return t or _DASH


def status_style(console: Console, status: Any) -> Style:
    s = str(status or "").lower()
    if s in ("done", "pass", "passed", "ok", "complete", "completed"):
        return Style(color="green", bold=True)
    if s in ("fail", "failed", "error", "fatal"):
        return Style(color="red", bold=True)
    if s in ("warn", "warning", "soft", "maybe", "partial"):
        return Style(color="yellow")
    if s in ("running", "queued", "in-progress", "connecting", "active"):
        return Style(color="cyan")
    if s in ("skip", "skipped", "none"):
        return Style(dim=True)
    return Style()


def _table(columns: list[str], *, title: str | None = None) -> Table:
    t = Table(title=title)
    for col in columns:
        t.add_column(col, overflow="fold", no_wrap=False)
    return t


def _kv(console: Console, pairs: list[tuple[str, Any]], *, title: str | None = None) -> None:
    t = _table(["key", "value"], title=title)
    for k, v in pairs:
        t.add_row(k, v)
    console.print(t)


# ---------------------------------------------------------------------------
# renderers — one per list/table-shaped CLI command
# ---------------------------------------------------------------------------


def render_scenarios(console: Console, rows: list[dict[str, Any]]) -> None:
    if not rows:
        console.print("no scenarios")
        return
    t = _table(
        [
            "id",
            "locale",
            "max_turns",
            "first_speaker",
            "tags",
            "execute",
            "dispatch",
            "caller_mode",
            "pass",
            "script",
            "file",
            "error",
        ]
    )
    for r in rows:
        if r.get("error"):
            t.add_row(
                "✗",
                _DASH,
                _DASH,
                _DASH,
                _DASH,
                _DASH,
                _DASH,
                _DASH,
                _DASH,
                _DASH,
                r.get("file") or _DASH,
                truncate(r["error"], 60),
                style="red",
            )
            continue
        t.add_row(
            r.get("id") or _DASH,
            r.get("locale") or _DASH,
            truncate(r.get("max_turns")),
            r.get("first_speaker") or _DASH,
            fmt_list(r.get("tags")),
            fmt_bool(r.get("has_execute")),
            fmt_bool(r.get("has_dispatch")),
            r.get("caller_mode") or _DASH,
            truncate(r.get("pass_criteria")),
            truncate(r.get("script_steps")),
            r.get("file") or _DASH,
            _DASH,
        )
    console.print(t)


def render_runs(console: Console, rows: list[dict[str, Any]]) -> None:
    if not rows:
        console.print("no runs")
        return
    t = _table(
        [
            "run_id",
            "status",
            "scenario_id",
            "room_name",
            "agent_name",
            "started_utc",
            "duration",
            "turns",
            "tool_errors",
            "verdict",
        ]
    )
    for r in rows:
        t.add_row(
            r.get("run_id") or _DASH,
            r.get("status") or _DASH,
            r.get("scenario_id") or _DASH,
            truncate(r.get("room_name"), 30),
            r.get("agent_name") or _DASH,
            fmt_utc(r.get("started_utc")),
            fmt_ms(r.get("duration_ms")),
            truncate(r.get("turn_count")),
            truncate(r.get("tool_errors")),
            truncate(r.get("verdict"), 20),
            style=status_style(console, r.get("status")),
        )
    console.print(t)


def render_plugins(console: Console, data: dict[str, Any]) -> None:
    console.print(f"entry point group: {data.get('entry_point_group') or _DASH}")
    entry = sorted(set(data.get("verify_plugins") or []))
    local = sorted(set(data.get("local_modules") or []))
    if not entry and not local:
        console.print("no verify plugins")
        return
    t = _table(["name", "source"])
    for name in entry:
        t.add_row(name, "entrypoint")
    for name in local:
        t.add_row(name, "local")
    console.print(t)
    load = data.get("load") or {}
    errors = load.get("errors") or []
    if errors:
        et = _table(["load error"])
        for e in errors:
            et.add_row(truncate(e, 100))
        console.print(et)


def render_cues(console: Console, data: dict[str, Any]) -> None:
    order = data.get("resolve_order") or []
    if order:
        console.print("resolve order: " + " → ".join(order))
    aliases = data.get("aliases") or {}
    if aliases:
        console.print("aliases: " + ", ".join(f"{k}={v}" for k, v in sorted(aliases.items())))
    for d in data.get("extra_dirs") or []:
        console.print(f"extra dir: {d}")

    builtin = data.get("builtin") or []
    if builtin:
        bt = _table(["id", "file", "kind", "interrupt_class", "locale", "text", "description", "ref"])
        for c in builtin:
            bt.add_row(
                c.get("id") or _DASH,
                c.get("file") or _DASH,
                c.get("kind") or _DASH,
                c.get("interrupt_class") or _DASH,
                c.get("locale") or _DASH,
                truncate(c.get("text"), 40),
                truncate(c.get("description"), 50),
                c.get("ref") or _DASH,
            )
        console.print(bt)

    target = data.get("target") or []
    if target:
        tt = _table(["id", "file", "overrides_builtin", "ref"])
        for c in target:
            tt.add_row(
                c.get("id") or _DASH,
                c.get("file") or _DASH,
                fmt_bool(c.get("overrides_builtin")),
                c.get("ref") or _DASH,
            )
        console.print(tt)

    if not builtin and not target:
        console.print("no cues")


def render_validate(console: Console, data: dict[str, Any]) -> None:
    if not data.get("valid"):
        console.print("validation: ✗ not valid", style="red bold")
        console.print(f"error: {truncate(data.get('error'), 200)}")
        avail = data.get("available")
        if avail:
            console.print(f"available: {fmt_list(avail)}")
        return
    console.print("validation: ✓ valid", style="green bold")
    _kv(
        console,
        [
            ("id", data.get("id") or _DASH),
            ("locale", data.get("locale") or _DASH),
            ("max_turns", truncate(data.get("max_turns"))),
            ("timeout_s", truncate(data.get("timeout_s"))),
            ("first_speaker", data.get("first_speaker") or _DASH),
            ("execute", fmt_bool(data.get("has_execute"))),
            ("dispatch", fmt_bool(data.get("has_dispatch"))),
            ("pass_criteria", truncate(data.get("pass_criteria"))),
            ("warnings", truncate(len(data.get("warnings") or []))),
        ],
    )
    authoring = data.get("authoring") or {}
    if authoring:
        console.print(f"authoring tier: {authoring.get('tier') or _DASH}")
    warnings = data.get("warnings") or []
    if warnings:
        for w in warnings:
            console.print(f"  ⚠ {w}", style="yellow")
    else:
        console.print("warnings: none")


def render_preflight(console: Console, data: dict[str, Any]) -> None:
    checks = data.get("checks") or []
    if not checks:
        console.print("no checks")
        return
    t = _table(["check", "status", "detail"])
    for c in checks:
        status = c.get("status") or ""
        mark = {"pass": "✓", "warn": "⚠", "fail": "✗"}.get(status, status)
        t.add_row(
            c.get("name") or _DASH,
            mark,
            truncate(c.get("detail"), 100),
            style=status_style(console, status),
        )
    console.print(t)
    ok = bool(data.get("ok"))
    console.print(
        "preflight ok" if ok else "preflight failed",
        style="green bold" if ok else "red bold",
    )


def render_execute(console: Console, data: dict[str, Any]) -> None:
    val = data.get("validation") or {}
    ok = bool(data.get("ok"))
    _kv(
        console,
        [
            ("validation", fmt_bool(val.get("valid") if isinstance(val, dict) else None)),
            ("repeat", truncate(data.get("repeat"))),
            ("pass_at_k", truncate(data.get("pass_at_k"))),
            ("hard_passes", truncate(data.get("hard_passes"))),
            ("ok", "✓" if ok else "✗"),
            ("run_id", data.get("run_id") or _DASH),
            ("status", data.get("status") or _DASH),
        ],
        title="execute",
    )
    iters = data.get("iterations") or []
    if iters:
        t = _table(
            ["i", "run_id", "status", "gate", "ok", "ttfw", "p50", "p95", "hard_reasons"],
            title="iterations",
        )
        for it in iters:
            t.add_row(
                str(it.get("i") or _DASH),
                it.get("run_id") or _DASH,
                it.get("status") or _DASH,
                it.get("gate") or _DASH,
                fmt_bool(it.get("ok")),
                fmt_ms(it.get("ttfw_ms")),
                fmt_ms(it.get("turn_p50_ms")),
                fmt_ms(it.get("turn_p95_ms")),
                fmt_list(it.get("hard_reasons")),
                style=status_style(console, it.get("status")),
            )
        console.print(t)
    if not ok:
        reasons = data.get("gate") or {}
        hard = reasons.get("hard_reasons") if isinstance(reasons, dict) else None
        if hard:
            console.print("hard fail: " + fmt_list(hard), style="red bold")


def render_execute_all(console: Console, data: dict[str, Any]) -> None:
    suite = data.get("suite") or {}
    totals = suite.get("totals") or {}
    ok = bool(suite.get("ok"))
    console.print(
        f"suite ok (CI) · exit {suite.get('exit_code')}"
        if ok
        else f"suite FAILED (CI) · exit {suite.get('exit_code')}",
        style="green bold" if ok else "red bold",
    )
    console.print(
        f"total={totals.get('total') or 0} "
        f"passed={totals.get('passed_gate') or 0} "
        f"hard_fail={totals.get('failed_hard') or 0} "
        f"soft_judge={totals.get('failed_soft_judge') or 0}"
    )
    rows = suite.get("matrix") or data.get("results") or []
    if not rows:
        console.print("no results")
        return
    t = _table(
        [
            "scenario",
            "gate",
            "status",
            "assert",
            "script",
            "judge",
            "p50",
            "p95",
            "ttfw",
            "duration",
            "run_id",
            "error",
        ]
    )
    hard_lines: list[str] = []
    for r in rows:
        verdict = r.get("judge_verdict")
        if r.get("hard_reasons"):
            hard_lines.append(f"↳ {r.get('scenario_id') or '?'}: {fmt_list(r['hard_reasons'])}")
        t.add_row(
            r.get("scenario_id") or _DASH,
            r.get("gate") or _DASH,
            r.get("status") or _DASH,
            fmt_tristate(r.get("assert_pass")),
            fmt_tristate(r.get("script_pass")),
            verdict if verdict is not None else _DASH,
            fmt_ms(r.get("turn_p50_ms")),
            fmt_ms(r.get("turn_p95_ms")),
            fmt_ms(r.get("ttfw_ms")),
            fmt_ms(r.get("duration_ms")),
            r.get("run_id") or _DASH,
            truncate(r.get("error"), 50),
            style=status_style(console, r.get("status")),
        )
    console.print(t)
    for line in hard_lines:
        console.print(line, style="red")


def render_compare(console: Console, data: dict[str, Any]) -> None:
    if data.get("error"):
        console.print(f"compare error: {data.get('error')}", style="red bold")
        return
    a, b = data.get("a") or {}, data.get("b") or {}
    console.print(f"{a.get('run_id') or '?'} → {b.get('run_id') or '?'}")
    labels = [
        ("duration_ms", "duration"),
        ("turn_count", "turn_count"),
        ("tool_errors", "tool_errors"),
        ("interruptions", "interruptions"),
        ("turn_taking_p50", "turn_p50"),
        ("turn_taking_p95", "turn_p95"),
        ("ttfw_ms", "ttfw"),
        ("recovery_p50_ms", "recovery_p50"),
        ("barge_count", "barge_count"),
        ("barge_recovery_rate", "barge_recovery_rate"),
        ("talk_ratio", "talk_ratio"),
        ("verdict", "verdict"),
        ("assert_pass", "assert_pass"),
    ]
    has_gate = isinstance(data.get("gate"), dict)
    cols = ["metric", "a", "b", "delta"] + (["gate"] if has_gate else [])
    t = _table(cols)
    for key, label in labels:
        av, bv = a.get(key), b.get(key)
        if key in ("verdict", "assert_pass"):
            avs = fmt_tristate(av) if key == "assert_pass" else (av or _DASH)
            bvs = fmt_tristate(bv) if key == "assert_pass" else (bv or _DASH)
            delta = _DASH
        else:
            avs = fmt_ms(av) if key in ("duration_ms", "ttfw_ms", "recovery_p50_ms") else (
                fmt_pct(av) if key in ("barge_recovery_rate", "talk_ratio") else truncate(av)
            )
            bvs = fmt_ms(bv) if key in ("duration_ms", "ttfw_ms", "recovery_p50_ms") else (
                fmt_pct(bv) if key in ("barge_recovery_rate", "talk_ratio") else truncate(bv)
            )
            delta = _delta(av, bv, key)
        row = [label, avs, bvs, delta]
        if has_gate:
            row.append(_gate_cell(data, key))
        t.add_row(*row)
    console.print(t)
    if has_gate:
        gate = data["gate"]
        reasons = gate.get("reasons") or []
        if reasons:
            console.print("gate fail: " + fmt_list(reasons), style="red bold")


def _delta(av: Any, bv: Any, key: str) -> str:
    if av is None or bv is None:
        return _DASH
    try:
        d = float(bv) - float(av)
    except (TypeError, ValueError):
        return _DASH
    if key in ("duration_ms", "ttfw_ms", "recovery_p50_ms"):
        return ("+" if d > 0 else "") + fmt_ms(abs(d))
    if key in ("barge_recovery_rate", "talk_ratio"):
        return f"{d:+.2f}"
    if d.is_integer():
        return f"{int(d):+d}"
    return f"{d:+.1f}"


def _gate_cell(data: dict[str, Any], key: str) -> str:
    gate = data.get("gate") or {}
    checks = gate.get("checks") or []
    for c in checks:
        if str(c.get("check", "")).endswith(key):
            if c.get("skipped"):
                return _DASH
            return "✓" if c.get("pass") else "✗"
    return _DASH


def render_status(console: Console, data: dict[str, Any]) -> None:
    if not data.get("found"):
        console.print(f"run {data.get('run_id') or '?'}: not found", style="red bold")
        return
    _kv(
        console,
        [
            ("run_id", data.get("run_id") or _DASH),
            ("status", data.get("status") or _DASH),
            ("scenario_id", data.get("scenario_id") or _DASH),
            ("room_name", truncate(data.get("room_name"), 40)),
            ("started_utc", fmt_utc(data.get("started_utc"))),
            ("ended_utc", fmt_utc(data.get("ended_utc"))),
            ("duration", fmt_ms(data.get("duration_ms"))),
            ("turn_count", truncate(data.get("turn_count"))),
            ("tool_errors", truncate(data.get("tool_errors"))),
            ("report_dir", truncate(data.get("report_dir"), 90)),
        ],
        title="status",
    )


def render_report(console: Console, data: dict[str, Any]) -> None:
    if not data.get("found"):
        console.print(f"run {data.get('run_id') or '?'}: not found", style="red bold")
        return
    s = data.get("summary") or {}
    m = data.get("meta") or {}
    v = s.get("verdict") or {}
    from .metrics import metrics_digest

    md = metrics_digest(s.get("metrics") if isinstance(s.get("metrics"), dict) else None)
    av = s.get("assert_verify") if isinstance(s.get("assert_verify"), dict) else {}
    sv = s.get("script_verify") if isinstance(s.get("script_verify"), dict) else {}
    _kv(
        console,
        [
            ("run_id", data.get("run_id") or _DASH),
            ("status", s.get("status") or _DASH),
            ("duration", fmt_ms(s.get("duration_ms"))),
            ("turn_count", truncate(s.get("turn_count"))),
            ("event_count", truncate(s.get("event_count"))),
            ("tool_calls", truncate(s.get("tool_calls"))),
            ("tool_errors", truncate(s.get("tool_errors"))),
            ("interruptions", truncate(s.get("interruptions"))),
            ("silences", truncate(s.get("silences"))),
            ("caller_mode", s.get("caller_mode") or m.get("caller_mode") or _DASH),
            ("end_reason", s.get("end_reason") or _DASH),
            ("verdict", v.get("verdict") or _DASH),
            ("ttfw", fmt_ms(md.get("ttfw_ms"))),
            ("turn_p50", fmt_ms(md.get("turn_p50_ms"))),
            ("turn_p95", fmt_ms(md.get("turn_p95_ms"))),
            ("recovery_p50", fmt_ms(md.get("recovery_p50_ms"))),
            ("barge_count", truncate(md.get("barge_count"))),
            ("barge_recovery_rate", fmt_pct(md.get("barge_recovery_rate"))),
            ("assert_pass", fmt_tristate(av.get("pass"))),
            ("script_pass", fmt_tristate(sv.get("pass"))),
        ],
        title="report",
    )
    suspicious = data.get("suspicious_turns") or []
    if suspicious:
        t = _table(["turn", "turn_taking_ms", "tool_errors", "interrupted", "reasons"], title="suspicious turns")
        for st in suspicious:
            t.add_row(
                str(st.get("turn") or _DASH),
                fmt_ms(st.get("turn_taking_ms")),
                truncate(st.get("tool_errors")),
                fmt_bool(st.get("interrupted")),
                fmt_list(st.get("reasons")),
            )
        console.print(t)
    for label, key in (("timeline", "timeline_path"), ("events", "events_path"), ("audio", "audio_path")):
        path = data.get(key)
        console.print(f"{label}: {truncate(path, 90) if path else 'not available'}")


def render_optimize(console: Console, data: dict[str, Any]) -> None:
    """Summary table for `lks optimize`."""
    _kv(
        console,
        [
            ("name", data.get("name") or _DASH),
            ("dir", data.get("dir") or _DASH),
            ("baseline_pass_rate", fmt_pct(data.get("baseline_pass_rate"))),
            ("winner", (data.get("winner") or {}).get("id") or "none"),
            ("winner_pass_rate", fmt_pct((data.get("winner") or {}).get("pass_rate"))),
        ],
        title="optimize",
    )
    cands = data.get("candidate_pass_rates") or []
    if cands:
        t = _table(["candidate", "pass_rate"], title="candidates")
        for c in cands:
            t.add_row(str(c.get("id") or _DASH), fmt_pct(c.get("pass_rate")))
        console.print(t)


def render_log(console: Console, data: dict[str, Any]) -> None:
    if not data.get("found"):
        console.print(f"run {data.get('run_id') or '?'}: not found", style="red bold")
        return
    matched = data.get("matched") or 0
    total = data.get("total_events") or 0
    truncated = bool(data.get("truncated"))
    console.print(
        f"run {data.get('run_id') or '?'} · {matched}/{total} events"
        + (" · truncated" if truncated else "")
    )
    events = data.get("events") or []
    if not events:
        console.print("no events")
        return
    t = _table(["ts_mono_ms", "turn", "kind", "source", "detail"])
    for e in events:
        t.add_row(
            str(e.get("ts_mono_ms") or _DASH),
            str(e.get("turn") or _DASH),
            e.get("kind") or _DASH,
            e.get("source") or _DASH,
            _describe(e),
            style=Style(color="cyan" if (e.get("kind") or "").startswith("tool.") else "default"),
        )
    console.print(t)


def _describe(e: dict[str, Any]) -> str:
    """Condensed event detail — mirrors ``event_writer.EventWriter._describe``."""
    spec = e.get("spec") or {}
    kind = e.get("kind") or ""
    if kind.startswith("transcript."):
        return truncate(spec.get("text"), 120)
    if kind.startswith("tool."):
        parts = [str(spec.get("name", "?"))]
        if spec.get("duration_ms") is not None:
            parts.append(f"{spec['duration_ms']}ms")
        if spec.get("error"):
            parts.append(f"error={spec['error']}")
        return truncate(" ".join(parts), 120)
    if kind in ("session.agent_state", "session.user_state"):
        return f"{spec.get('old_state', '?')} → {spec.get('new_state', '?')}"
    if kind == "session.error":
        return truncate(spec.get("message") or spec.get("error"), 120)
    if kind == "session.chat_history":
        return f"{len(spec.get('items') or [])} items"
    if kind == "session.usage":
        return f"{len(spec.get('model_usage') or [])} model usage entries"
    if kind == "silence.detected":
        return f"{spec.get('duration_ms', '?')}ms of silence"
    keys = [k for k in ("name", "identity", "topic", "status", "room", "node_id", "reason") if spec.get(k)]
    return truncate(", ".join(f"{k}={spec[k]}" for k in keys), 120)


__all__ = [
    "RENDERER",
    "make_console",
    "render_text",
    "fmt_bool",
    "fmt_tristate",
    "fmt_ms",
    "fmt_pct",
    "fmt_list",
    "truncate",
    "fmt_utc",
    "status_style",
    "render_scenarios",
    "render_runs",
    "render_plugins",
    "render_cues",
    "render_validate",
    "render_preflight",
    "render_execute",
    "render_execute_all",
    "render_compare",
    "render_status",
    "render_report",
    "render_log",
    "render_optimize",
]
