"""End-to-end run: preflight → SimLeg.connect → converse → report.

Phases: prepare → SimLeg (WebRTC | inbound_sip | outbound_human_pickup | outbound_sim_callee | agent_dials) →
SimBrain → converse → verify → judge → finalize.

End conditions (first one wins):
    - simulator persona says goodbye and emits [END_CALL]
    - scenario max_turns reached (after the agent replied in the final turn)
    - scenario timeout_s exceeded
    - agent participant disconnected / room closed
    - hold timeout: agent dead air >= Execute.spec.hold_music_timeout_s → sim hangs up (#29)
    - dead call: no agent activity for 3 × silence_threshold_ms (safety net)
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .audio.local_recorder import DEFAULT_FILENAME, LocalConversationRecorder
from .behavior_compile import silent_mode_enabled
from .config import SimConfig, config_snapshot
from .callers.base import CallerBridge
from .callers.factory import build_caller_bridge
from .callers.gemini import resolve_voice_gain
from .audio.degradation import resolve_audio_effects
from .livekit.adapter import AgentJoinTimeout, LiveKitAdapter
from .livekit.observer import Observer
from .livekit.sim_leg import SimLegContext, SimLegError, SimLegHandle, sim_leg_factory
from .logging.event_writer import EventWriter
from .logging.sqlite_store import RunStore
from .preflight import run_preflight
from .plugins.loader import ensure_plugins_loaded
from .plugins import registry as plugin_registry
from .plugins.api import AfterRunContext, BeforeRunContext
from .scenario import Scenario, SimulatorSpec, find_scenario, validate_telephony_for_mode
from .script import build_caller_behavior_summary, evaluate_script_log
from .script.verify import run_verify_plugins


_LEADING_SEQ = re.compile(r"^(\d+)-")


def _run_id_slug(value: str, *, max_len: int = 48, fallback: str = "") -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", (value or "").strip()).strip("-_.")
    slug = (slug[:max_len] if slug else fallback).lower()
    return slug


def next_run_seq(reports_dir: Path | None) -> int:
    """Next report sequence number (001, 002, …) from existing report folders."""
    if reports_dir is None or not Path(reports_dir).is_dir():
        return 1
    best = 0
    for p in Path(reports_dir).iterdir():
        if not p.is_dir():
            continue
        m = _LEADING_SEQ.match(p.name)
        if m:
            best = max(best, int(m.group(1)))
    return best + 1


def _run_id_stamp() -> str:
    """UTC timestamp + short random suffix so run_id stays unique vs SQLite history."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{secrets.token_hex(2)}"


def new_run_id(
    scenario_id: str,
    *,
    name: str | None = None,
    reports_dir: Path | None = None,
    seq: int | None = None,
) -> str:
    """Human-readable run id.

    Default: ``{NNN}-{scenario}-{YYYYMMDD}-{HHMMSS}-{xxxx}``
    With ``name``: ``{NNN}-{name}-{YYYYMMDD}-{HHMMSS}-{xxxx}``
    (scenario id stays in meta.json only).

    ``NNN`` is an auto-incrementing prefix from ``reports_dir``.
    Pass ``seq`` to pin a number (tests / retry loops).
    The timestamp+hex suffix avoids ``runs.run_id`` UNIQUE collisions when a
    report folder was deleted but the SQLite row remains.
    """
    scenario_slug = _run_id_slug(scenario_id, fallback="scenario")
    n = seq if seq is not None else next_run_seq(reports_dir)
    prefix = f"{n:03d}"
    stamp = _run_id_stamp()
    if name:
        name_slug = _run_id_slug(name, max_len=64)
        if name_slug:
            return f"{prefix}-{name_slug}-{stamp}"
    return f"{prefix}-{scenario_slug}-{stamp}"


def allocate_run_dir(
    reports_dir: Path,
    scenario_id: str,
    *,
    name: str | None = None,
) -> tuple[str, Path]:
    """Pick a free run_id and create its report folder (safe under parallel runs)."""
    reports_dir = Path(reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    seq = next_run_seq(reports_dir)
    for _ in range(10_000):
        run_id = new_run_id(scenario_id, name=name, seq=seq)
        report_dir = reports_dir / run_id
        try:
            report_dir.mkdir(parents=False)
            return run_id, report_dir
        except FileExistsError:
            seq += 1
    raise RuntimeError(f"Could not allocate a free report dir under {reports_dir}")



def _summarize_router(events: list[dict]) -> dict | None:
    """Fold `contract.router_decision` events into run-summary evidence.

    Returns None when the run never routed, so a non-router scenario gains no
    `router` key and its summary is unchanged.

    Attribution has to be readable in BOTH directions: a correct agent that
    draws a false off_script, and a hallucinating agent that draws matched,
    each silently corrupt a suite. So the counts are kept separate rather than
    collapsed, and the per-decision list keeps responseId + the agent-line
    hash so a human can audit any single row.
    """
    decisions = [
        (e.get("spec") or {})
        for e in events
        if e.get("kind") == "contract.router_decision"
    ]
    faults = [
        (e.get("spec") or {})
        for e in events
        if e.get("kind") == "contract.router_fault"
    ]
    unroutable = [
        (e.get("spec") or {})
        for e in events
        if e.get("kind") == "contract.router_unroutable"
    ]
    if not decisions and not faults and not unroutable:
        return None
    return {
        "matched": sum(1 for d in decisions if not d.get("off_script")),
        "off_script": sum(1 for d in decisions if d.get("off_script")),
        "faults": len(faults),
        "unroutable": len(unroutable),
        "decisions": [
            {
                "turn": d.get("turn"),
                "response_id": d.get("response_id"),
                "off_script": bool(d.get("off_script")),
                "backend": d.get("backend"),
                "latency_ms": d.get("latency_ms"),
                # Full-line hash: the text is truncated for readability, and
                # truncation must never be able to hide a divergence (D10).
                "agent_text_sha": d.get("agent_text_sha"),
            }
            for d in decisions
        ],
    }

def _collect_flow_events(
    events: list[dict[str, Any]],
    flow_topics: list[str],
) -> list[dict[str, Any]]:
    """Collect opaque flow-lifecycle payloads published on configured topics.

    The target decides which data topics carry flow/node-lifecycle events via
    ``observe.flow_topics``; core stays repo-agnostic and never interprets the
    payload keys. Payloads keep their ``source``/``spec.payload`` envelope so
    the judge digest and verify plugins can render them generically.
    """
    if not flow_topics:
        return []
    topics = set(flow_topics)
    out: list[dict[str, Any]] = []
    for e in events:
        if e.get("kind") != "data.message":
            continue
        if (e.get("source") or "") not in topics:
            continue
        payload = (e.get("spec") or {}).get("payload")
        if not isinstance(payload, dict) or not payload:
            continue
        out.append(e)
    return out


async def run_scenario(
    cfg: SimConfig,
    scenario_id: str,
    *,
    run_name: str | None = None,
    agent_name: str | None = None,
    caller_policy: Any = None,
    record_path: Any = None,
    replay_path: Any = None,
    no_router: bool = False,
) -> dict[str, Any]:
    """Run one scenario by id from `.agent-sim/scenarios/`.

    ``caller_policy`` (optional) overrides the persona-prompt composer — the
    runtime seam for a saved ``lks optimize`` artifact.

    ``record_path``/``replay_path`` thread the caller's record/replay flow
    (see caller_contract.record_replay): record writes a versioned RunRecord
    of every generate+validate attempt; replay serves it back with no AI
    calls. Mutually exclusive.
    """
    preflight, _ = await run_preflight(cfg.project_root, connectivity=True)
    if not preflight.ok:
        failed = [c for c in preflight.checks if c["status"] == "fail"]
        raise RuntimeError("Preflight failed: " + "; ".join(f"{c['name']}: {c['detail']}" for c in failed))
    scenario = find_scenario(cfg.scenarios_dir, scenario_id)
    if caller_policy is not None:
        scenario.caller_policy = caller_policy
    return await run_scenario_instance(
        cfg, scenario, run_name=run_name, agent_name=agent_name,
        record_path=record_path, replay_path=replay_path, no_router=no_router,
    )


def _with_report_pointer(error: str | None, report_dir: Path) -> str | None:
    """Append the report directory to a failure message.

    Measured on the target repo (2026-09-30): a run killed by a LiveKit
    read timeout reported `TimeoutError: The read operation timed out (in
    webrtc_sim)` and nothing else. Its `events.jsonl` held 40 events including
    a `contract.turn_published` and a `judge.verdict` — the run was not
    evidence-free, it was just not reachable from the one line the CLI prints.

    A peer session reported that run as having "produced no evidence at all",
    which is a reasonable reading of the output and completely wrong about the
    run. The cost was a round trip to find 40 events that were already written.

    So the message points at its own evidence. Cheap, and it only ever adds a
    path that already exists.
    """
    if error is None:
        return None
    return f"{error} (report: {report_dir})"


async def _cleanup_rooms(
    adapter: Any,
    leg_handle: Any,
    meta: dict[str, Any],
    writer: Any,
) -> None:
    """Disconnect the sim's rooms and delete them, recording every failure.

    Extracted from `run_scenario_instance` so it is testable directly — the
    first version of its test inspected the caller's source with string
    searches, which proves nothing about behaviour.

    Every step is guarded and every failure is RECORDED rather than
    swallowed. `delete_room` was the one unguarded step: a failure propagated
    out and skipped everything after it. A bare `except: pass` would have been
    worse in the other direction — a room that survives still holds a LiveKit
    dispatch job, and a worker still holding one may refuse to initialise a
    runner for the next dispatch. That is the shape of the `AgentJoinTimeout`
    reports on the target repo, so this failure has to be loud.

    NOT a candidate for those reports themselves: `rooms_to_delete` is
    correctly populated for every leg (`[dispatch.room_name]` for webrtc_sim),
    and the reported runs end with the sim leg closing cleanly. Recorded here
    so the two are not conflated.
    """
    rooms: list[str] = []
    if leg_handle is not None:
        rooms = list(leg_handle.rooms_to_delete)
        try:
            await leg_handle.disconnect_rooms()
        except Exception as exc:  # noqa: BLE001 — cleanup must continue
            writer.emit(
                "sim.error",
                spec={
                    "where": "disconnect_rooms",
                    "error": f"{type(exc).__name__}: {exc}",
                },
                source="sim",
                include_dialogue=False,
            )
    elif meta.get("room_name"):
        rooms = [str(meta["room_name"])]
    for rn in dict.fromkeys(rooms):
        try:
            await adapter.delete_room(rn)
        except Exception as exc:  # noqa: BLE001 — one room must not block the rest
            writer.emit(
                "sim.error",
                spec={
                    "where": "delete_room",
                    "room": rn,
                    "error": f"{type(exc).__name__}: {exc}",
                    "note": (
                        "the room may still hold a dispatch job; a worker still "
                        "holding one may not initialise a runner for the next "
                        "dispatch"
                    ),
                },
                source="sim",
                include_dialogue=False,
            )


def diagnose_failure(
    status: str,
    events: list[dict[str, Any]],
    meta: dict[str, Any] | None = None,
) -> str | None:
    """Why the run failed, or None if there is nothing to say.

    `cli_render` renders an `error` column for a failed run, but nothing
    populated it: the run result had no such key, so a hard failure printed
    `status: failed` / `hard_reasons: ["status:failed"]` and the real cause sat
    one layer down in ``reports/<run-id>/events.jsonl``. Cost, once:
    ``CALLER_BEHAVIOR_VIOLATION: LOW_CONFIDENCE`` took a bisect and a ~70s
    paid call to find, from a report that said only ``status:failed``.

    Derived from the reporter events rather than a threaded variable, so every
    failure path that reports itself is covered without new plumbing. Matches on
    "any spec carrying an error", NOT on event kind: the paths use different
    kinds (``run.error`` from the top-level handler, ``sim.error`` from audio
    finalize, ``sim.leg_error`` / ``dispatch.agent_timeout`` from connect), and
    enumerating them here would be a list to forget to update.

    Returns None on success, and None on a failure with nothing diagnosable —
    the CLI renders a dash for that rather than inventing a reason.
    """
    if status != "failed":
        return None
    for ev in reversed(events):
        # `EventWriter.events` is a list of plain dicts with a "spec" key, not
        # objects with attributes.
        spec = ev.get("spec") if isinstance(ev, dict) else None
        if not isinstance(spec, dict):
            continue
        raw = spec.get("error")
        if not raw:
            continue
        where = spec.get("where") or spec.get("mode")
        return f"{raw} (in {where})" if where else str(raw)
    if meta and meta.get("assert_failed"):
        return "assert verify failed"
    return None


async def run_scenario_instance(
    cfg: SimConfig,
    scenario: Scenario,
    *,
    run_name: str | None = None,
    agent_name: str | None = None,
    record_path: Any = None,
    replay_path: Any = None,
    no_router: bool = False,
) -> dict[str, Any]:
    """Run a parsed Scenario (file or in-memory). Returns {run_id, status, report_dir, summary}.

    ``no_router`` is the operator abort path: it forces the caller_steps path
    even when the scenario authors ``responses:``. It is a RUN-TIME flag, not a
    config key - a key left set to false is itself a second source of truth,
    and a flag in a shell history is not. It bypasses the ROUTER, not the
    wording layer: ``text_planner.enabled: false`` is not equivalent, because
    the router would still pick responseIds, still record verdicts, and still
    bypass the validator, so a misrouting run stays misrouting.

    ``agent_name`` overrides ``cfg.livekit.agent_name`` for this run only —
    dispatch targets the named worker without editing ``.agent-sim/config.yaml``
    (enables parallel worktree workflows where each worktree registers its own
    agent under a distinct name).

    Phases (in order):
      1. prepare  — plugins, report dir, event writer
      2-3. SimLeg  — factory(mode).connect → rooms + identities
      4. brain    — GeminiCallerBridge + optional script runner
      5. converse — turns until end condition
      6. verify   — script/assert hard checks + behavior_summary
      7. judge    — optional soft LLM verdict
      8. finalize — summary.json / sqlite / multi-room cleanup
    """
    if agent_name:
        cfg = dataclasses.replace(cfg, livekit=dataclasses.replace(cfg.livekit, agent_name=agent_name))

    # ── Phase 1: prepare ────────────────────────────────────────────────
    plugin_load = ensure_plugins_loaded(cfg.project_root, scenario.plugin_modules)
    run = scenario.run_spec
    dispatch_metadata = scenario.dispatch_metadata(cfg.livekit.dispatch_metadata)
    run_id, report_dir = allocate_run_dir(cfg.reports_dir, scenario.id, name=run_name)
    writer = EventWriter(
        run_id,
        report_dir,
        timezone_name=cfg.observe.timezone,
        turn_taking_warn_ms=cfg.observe.turn_taking_warn_ms,
    )
    store = RunStore(cfg.sqlite_path)

    started_utc = datetime.now(timezone.utc).isoformat()
    meta: dict[str, Any] = {
        "run_id": run_id,
        "run_name": run_name,
        "scenario_id": scenario.id,
        "scenario_file": str(scenario.path),
        "run_spec": {
            "max_turns": run.max_turns,
            "timeout_s": run.timeout_s,
            "first_speaker": run.first_speaker,
        },
        "dispatch_metadata_set": bool(dispatch_metadata),
        "agent_name": cfg.livekit.agent_name,
        "started_utc": started_utc,
        "config_snapshot": config_snapshot(cfg),
        "plugins_loaded": plugin_load,
    }

    # ── Phase 1b: before_run hooks (plugins can enrich meta, set up external resources) ──
    plugin_registry.run_before_run_hooks(
        BeforeRunContext(
            scenario=scenario,
            project_root=Path(cfg.project_root),
            run_id=run_id,
            run_name=run_name,
            meta=meta,
            dispatch_metadata=dispatch_metadata,
            options=dict(scenario.script_verify.plugin_options) if scenario.script_verify else {},
        ),
    )

    status = "failed"
    verdict: dict[str, Any] | None = None
    summary: dict[str, Any] = {}
    recorder: LocalConversationRecorder | None = None
    observer: Observer | None = None
    session_snapshot_attempted = False
    leg_handle: SimLegHandle | None = None
    # Pre-declared: an exception raised anywhere in the try block below (SimLeg
    # connect failure, ContractDriverFailure, etc.) must never leave this
    # UnboundLocalError'd — the post-run summary code reads end_reason
    # unconditionally regardless of whether the run reached the point where
    # it would normally be assigned.
    end_reason: str | None = None
    caller_mode = scenario.effective_caller_mode()
    meta["caller_mode"] = caller_mode

    async with LiveKitAdapter(cfg) as adapter:
        writer.emit(
            "run.started",
            spec={
                "scenario_id": scenario.id,
                "caller_mode": caller_mode,
                "config_snapshot": config_snapshot(cfg),
            },
            include_dialogue=False,
        )
        try:
            validate_telephony_for_mode(scenario, cfg)

            # ── Phase 2–3: SimLeg.connect (Strategy) ─────────────────────
            leg = sim_leg_factory(caller_mode)
            try:
                leg_handle = await leg.connect(
                    SimLegContext(
                        adapter=adapter,
                        cfg=cfg,
                        scenario=scenario,
                        writer=writer,
                        run_id=run_id,
                        dispatch_metadata=dispatch_metadata,
                        first_speaker=run.first_speaker,
                    )
                )
            except (AgentJoinTimeout, SimLegError) as e:
                writer.emit(
                    "dispatch.agent_timeout" if isinstance(e, AgentJoinTimeout) else "sim.leg_error",
                    spec={"error": str(e), "mode": caller_mode},
                    include_dialogue=False,
                )
                raise

            meta["room_name"] = leg_handle.agent_room_name
            meta["sim_room_name"] = leg_handle.sim_room_name
            meta["agent_identity"] = leg_handle.agent_identity
            meta["sim_identity"] = leg_handle.sim_identity
            if leg_handle.meta:
                meta["leg"] = dict(leg_handle.meta)
                if "dial_ms" in leg_handle.meta:
                    meta["dial_ms"] = leg_handle.meta["dial_ms"]

            await store.create_run(
                run_id,
                scenario.id,
                leg_handle.agent_room_name,
                cfg.livekit.agent_name,
                started_utc,
                str(report_dir),
            )

            if cfg.observe.audio_recording_enabled:
                recorder = LocalConversationRecorder()
                # Pin audio t=0 as early as possible so pre-Gemini agent media
                # (outbound greeting while sim-leg wait used to block) is on the timeline.
                recorder.mark_start()

            # Observer on agent-room: transcripts + agent WAV R-channel (works for SIP 2-room).
            # For outbound_sim_callee, agent_room was joined *before* dial inside OutboundSimCalleeSimLeg.
            observer = Observer(
                leg_handle.agent_room,
                writer,
                cfg.observe,
                leg_handle.agent_identity,
                leg_handle.sim_identity,
                first_speaker=run.first_speaker,
                recorder=recorder,
            )
            observer.attach()

            # Gemini brain always on sim_room (WebRTC: same as agent_room).
            # Recorder still gets L=sim via mixer; R=agent via Observer (not only Gemini listen path).
            #
            # The caller brain (persona system instruction + mid-call cues) is
            # NOT built here. It was pure dead computation on this path: the
            # only consumers are inside the bridge's Realtime session loop
            # (``GeminiCallerBridge.run`` -> ``_emit_bootstrap_cues`` /
            # ``inject_reground``), and the contract path never opens a
            # session — ``run()`` has no call sites (the bridge is used only
            # for publish_mic / publish_validated_pcm / watch_agent_tracks /
            # stop). The contract AI adapter builds its prompt from the
            # BehaviorContract instead (see caller_contract.text_backends).
            #
            # Consequence, stated plainly: a saved ``lks optimize`` artifact
            # (``Scenario.caller_policy``) has NO runtime effect on the
            # caller-contract path, because the persona prompt it optimizes is
            # not part of it. Wiring the policy into the contract adapter's
            # context is a deliberate design change, not a cleanup — do not
            # re-add the composition here expecting it to take effect.
            _silent = silent_mode_enabled(scenario.persona)
            bridge = build_caller_bridge(
                cfg=cfg,
                room=leg_handle.sim_room,
                observer=observer,
                writer=writer,
                persona_system_prompt="",
                first_speaker=run.first_speaker,
                recorder=recorder,
                voice_gain=resolve_voice_gain(scenario.persona),
                silent_mode=_silent,
                audio_effects=resolve_audio_effects(scenario.persona),
            )
            if _silent:
                writer.emit(
                    "sim.silent_mode",
                    spec={
                        "enabled": True,
                        "note": "Caller stays mute: no freestyle, no nudge, no auto barge/noise",
                    },
                    source="sim",
                    include_dialogue=False,
                )
                meta["silent_mode"] = True
            # Listen/record feed derived from SimLegHandle — no mode ifs.
            if leg_handle.gemini_listen_agent_room:
                bridge.watch_agent_tracks_on_room(
                    leg_handle.agent_room, leg_handle.agent_identity
                )
            elif leg_handle.gemini_listen_sip:
                bridge.watch_sip_audio_tracks()
            elif leg_handle.gemini_listen_identity:
                bridge.watch_agent_tracks(leg_handle.gemini_listen_identity)
            else:
                bridge.watch_agent_tracks(leg_handle.agent_identity)

            # ── operator abort path (--no-router) ──────────────────────────
            # Placed here, immediately before the contract path, so the router
            # never receives a catalog: clearing scenario.responses means the
            # driver's `response_catalog is not None` gate is false, and the run
            # takes the caller_steps path. Emitting alone would NOT do that - the
            # router would still route, still record verdicts, still bypass the
            # validator.
            if no_router and getattr(scenario, "responses", None) is not None:
                # Recorded, never silent: a run that did NOT exercise the router
                # must never be mistaken for one that did and passed.
                writer.emit(
                    "contract.router_aborted",
                    spec={
                        "reason": "--no-router",
                        "responses": sorted(scenario.responses.responses),
                    },
                    include_dialogue=False,
                )
                scenario.responses = None

            # ── caller_contract single path (the ONLY caller path) ──
            # Every scenario carries caller_actions (migration gate
            # test_every_template_has_caller_steps enforces this): the
            # legacy persona free-generation / ScriptRunner /
            # interrupt-rate / nudge / bridge.run() Realtime session path
            # is deleted, not branched. Only bridge.publish_mic()
            # (mixer/track plumbing) is shared.
            if not scenario.caller_actions:
                raise RuntimeError(
                    f"scenario {scenario.id!r} has no caller_steps: "
                    "the legacy caller path is removed; add caller_steps "
                    "to the scenario (see templates/scenario-scaffold.yaml)"
                )
            if record_path is not None and replay_path is not None:
                raise ValueError(
                    "record_path and replay_path are mutually exclusive"
                )
            from .caller_contract.live_wiring import run_contract_driver_path

            await bridge.publish_mic()
            try:
                end_reason = await run_contract_driver_path(
                    scenario,
                    run,
                    observer,
                    bridge,
                    writer,
                    cfg,
                    record_path=record_path,
                    replay_path=replay_path,
                )
            finally:
                bridge.stop()

            # Which engine actually produced the caller's voice. The
            # caller_contract path is the ONLY caller path: `bridge.run()`
            # (the cloud Realtime session selected by `simulator.provider`)
            # is deleted, and caller speech comes from the local TTS branch —
            # sherpa when the `tts-sherpa` extra is installed, otherwise the OS
            # TTS fallback. `simulator.provider` still selects the bridge object
            # and the semantic LLM behind `do:` steps, but it does NOT choose
            # the caller's speech engine.
            #
            # Without this the run looks like it honoured the configured
            # provider: the config snapshot reports `provider`/`voice_model`/
            # `active_profile`, `preflight` checks `simulator.api_key`, and
            # `sim.mic_published` reports `provider` — while every utterance was
            # spoken by a different engine. Per-utterance `contract.published`
            # carries `tts`, but a run that is only skimmed never surfaces it.
            try:
                from .caller_contract.live_wiring import last_tts_branch

                caller_tts = last_tts_branch()
            except Exception:  # noqa: BLE001 — diagnostics must never fail a run
                caller_tts = None
            writer.emit(
                "sim.caller_tts",
                spec={
                    "branch": caller_tts,
                    "configured_provider": cfg.simulator.provider,
                    "provider_selects_speech_engine": False,
                    "note": (
                        "caller speech is produced by the local TTS branch; "
                        "simulator.provider selects the caller bridge and the "
                        "semantic LLM, not the speech engine. Install the "
                        "'tts-sherpa' extra for a deterministic voice."
                    ),
                },
                include_dialogue=False,
            )

            writer.emit("run.end_condition", spec={"reason": end_reason}, include_dialogue=False)
            session_snapshot_attempted = True
            await observer.finalize_session_snapshot()
            await observer.detach()
            if leg_handle is not None:
                await leg_handle.disconnect_rooms()
            status = "done"
        except asyncio.CancelledError as e:
            # CancelledError is a BaseException, NOT an Exception, since
            # Python 3.8 — so `except Exception` below does not catch it, and
            # anything that cancels during connect or teardown walks straight
            # past every handler in this function.
            #
            # Measured on the target repo, 2026-09-30: runs 118 and 119 wrote
            # exactly three events — run.started, dispatch.created,
            # dispatch.agent_joined — and stopped. No sim.connected, no
            # contract.*, and no run.error at all. The run did not fail; it
            # ceased to exist, which leaves nothing to investigate.
            #
            # Recorded and re-raised. Swallowing a cancellation would leave the
            # task silently unfinished, which is worse than a loud failure.
            writer.emit(
                "run.error",
                spec={
                    "error": f"{type(e).__name__}: {e}" if str(e) else type(e).__name__,
                    "mode": caller_mode,
                    "cancelled": True,
                },
                include_dialogue=False,
            )
            raise
        except Exception as e:
            writer.emit(
                "run.error",
                spec={"error": f"{type(e).__name__}: {e}", "mode": caller_mode},
                include_dialogue=False,
            )
            status = "failed"
        finally:
            if observer is not None:
                if (
                    not session_snapshot_attempted
                    and not observer.agent_disconnected.is_set()
                ):
                    session_snapshot_attempted = True
                    await observer.finalize_session_snapshot()
                await observer.detach()
            if recorder is not None:
                try:
                    audio_path = report_dir / DEFAULT_FILENAME
                    result = recorder.finalize(audio_path)
                    if result is not None:
                        t0_mono_ms = 0
                        if recorder.started_mono is not None:
                            t0_mono_ms = max(
                                0, int((recorder.started_mono - writer.t0_mono) * 1000)
                            )
                        audio_meta = {
                            "path": str(result.path),
                            "sample_rate": result.sample_rate,
                            "duration_ms": result.duration_ms,
                            "channels": {"left": "sim", "right": "agent"},
                            "sim_samples": result.sim_samples,
                            "agent_samples": result.agent_samples,
                            # Align event ts_mono_ms → audio seconds: audio_ms = ts_mono_ms - t0_mono_ms
                            "t0_mono_ms": t0_mono_ms,
                        }
                        meta["audio"] = audio_meta
                        writer.emit(
                            "sim.audio_recorded",
                            spec=audio_meta,
                            source="sim",
                            include_dialogue=False,
                        )
                    else:
                        writer.emit(
                            "sim.audio_recorded",
                            spec={"path": None, "note": "no audio frames captured"},
                            source="sim",
                            include_dialogue=False,
                        )
                except Exception as e:
                    writer.emit(
                        "sim.error",
                        spec={
                            "where": "audio_finalize",
                            "error": f"{type(e).__name__}: {e}",
                        },
                        source="sim",
                        include_dialogue=False,
                    )
            # Cleanup rooms from SimLegHandle (WebRTC: one room; SIP: agent + sim).
            await _cleanup_rooms(adapter, leg_handle, meta, writer)

    # ── Phase: post-run hard verify + report digests ─────────────────────
    summary_extra: dict[str, Any] = {}

    # script.verify reads the LEGACY scripted-caller event vocabulary
    # (sim.script.cue / dtmf / wait / hang_up) — the only kinds
    # evaluate_script_log matches step_ids against. The contract path emits
    # contract.* instead, so on a contract scenario every step would report
    # "script step not fired" and script_verify would land in the suite's
    # HARD gate (suite.py) — failing CI for a caller that actually ran fine.
    # There is nothing to verify here: the mechanism checks an execution
    # engine (ScriptRunner) that is no longer instantiated.
    #
    # Skipped EXPLICITLY (not omitted) so a report reader can tell "not
    # applicable to this caller path" from "verify did not run".
    contract_caller_path = bool(scenario.caller_actions)
    has_script_verify = (
        not contract_caller_path
        and scenario.script_verify is not None
        and (scenario.script_steps or bool(scenario.script_verify.plugins))
    )
    if status == "done" and contract_caller_path and (
        scenario.script_steps or (scenario.script_verify and scenario.script_verify.plugins)
    ):
        # Step matching genuinely does not apply here (it keys off step_ids
        # matched against sim.script.cue, which the contract path never emits).
        # Verify PLUGINS however are a different matter: they consume the run's
        # events, not step ids, so skipping them for that reason skipped
        # something that DID apply - every plugin assertion was a silent no-op
        # reporting ok:true. Run them and keep only the step half skipped.
        _plugin_checks: list = []
        if scenario.script_verify is not None:
            _plugin_checks = run_verify_plugins(
                scenario.script_verify,
                scenario=scenario,
                project_root=project_root,
                events=writer.events,
            )
        script_verify = {
            "skipped": True,
            "reason": (
                "caller_steps drives the contract path; step matching checks the "
                "legacy sim.script.cue vocabulary that this path never emits"
            ),
            "steps_not_applicable": [s.id for s in scenario.script_steps],
            "plugin_checks": _plugin_checks,
        }
        writer.emit("script.verify", spec=script_verify, include_dialogue=False)
        summary_extra["script_verify"] = script_verify
    elif status == "done" and has_script_verify:
        script_verify = evaluate_script_log(
            writer.events,
            scenario.script_steps,
            scenario.script_verify,
            scenario=scenario,
            project_root=cfg.project_root,
        )
        writer.emit("script.verify", spec=script_verify, include_dialogue=False)
        summary_extra["script_verify"] = script_verify
        # A failing verify plugin must fail the run, the same way a failing
        # assert outcome does. Without this the contract path reported ok:true
        # on an impossible plugin assertion - a silent false-pass.
        if status == "done" and any(
            isinstance(c, dict) and c.get("pass") is False
            for c in _plugin_checks
        ):
            status = "failed"
            meta["verify_plugin_failed"] = [
                c.get("check") for c in _plugin_checks
                if isinstance(c, dict) and c.get("pass") is False
            ]

    if status == "done" and scenario.asserts is not None and not scenario.asserts.empty:
        from .asserts import evaluate_asserts

        assert_result = evaluate_asserts(writer.events, scenario.asserts)
        writer.emit("assert.verify", spec=assert_result, include_dialogue=False)
        summary_extra["assert_verify"] = assert_result
        if not assert_result.get("pass"):
            # Hard asserts fail the run even if the LLM judge would pass.
            if status == "done":
                status = "failed"
            meta["assert_failed"] = True

    # Caller behavior digest for reports / web (barges, silences, recovery latency).
    if status in ("done", "failed"):
        behavior_summary = build_caller_behavior_summary(writer.events)
        # Enrich recovery latency from assert recovery outcomes when present.
        assert_v = summary_extra.get("assert_verify")
        if isinstance(assert_v, dict):
            for chk in assert_v.get("checks") or []:
                if (
                    isinstance(chk, dict)
                    and chk.get("type") == "recovery"
                    and chk.get("recovery_ms") is not None
                ):
                    behavior_summary["recovery_ms"] = chk.get("recovery_ms")
                    behavior_summary["recovery_assert_pass"] = bool(chk.get("pass"))
                    break
        summary_extra["caller"] = {"behavior_summary": behavior_summary}
        # Caller-contract evidence (§11): behavior sequence + per-turn
        # generated text + validation, aggregated from the contract.*
        # event trail (no new collection — the driver already emits all of
        # it). Additive under summary["caller_contract"]; the transcript /
        # timing / asserts pipeline above is untouched.
        try:
            from .caller_contract.contract_summary import (
                build_caller_contract_summary,
            )

            summary_extra["caller_contract"] = build_caller_contract_summary(
                events=writer.events,
                behaviors=[
                    {
                        "behavior": (a.contract.behavior if a.contract else a.kind),
                        "target": (
                            a.contract.target if a.contract is not None else None
                        ),
                    }
                    for a in (scenario.caller_actions or [])
                    if a.kind == "do" and a.contract is not None
                ],
            )
            # Router evidence, in the SAME try/except as its siblings so a
            # router failure can never break summary generation. Present only
            # for a routed run: a non-router run has no router key at all
            # (D11), so its summary is byte-identical to before.
            _router_summary = _summarize_router(writer.events)
            if _router_summary is not None:
                summary_extra["caller_contract"]["router"] = _router_summary
        except Exception:  # noqa: BLE001 — summary must never break a run
            pass

    # ── Phase: soft LLM judge (does not flip hard gate by itself) ────────
    if status in ("done", "failed") and cfg.judge is not None and scenario.pass_criteria:
        try:
            tool_events = [e for e in writer.events if e["kind"].startswith("tool.")]
            # Flow-lifecycle events published on the target's configured flow
            # data topics (observe.flow_topics) prove node hold/advance — the
            # soft judge surfaces them as flow evidence.
            flow_events = _collect_flow_events(writer.events, cfg.observe.flow_topics)
            # Include llm_bool outcome prompts as extra criteria when present.
            criteria = list(scenario.pass_criteria)
            if scenario.asserts:
                for oc in scenario.asserts.outcomes:
                    if oc.type == "llm_bool" and oc.prompt:
                        criteria.append(f"[outcome:{oc.id}] {oc.prompt}")
            from .evals.prompt import build_router_digest
            from .evals.runner import judge_run, judge_run_multi

            # The judge needs the router's routing evidence: without it, a
            # routed run cannot be distinguished from "the line was authored
            # and on-script" vs "the system entry fired because nothing in the
            # catalog matched" — which is the distinction the response catalog
            # exists to record. Framed as EVIDENCE, not a verdict: an
            # off-script turn can still sound natural and the call still
            # recover, so a judge treating the count as authoritative would
            # invent the exact UX complaint `off_script` exists to prevent.
            router_digest = build_router_digest(_router_summary)

            if getattr(scenario, "pass_judges", None):
                verdict = await judge_run_multi(
                    cfg.judge,
                    cfg.simulator.api_key,
                    scenario.pass_judges,
                    getattr(scenario, "pass_criteria_mode", None) or "all",
                    writer.turn_metrics(),
                    tool_events,
                    flow_events,
                    router_digest=router_digest,
                )
            else:
                verdict = await judge_run(
                    cfg.judge,
                    cfg.simulator.api_key,
                    criteria,
                    writer.turn_metrics(),
                    tool_events,
                    flow_events,
                    # Hand the judge the machine-checked contract so it does not
                    # re-litigate behavior the run already proved correct (it was
                    # reading deliberate test markers as leaked internals).
                    summary_extra.get("assert_verify"),
                    router_digest=router_digest,
                )
        except Exception as e:
            verdict = {
                "verdict": "error",
                "notes": f"Judge failed (soft): {type(e).__name__}: {e}",
            }
        writer.emit("judge.verdict", spec=verdict or {}, include_dialogue=False)

    # ── Post-run: goals_met (hard fail only on explicit LLM fail; soft-skip if judge unavailable) ─
    if status in ("done", "failed") and scenario.asserts and cfg.judge is not None:
        from .evals.runner import judge_goals

        for oc in scenario.asserts.outcomes or []:
            if oc.type != "goals_met":
                continue
            goal_list = list(oc.goals) if oc.goals else [
                g for g in (scenario.persona.get("goals") or []) if isinstance(g, str)
            ]
            if not goal_list:
                continue
            try:
                goals_result = await judge_goals(
                    cfg.judge, cfg.simulator.api_key,
                    goal_list, oc.min_goals,
                    writer.turn_metrics(),
                )
                gv = str((goals_result or {}).get("verdict") or "fail").lower()
                notes = str((goals_result or {}).get("notes") or "")
                # Misconfig / transport / skip → do not flip hard run status
                if gv in ("skipped", "error"):
                    writer.emit(
                        "assert.goals_met",
                        spec={
                            "outcome_id": oc.id,
                            "min_goals": oc.min_goals,
                            "goals": goal_list,
                            "verdict": gv,
                            "pass": True,
                            "skipped": True,
                            "notes": notes or "goals_met soft-skipped (judge unavailable).",
                        },
                        include_dialogue=False,
                    )
                    continue
                try:
                    gs = int((goals_result or {}).get("score", 0))
                except (TypeError, ValueError):
                    gs = 0
                goals_pass = gv == "pass" and gs >= 50
                writer.emit(
                    "assert.goals_met",
                    spec={
                        "outcome_id": oc.id,
                        "min_goals": oc.min_goals,
                        "goals": goal_list,
                        "verdict": gv,
                        "score": gs,
                        "pass": goals_pass,
                        "notes": notes,
                    },
                    include_dialogue=False,
                )
                if not goals_pass:
                    if status == "done":
                        status = "failed"
                    meta.setdefault("goals_failed", []).append(oc.id)
            except Exception as e:
                writer.emit(
                    "assert.goals_met",
                    spec={
                        "outcome_id": oc.id,
                        "error": f"{type(e).__name__}: {e}",
                        "pass": True,
                        "skipped": True,
                        "notes": "goals_met soft-skipped after judge exception.",
                    },
                    include_dialogue=False,
                )

    summary = writer.finalize(status, meta=meta, verdict=verdict)
    summary.setdefault("caller_mode", caller_mode)
    # Record why the call ended so run-level retries can distinguish a Gemini
    # Live transport drop (`gemini_socket_drop`) from a real hang-up — the
    # former is retryable flakiness, the latter is a genuine call outcome.
    if end_reason:
        summary["end_reason"] = end_reason
    if meta.get("dial_ms") is not None:
        summary.setdefault("dial_ms", meta.get("dial_ms"))
    if summary_extra:
        summary.update(summary_extra)
        (report_dir / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    else:
        # Persist mode/dial fields even without assert/script extras.
        (report_dir / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    ended_utc = datetime.now(timezone.utc).isoformat()
    # Everything from here to the return is POST-RUN bookkeeping: sqlite
    # writes and the after_run hooks. None of it produces the evidence - the
    # transcript, the events and summary.json are already on disk.
    #
    # So a failure here must NOT escape. It did, once: a read timeout on the
    # sim leg surfaced during teardown/storage, ops.execute_scenario caught
    # it, and the run came back as {"run_id": null, "status": "failed"} -
    # discarding the identifiers and orphaning a paid run's whole transcript,
    # with nothing pointing at it. Run 033 (gpt-live-instrumentation-repro).
    #
    # Persist the fault and keep going, so the caller always gets a run_id and
    # a report_dir it can open.
    def _record_post_run_fault(stage: str, exc: BaseException) -> None:
        writer.emit(
            "run.post_run_fault",
            spec={
                "stage": stage,
                "error": f"{type(exc).__name__}: {exc}",
                "evidence_persisted": True,
            },
            include_dialogue=False,
        )

    try:
        await store.insert_events(run_id, writer.events)
        await store.insert_turns(run_id, writer.turn_metrics())
        await store.finish_run(run_id, status, summary, ended_utc)
    except Exception as exc:  # noqa: BLE001 - sqlite is a cache, not the record
        _record_post_run_fault("store", exc)

    # ── Phase: after_run hooks ──────────────────────────────────────────
    try:
        plugin_registry.run_after_run_hooks(
            AfterRunContext(
                scenario=scenario,
                project_root=Path(cfg.project_root),
                run_id=run_id,
                run_name=run_name,
                report_dir=report_dir,
                status=status,
                summary=summary,
                events=list(writer.events),
                verdict=verdict,
                options=dict(scenario.script_verify.plugin_options) if scenario.script_verify else {},
            ),
        )
    except Exception as exc:  # noqa: BLE001 - a user plugin must not orphan a run
        _record_post_run_fault("after_run_hooks", exc)

    return {
        "run_id": run_id,
        "status": status,
        "report_dir": str(report_dir),
        "summary": summary,
        **(
            {"error": _with_report_pointer(diagnose_failure(status, writer.events, meta), report_dir)}
            if status == "failed"
            else {}
        ),
    }
