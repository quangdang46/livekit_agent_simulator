"""Measure how often the router gives a different answer to the SAME input.

This is a MEASUREMENT job, not a test, and it is not a CI gate. It exists
because the question it answers has no other honest answer, and every number
tried so far was contaminated.

    55%  — grouped across scenarios, so a correct answer for a different
            catalog was counted as a flip
    23%  — grouped per-run, so it measured within-run instability only
    7/10 — a ratio whose denominator was itself already wrong

All three were measured on a DEPLETED catalog. `offerable_ids()` drops a
non-reusable entry after one use, so when the agent asked a question twice the
correct answer was not in the option set, the router picked something else
CORRECTLY, and that was recorded as a routing failure. A flip rate computed
over that is not a flip rate.

So the input is pinned. For each fixed `agent_text`, the router is called N
times with a FRESHLY BUILT catalog and the distinct answers counted. Nothing
else varies: no scenario, no LiveKit room, no agent, no paid voice call.

Why a fresh catalog every iteration is not optional
--------------------------------------------------
`ResponseCatalog` is stateful — it tracks `_served`, and `serve()` spends a
non-reusable entry for the rest of the run. Reuse one catalog across the N
iterations and iteration 1 has 6 options, iteration 2 has 5, and by iteration 3
the interesting entries are gone. The result is a rising "flip rate" that is
purely your own harness eating its own evidence. This is the same class of
error as the ones above, one level down, and it would have looked like a real
finding.

Usage
-----
    # once, to record the inputs (or hand-write the JSON — see the schema below)
    uv run python scripts/router_flip_probe.py capture \\
        --root C:/path/to/voice-ai-agent --scenario gpt-live-happy-path

    # N calls per input, no scenario executed
    uv run python scripts/router_flip_probe.py measure \\
        --inputs router-probe-inputs.json --root C:/path/to/voice-ai-agent --n 5

`capture` pulls `agent_text` out of a scenario's existing report events, so the
inputs are transcripts the agent ACTUALLY produced rather than sentences
someone wrote to be easy.

Input JSON schema
-----------------
    {
      "inputs": [
        {"scenario": "gpt-live-happy-path",
         "agent_text": "could I please get your company name",
         "agent_text_sha": "…"}          # optional, verified if present
      ]
    }

Catalogs are NOT in this file on purpose. They are re-read from the scenario
YAML at measure time, because a catalog exported alongside its measurements is
a snapshot that can drift from the thing the measurements were taken against —
and a stale catalog here reproduces exactly the bug this script exists to avoid.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

# Allow `python scripts/...` without an editable install.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from livekit_agent_simulator.caller_contract.responses import ResponseCatalog  # noqa: E402
from livekit_agent_simulator.scenario_yaml import load_scenario_yaml  # noqa: E402


def _sha(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------- capture


def _scenario_path(root: Path, scenario_id: str) -> Path:
    base = root / ".agent-sim" / "scenarios"
    matches = sorted(base.rglob(f"{scenario_id}.yaml"))
    if not matches:
        raise SystemExit(
            f"no scenario file for {scenario_id!r} under {base}. "
            "Pass the id, not the filename stem if they differ."
        )
    return matches[0]


def _catalog_from_scenario(root: Path, scenario_id: str) -> dict[str, Any]:
    """Re-read the authored `responses:` block from the scenario YAML.

    Returns the RAW dict, rebuilt from the parsed specs, so the probe can
    construct a FRESH `ResponseCatalog` for every router call. `ResponseCatalog`
    has no `export()`, and reusing one parsed instance across the N iterations
    spends non-reusable entries as it goes — see the module docstring.
    """
    path = _scenario_path(root, scenario_id)
    scenario = load_scenario_yaml(path)
    catalog = getattr(scenario, "responses", None)
    if catalog is None:
        raise SystemExit(f"{path} authors no `responses:` block — nothing to probe")
    if not isinstance(catalog, ResponseCatalog):
        raise SystemExit(
            f"{path} gave {type(catalog).__name__} for `responses:`, expected a "
            "ResponseCatalog. The scenario loader changed shape; update this probe."
        )
    # A catalog parsed by the loader is unspent, so reading it here is safe.
    # Only the REUSE across router calls is what corrupts a measurement.
    raw: dict[str, Any] = {}
    for rid, spec in catalog.responses.items():
        entry: dict[str, Any] = {
            "intent": spec.intent,
            "instruction": spec.instruction,
            "text": spec.text,
        }
        # Mirrors the plan's export rule: only emit the flags when set, so a
        # round-trip through from_dict is byte-faithful to what was authored.
        if spec.system:
            entry["system"] = True
        if spec.reusable:
            entry["reusable"] = True
        raw[rid] = entry
    return raw


def _load_catalogs(root: Path, scenario_ids: list[str]) -> dict[str, dict[str, Any]]:
    catalogs: dict[str, dict[str, Any]] = {}
    for sid in sorted(set(scenario_ids)):
        catalogs[sid] = _catalog_from_scenario(root, sid)
    return catalogs


# -------------------------------------------------------------------- measure


def _router_for(
    *, provider: str, model: str, api_key: str, timeout_s: float, temperature: float
) -> Any:
    """Build the real provider adapter.

    A named seam rather than an inline branch: the freshness and fault-accounting
    behaviour of `_measure` is the part worth testing, and neither is reachable
    without swapping this out.
    """
    if provider == "gemini":
        from livekit_agent_simulator.caller_contract.router_gemini import (
            GeminiResponseRouter,
        )

        return GeminiResponseRouter(
            api_key=api_key, model=model, timeout_s=timeout_s,
            temperature=temperature,
        )
    from livekit_agent_simulator.caller_contract.router_openai import (
        OpenAIResponseRouter,
    )

    return OpenAIResponseRouter(
        api_key=api_key, model=model, timeout_s=timeout_s,
        temperature=temperature,
    )


async def _measure(
    inputs: list[dict[str, Any]],
    catalogs: dict[str, dict[str, Any]],
    *,
    n: int,
    provider: str,
    model: str,
    api_key: str,
    timeout_s: float,
    temperature: float,
) -> dict[str, Any]:
    router = _router_for(
        provider=provider, model=model, api_key=api_key,
        timeout_s=timeout_s, temperature=temperature,
    )

    results: dict[str, Any] = {}
    unstable = 0
    faults = 0
    total_calls = 0

    for idx, item in enumerate(inputs, start=1):
        scenario_id = item["scenario"]
        text = item["agent_text"]
        expected_sha = item.get("agent_text_sha")
        if expected_sha and expected_sha != _sha(text):
            print(
                f"  !! {scenario_id}: agent_text_sha does not match agent_text — "
                "the pair is stale or was edited. Skipping.",
                file=sys.stderr,
            )
            continue

        seen: list[str] = []
        option_counts: list[int] = []
        err: str | None = None
        for _ in range(n):
            # FRESH catalog per call. `serve()` below depletes it, exactly as
            # the driver does after each routed turn (driver.py:737) — so
            # sharing one would shrink the option set under the measurement
            # and report the harness eating its own evidence.
            catalog = ResponseCatalog.from_dict(
                json.loads(json.dumps(catalogs[scenario_id]))
            )
            try:
                decision = await router.route(
                    agent_transcript=text, catalog=catalog
                )
                option_counts.append(len(catalog.offerable_ids()))
                catalog.serve(decision.response_id)
                seen.append(decision.response_id)
                total_calls += 1
            except Exception as exc:  # noqa: BLE001 — recorded, not swallowed
                err = f"{type(exc).__name__}: {exc}"
                break

        distinct = sorted(set(seen))
        key = f"{scenario_id}::{_sha(text)[:12]}"
        if err:
            faults += 1
            results[key] = {
                "scenario": scenario_id, "agent_text": text, "error": err,
                "calls": len(seen), "response_ids": distinct,
                "option_counts": option_counts,
            }
            status = "FAULT"
        else:
            if len(distinct) > 1:
                unstable += 1
            results[key] = {
                "scenario": scenario_id, "agent_text": text,
                "calls": len(seen), "response_ids": distinct,
                "distinct": len(distinct),
                "option_counts": option_counts,
            }
            status = "UNSTABLE" if len(distinct) > 1 else "stable"

        print(
            f"[{idx}/{len(inputs)}] {status:8} {scenario_id:34} "
            f"{len(seen)} calls -> {', '.join(distinct) or '(none)'}"
        )

    measured = len(results) - faults
    return {
        "n_per_input": n,
        "inputs_measured": measured,
        "inputs_unstable": unstable,
        "inputs_faulted": faults,
        "router_calls": total_calls,
        # Denominator excludes faults: a call that raised is not a flip, and
        # counting it as one would make a network blip look like instability.
        "flip_rate": (unstable / measured) if measured else None,
        "abstention_flip_count": sum(
            1
            for r in results.values()
            if not r.get("error") and len(r.get("response_ids", [])) > 1
            and any(
                resp == _system_id(catalogs[r["scenario"]]) for resp in r["response_ids"]
            )
        ),
        "results": results,
    }


def _system_id(raw_catalog: dict[str, Any]) -> str | None:
    for rid, body in raw_catalog.items():
        if isinstance(body, dict) and body.get("system"):
            return rid
    return None


# ----------------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    cap = sub.add_parser("capture", help="pull agent_text out of an existing report")
    cap.add_argument("--root", required=True, type=Path)
    cap.add_argument("--scenario", required=True)
    cap.add_argument("--out", type=Path, default=Path("router-probe-inputs.json"))

    mea = sub.add_parser("measure", help="call the router N times per fixed input")
    mea.add_argument("--inputs", required=True, type=Path)
    mea.add_argument("--root", required=True, type=Path)
    mea.add_argument("--n", type=int, default=5)
    mea.add_argument("--provider", default="openai", choices=["openai", "gemini"])
    mea.add_argument("--model", default="gpt-4.1-nano")
    mea.add_argument("--timeout-s", type=float, default=10.0)
    mea.add_argument(
        "--temperature", type=float, default=0.0,
        help="0.0 is the point of the exercise. A non-zero value here "
             "re-introduces the variable you are trying to measure.",
    )
    mea.add_argument("--out", type=Path)
    mea.add_argument(
        "--api-key", default=os.environ.get("ROUTER_PROBE_API_KEY", ""),
        help="or set ROUTER_PROBE_API_KEY",
    )

    args = ap.parse_args()

    if args.cmd == "capture":
        reports = sorted(
            # Run dirs are `<counter>-<scenario>-<ts>-<hash>`, so the scenario
            # id is in the middle. A `{scenario}-*` prefix glob matches nothing
            # and reports "no reports" for a scenario that plainly has some.
            (args.root / ".agent-sim" / "reports").glob(f"*-{args.scenario}-*"),
            key=lambda p: p.stat().st_mtime,
        )
        if not reports:
            raise SystemExit(f"no reports for {args.scenario!r} under {args.root}")

        # Every run, not just the latest. The most recent run of a scenario is
        # often the SHORTEST — a run that aborts early still produces a
        # report — and taking only that one silently yields a handful of
        # inputs, or none at all if it died before the agent spoke.
        inputs: list[dict[str, Any]] = []
        seen: set[str] = set()
        per_kind: dict[str, int] = defaultdict(int)
        for report_dir in reports:
            events = report_dir / "events.jsonl"
            if not events.is_file():
                continue
            for line in events.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                ev = json.loads(line)
                kind = ev.get("kind") or ""
                # Preambles are included on purpose: the router IS asked to
                # route them, and they are where a "the model always finds
                # something" pattern shows up. Dropping them would measure a
                # kinder router than the one that runs.
                if kind not in ("transcript.agent.final", "transcript.agent.preamble"):
                    continue
                text = ((ev.get("spec") or {}).get("text") or "").strip()
                if not text or text.startswith("[untranscribed"):
                    continue
                sha = _sha(text)
                if sha in seen:
                    continue
                seen.add(sha)
                per_kind[kind] += 1
                inputs.append(
                    {
                        "scenario": args.scenario,
                        "agent_text": text,
                        "agent_text_sha": sha,
                        "kind": kind,
                        "source_run": report_dir.name,
                    }
                )
        payload = {
            "purpose": "Fixed router inputs. Catalogs are re-read from the "
                       "scenario YAML at measure time, never stored here.",
            "scenario": args.scenario,
            "runs_scanned": [p.name for p in reports],
            "inputs": inputs,
        }
        args.out.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        print(
            f"{len(inputs)} inputs from {len(reports)} run(s) of {args.scenario} "
            f"({', '.join(f'{k.split('.')[-1]}={v}' for k, v in sorted(per_kind.items()))})"
            f" -> {args.out}"
        )
        if not inputs:
            print(
                "  no usable agent utterances found. A scenario that never got\n"
                "  the agent speaking produces a report with no inputs; check the\n"
                "  run actually started before concluding the router is untestable.",
                file=sys.stderr,
            )
        return 0

    if not args.api_key:
        raise SystemExit("set ROUTER_PROBE_API_KEY or pass --api-key")

    data = json.loads(args.inputs.read_text(encoding="utf-8"))
    inputs = data["inputs"]
    catalogs = _load_catalogs(args.root, [i["scenario"] for i in inputs])
    print(
        f"{len(inputs)} inputs across {len(catalogs)} scenario(s), "
        f"{args.n} router calls each, temperature {args.temperature}\n"
    )

    report = asyncio.run(
        _measure(
            inputs, catalogs, n=args.n, provider=args.provider, model=args.model,
            api_key=args.api_key, timeout_s=args.timeout_s,
            temperature=args.temperature,
        )
    )
    report["provider"] = args.provider
    report["model"] = args.model
    report["temperature"] = args.temperature
    report["inputs_file"] = str(args.inputs)

    out = args.out or args.inputs.with_name("router-flip-report.json")
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    rate = report["flip_rate"]
    print("\n" + "=" * 66)
    print(f"  measured            : {report['inputs_measured']}")
    print(f"  unstable inputs     : {report['inputs_unstable']}")
    print(f"  faulted inputs      : {report['inputs_faulted']}")
    print(f"  router calls        : {report['router_calls']}")
    print(f"  flip rate           : {rate:.1%}" if rate is not None else "  flip rate: n/a")
    print(f"  ...of which abstain : {report['abstention_flip_count']}")
    print("=" * 66)
    if report["inputs_measured"] < 20:
        print(
            "\n  WARNING: fewer than 20 measurable inputs. A rate this small has\n"
            "  a confidence interval too wide to quote — report the counts, not\n"
            "  the percentage."
        )
    print(f"\n  -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
