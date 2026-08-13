"""Example plugins — copy to `<repo>/.agent-sim/plugins/` and reference from scenario JSONL.

Kinds:
  - @verify_plugin       → Script.verify checks on events.jsonl
  - @register_before_run → after prepare, before SimLeg connects
  - @register_after_run  → after finalize, just before execute returns

Full API: docs/plugins.md
"""

from __future__ import annotations

from livekit_agent_simulator.plugins import (
    AfterRunContext,
    BeforeRunContext,
    VerifyContext,
    register_after_run,
    register_before_run,
    verify_plugin,
)


@verify_plugin("example_backchannel_continue")
def example_backchannel_continue(ctx: VerifyContext) -> dict:
    """Pass when agent spoke again after the scripted backchannel cue."""
    min_finals = int(ctx.options.get("min_agent_finals", 1))
    actual = ctx.finals_after_first_cue("agent")
    return {
        "pass": actual >= min_finals,
        "checks": [
            {
                "check": "agent_finals_after_cue",
                "pass": actual >= min_finals,
                "expected": min_finals,
                "actual": actual,
            }
        ],
        "detail": "example plugin — replace with your project logic",
    }


@verify_plugin("adaptive_false_interrupt_resume")
def adaptive_false_interrupt_resume(ctx: VerifyContext) -> dict:
    """Pass when the agent resumed speech after a noise-class (false-interrupt) cue.

    Portable: reads only VerifyContext + the run's own events — no consumer IDs.
    """
    noise_cue_ms = ctx.first_cue_ms()
    agent_finals = ctx.events_of_kind("transcript.agent.final")
    resumed = (
        any(int(e.get("ts_mono_ms") or 0) >= noise_cue_ms for e in agent_finals)
        if noise_cue_ms is not None
        else False
    )
    tool_errors = len(ctx.events_of_kind("tool.error"))
    return {
        "pass": resumed and tool_errors == 0,
        "checks": [
            {"check": "agent_resumed_after_false_interrupt", "pass": resumed},
            {"check": "no_tool_error", "pass": tool_errors == 0},
        ],
        "detail": "portable example — replace with your project logic",
    }


@register_before_run
def example_before_run(ctx: BeforeRunContext) -> None:
    """Optional: stamp meta / prepare external resources before connect."""
    ctx.meta.setdefault("example_plugin", True)


@register_after_run
def example_after_run(ctx: AfterRunContext) -> None:
    """Optional: side effects after the report is written (CI notify, archive, …)."""
    # Example only — replace or delete. Do not log secrets.
    _ = (ctx.run_id, ctx.status, ctx.report_dir)


def setup() -> None:
    """Optional: called when this module is loaded from `.agent-sim/plugins/`."""
    pass
