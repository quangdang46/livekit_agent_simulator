# Agent install guide — livekit-agent-simulator (`lk-sim`)

**Audience:** coding agents (Claude Code, Cursor, Codex, AmpCode, Windsurf, …) and humans who paste this URL into an agent.

**Goal:** install the `lk-sim` CLI on the user’s machine, scaffold `.agent-sim/` inside **the user’s project repo** (the LiveKit voice agent under test), fill config safely, register MCP if useful, and prove the setup with `preflight` (and optionally a smoke run).

**Hard rules for the agent:**

1. **Never import or edit the agent-under-test application source** (no patches to worker business code). Only create/edit files under `<target>/.agent-sim/` (gitignored).
2. **Never commit secrets.** `.agent-sim/` must stay gitignored. Do not paste API keys into git-tracked files.
3. **Do not invent LiveKit / Gemini credentials.** Ask the user or read from existing local env files they already have (e.g. `.env` in the target repo) with permission.
4. Prefer **non-interactive** commands. Use `--root <absolute-path>` always.
5. Prefer the **portable installer** (no uv/pip on the user machine) unless the user is developing the simulator package itself.

---

## 0. What you are installing

| Piece | Purpose |
|-------|---------|
| CLI `lk-sim` | Black-box LiveKit room tester + report player |
| MCP server `livekit-agent-simulator` | Same ops for coding agents (`lk-sim mcp`) |
| Target folder `.agent-sim/` | Config, scenarios, reports, local cues/plugins (gitignored) |

The simulator joins LiveKit as `lk-sim-caller`, talks to the user’s already-running voice agent worker via Gemini Live, and writes forensic reports.

**Prerequisites the agent must verify or ask for:**

| Need | Why |
|------|-----|
| macOS / Linux / Windows | Portable packs ship for these |
| Network access | Download release + LiveKit + Gemini |
| A **running** LiveKit voice agent worker | Registered with a known `agent_name` |
| LiveKit Cloud (or self-host) URL + API key/secret | Room create + dispatch |
| Google API key with Gemini Live access | Simulated caller (+ optional judge) |

Optional: coding tools already installed (Claude Code / Cursor / …) so the installer can auto-register MCP.

---

## 1. Detect environment

Run these first and adapt:

```bash
uname -s
which lk-sim || true
lk-sim --help 2>/dev/null | head -5 || true
pwd
# If the user said "this project", use the current workspace root as TARGET_ROOT
```

Set variables (agent should use absolute paths):

```bash
# TARGET_ROOT = the user's LiveKit agent / monorepo root (where .agent-sim will live)
TARGET_ROOT="$(pwd)"   # or the path the user named

# Optional pin (default installer = latest release)
# LK_SIM_REF=v0.1.0
```

If `lk-sim --help` already works, skip §2 install and go to §3 init.

---

## 2. Install `lk-sim` (portable)

### macOS / Linux

```bash
curl -fsSL "https://raw.githubusercontent.com/quangdang46/livekit-agent-simulator/main/install.sh?$(date +%s)" \
  | bash -s -- --verify
```

Pin a release (recommended for CI / reproducible agent setups):

```bash
curl -fsSL "https://raw.githubusercontent.com/quangdang46/livekit-agent-simulator/main/install.sh?$(date +%s)" \
  | bash -s -- --ref v0.1.0 --verify
```

Flags:

| Flag | Meaning |
|------|---------|
| `--verify` | Run post-install check (`lk-sim --help`) |
| `--ref vX.Y.Z` / `--version` | Pin release tag (default: latest) |
| `--no-mcp` | Skip auto MCP registration |
| `--easy-mode` | Append install dir to shell PATH in rc files |
| `--uninstall` | Remove install |

Default binary location: `$HOME/.local/bin/lk-sim`  
If `lk-sim` is not found after install, ensure `~/.local/bin` is on `PATH` for this shell:

```bash
export PATH="$HOME/.local/bin:$PATH"
hash -r 2>/dev/null || true
command -v lk-sim
lk-sim --help | head -20
```

### Windows PowerShell

```powershell
irm "https://raw.githubusercontent.com/quangdang46/livekit-agent-simulator/main/install.ps1" -OutFile "$env:TEMP\lk-sim-install.ps1"
powershell -NoProfile -ExecutionPolicy Bypass -File "$env:TEMP\lk-sim-install.ps1" -Verify
# Optional pin:
# ... -File "$env:TEMP\lk-sim-install.ps1" -Ref v0.1.0 -Verify
```

### Installer success criteria

- `command -v lk-sim` resolves
- `lk-sim --help` exits 0 and lists: `init`, `preflight`, `execute`, `scenario-from-run`, `web`, `mcp`, …
- Prefer not to use `uv run` / `pip install` for end users

### From source (only if user is developing the simulator package)

```bash
git clone https://github.com/quangdang46/livekit-agent-simulator.git
cd livekit-agent-simulator
uv sync --extra dev
uv run lk-sim --help
# When pointing at a target repo, still use absolute --root
uv run lk-sim init --root /abs/path/to/target
```

---

## 3. Init project scaffold in the user’s repo

```bash
lk-sim init --root "$TARGET_ROOT"
```

This creates (if missing):

```text
$TARGET_ROOT/.agent-sim/
  config.yaml          # secrets go here (gitignored)
  scenarios/smoke-hello.jsonl
  reports/
  plugins/example_verify.py
  cues/README.md
```

And ensures `.agent-sim/` is listed in `$TARGET_ROOT/.gitignore`.

**Success criteria:**

```bash
test -f "$TARGET_ROOT/.agent-sim/config.yaml"
test -d "$TARGET_ROOT/.agent-sim/scenarios"
grep -q '\.agent-sim/' "$TARGET_ROOT/.gitignore" || true
lk-sim guide | head -5
```

---

## 4. Fill `.agent-sim/config.yaml` (required fields)

Open `$TARGET_ROOT/.agent-sim/config.yaml`. Required:

| Key | Source |
|-----|--------|
| `livekit.url` | `wss://…` LiveKit Cloud or self-host |
| `livekit.api_key` / `livekit.api_secret` | LiveKit server API credentials |
| `livekit.agent_name` | **Exact** dispatch name the worker registers (e.g. `voice-ai-worker-local` for local dev) |
| `simulator.google_api_key` | Gemini API key with Live model access |

Recommended defaults (already in template):

- `simulator.voice.model`: `gemini-3.1-flash-live-preview`
- `observe.record_audio: true` (stereo WAV L=sim R=agent)
- `judge.model`: `gemini-2.5-flash` (only used if scenario has `PassCriteria`)

Optional but common:

```yaml
livekit:
  # Opaque JSON string for RoomAgentDispatch.metadata (project-specific).
  # Example consumer key — do NOT invent values:
  # dispatch_metadata: '{"customAgentId":"agent_xxx"}'
  agent_join_timeout_ms: 25000

simulator:
  language: "en-US"   # or vi-VN, ja-JP, …
  voice:
    language: "en-US"

observe:
  timezone: "UTC"     # or Asia/Ho_Chi_Minh, …
```

**How the agent should obtain values:**

1. Ask the user for missing secrets, **or**
2. With user permission, read existing local-only files in the target repo (`.env`, `.env.local`, `~/.config/…`) and map:
   - `LIVEKIT_URL` → `livekit.url`
   - `LIVEKIT_API_KEY` → `livekit.api_key`
   - `LIVEKIT_API_SECRET` → `livekit.api_secret`
   - `GOOGLE_API_KEY` / `GEMINI_API_KEY` → `simulator.google_api_key`
   - Worker dispatch name from target docs / `LIVEKIT_AGENT_NAME` / code constants (e.g. `*-local` vs prod name)
3. Never print full secrets in chat logs if avoidable; confirm only that fields are **set**.

Per-scenario dispatch override (if the product needs opaque metadata):

```jsonl
{"kind":"Dispatch","spec":{"metadata":"{\"customAgentId\":\"agent_xxx\"}"}}
```

More consumer wiring notes: [portability.md](../portability.md).

---

## 5. MCP registration (coding agents)

Default installer registers MCP server name **`livekit-agent-simulator`** → command `lk-sim mcp` into detected tools (Claude Code, Cursor, Cline, Windsurf, VS Code Copilot, Gemini CLI, Amazon Q, OpenCode, Codex, Warp).

If the user skipped MCP or tools were installed later, manual Cursor example:

```json
{
  "mcpServers": {
    "livekit-agent-simulator": {
      "command": "lk-sim",
      "args": ["mcp"]
    }
  }
}
```

Dev checkout (package not on PATH):

```json
{
  "mcpServers": {
    "livekit-agent-simulator": {
      "command": "uv",
      "args": ["run", "--directory", "/abs/path/livekit-agent-simulator", "lk-sim", "mcp"]
    }
  }
}
```

Every MCP tool needs `project_root` **except** `guide`. Prefer absolute `project_root`.

Typical MCP flow:

1. `guide`
2. `init_project` → `preflight`
3. `list_scenarios` / `init_scenario` / `validate_scenario`
4. `execute_scenario` (or `execute_scenarios` with `repeat` / `pass_at_k`)
5. `get_run_report` / `get_run_log` / `web`
6. On failure: `scenario_from_run` → review draft → re-run

---

## 6. Preflight (must pass before promising a full call)

```bash
lk-sim preflight --root "$TARGET_ROOT"
# offline config-only:
lk-sim preflight --no-connectivity --root "$TARGET_ROOT"
```

**Success:** JSON `ok: true` with checks for config, livekit.url, folders, google key, and (if connectivity on) `livekit.api` list_rooms.

Common failures:

| Symptom | Fix |
|---------|-----|
| config missing | `lk-sim init --root …` first |
| livekit.api 401 | Wrong URL / api_key / api_secret |
| agent timeout later | Worker not running or `agent_name` mismatch |

---

## 7. Scenarios and first execute (optional but recommended)

List / scaffold:

```bash
lk-sim scenarios --root "$TARGET_ROOT"
lk-sim scenario-init my-case --root "$TARGET_ROOT"   # // guide lines in JSONL
lk-sim validate smoke-hello --root "$TARGET_ROOT"
```

**Worker must be running** and registered with the same `livekit.agent_name` before execute.

```bash
lk-sim execute smoke-hello --root "$TARGET_ROOT"
# flake control:
lk-sim execute smoke-hello --root "$TARGET_ROOT" --repeat 3 --pass-at-k 2
# suite:
lk-sim execute-all --tag smoke --root "$TARGET_ROOT"
```

Inspect:

```bash
lk-sim runs --root "$TARGET_ROOT"
lk-sim report <run-id> --root "$TARGET_ROOT"
lk-sim log <run-id> --kind "transcript.*" --root "$TARGET_ROOT"
lk-sim web --root "$TARGET_ROOT"    # http://127.0.0.1:8765 — Ctrl+C to stop
```

Promote a failure to a draft regression case:

```bash
lk-sim scenario-from-run <run-id> --root "$TARGET_ROOT"           # dry-run
lk-sim scenario-from-run <run-id> --root "$TARGET_ROOT" --write  # write JSONL
# then human/agent reviews Persona + Assert before treating as golden
```

Assert highlights (for scenario authors after setup):

- `latency` — hard gate on turn p95 / TTFW / barge recovery rate  
- `recovery` — agent re-engages after barge  
- `ended_by` — `sim` | `agent` | `detect` after script `hang_up` or natural end  

Script action `hang_up` makes the sim caller leave the room (hard hangup).

---

## 8. Definition of done (agent checklist)

Mark setup complete only when **all** of these are true:

- [ ] `lk-sim --help` works on PATH
- [ ] `$TARGET_ROOT/.agent-sim/config.yaml` exists with LiveKit + `agent_name` + Gemini key set
- [ ] `.agent-sim/` is gitignored
- [ ] `lk-sim preflight --root "$TARGET_ROOT"` → `ok: true`
- [ ] User knows the worker must be running before `execute`
- [ ] (Optional) MCP `livekit-agent-simulator` registered if they use a coding agent
- [ ] (Optional) `lk-sim execute smoke-hello --root "$TARGET_ROOT"` → `status: done` or a clear next fix (agent timeout / Gemini quota)

**Do not claim “fully working E2E”** if preflight failed or the worker is not registered.

---

## 9. Safety / scope boundaries

- **In scope:** install CLI, scaffold `.agent-sim/`, fill config from user/env, preflight, create/edit scenarios under `.agent-sim/scenarios/`, run executes, reports, MCP config.
- **Out of scope:** rewriting the user’s agent product logic; load testing; SIP telephony setup; committing secrets; changing production LiveKit keys without confirmation.
- **Quota:** Gemini free tier can 429 on judge/Live after many runs — report that honestly; hard gates (status/assert/script) still work when judge is soft-error.

---

## 10. One-shot command sequence (copy for agents)

```bash
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"
TARGET_ROOT="$(pwd)"   # change if needed

# 1) Install CLI (skip if already present)
if ! command -v lk-sim >/dev/null 2>&1; then
  curl -fsSL "https://raw.githubusercontent.com/quangdang46/livekit-agent-simulator/main/install.sh?$(date +%s)" \
    | bash -s -- --verify
  export PATH="$HOME/.local/bin:$PATH"
  hash -r 2>/dev/null || true
fi
lk-sim --help >/dev/null

# 2) Scaffold target project
lk-sim init --root "$TARGET_ROOT"

# 3) STOP: fill secrets in $TARGET_ROOT/.agent-sim/config.yaml
#    livekit.url / api_key / api_secret / agent_name
#    simulator.google_api_key
#    Then continue:

lk-sim preflight --root "$TARGET_ROOT"
# 4) Ensure voice agent worker is running with matching agent_name
# lk-sim execute smoke-hello --root "$TARGET_ROOT"
# lk-sim web --root "$TARGET_ROOT"
```

---

## 11. Links

| Resource | URL |
|----------|-----|
| Repo | https://github.com/quangdang46/livekit-agent-simulator |
| Installer (main) | https://raw.githubusercontent.com/quangdang46/livekit-agent-simulator/main/install.sh |
| This guide (raw) | https://raw.githubusercontent.com/quangdang46/livekit-agent-simulator/main/docs/guide/installation.md |
| Ops guide | package `lk-sim guide` or `templates/GUIDE.md` |
| Portability | https://github.com/quangdang46/livekit-agent-simulator/blob/main/docs/portability.md |
| Smoke notes | https://github.com/quangdang46/livekit-agent-simulator/blob/main/docs/smoke-test.md |

When instructions conflict: **this file + `lk-sim guide`** beat outdated blog snippets. Prefer latest release unless the user pins a tag.
