# Archive — legacy caller-path docs (frozen, not live)

These documents describe the pre-contract caller path (persona Realtime
session, `ScriptRunner`, `InterruptRateRunner`, `Script.verify` hooks,
JSONL-first authoring) that is no longer the runtime path. They are kept
frozen for history — do NOT follow them for new work.

Live docs: `docs/behavior-dsl.md` (authoring), `docs/contract-caller-wiring.md`
(single-path design), `docs/caller-runtime-state-machine.md` (driver spec),
`docs/caller-runtime-audit.md` (audit proving the contract path is the only
live path).

Removed from runtime (code deleted, tests removed):
- `script/runtime.py` (ScriptRunner), `script/farewell.py`,
  `script/hang_up_gate.py`, root `script_runner.py` shim
- `tests/test_hang_up_gate.py`, `tests/test_script_farewell.py`,
  `tests/test_script_dtmf_runtime.py`, `tests/test_script_mute_semantics.py`

Kept (still used by the contract path — NOT legacy):
- `script/models.py` helpers, `script_parse.py`, `script/summary.py`,
  `script/verify.py` (legacy-verify stub branch only)
- `caller_nudge.py`, `interrupt_rate.py` (`parse_interrupt_rate` used by
  `behavior_compile`), `behavior_compile.py`
- `.jsonl` parsing + `lks convert` (YAML canonical, JSONL compat kept)
