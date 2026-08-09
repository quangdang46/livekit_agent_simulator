# DTMF feature demo

A minimal, runnable LiveKit voice agent + `lks` simulator setup that exercises
**DTMF (keypad tones)** in both directions. Companion for the
`feat/dtmf-script-action` branch of `livekit-agent-simulator`.

## What it demos

| Direction | Agent side | Simulator side |
|---|---|---|
| **Receive** — caller presses keys | `room.on("sip_dtmf_received")` → `rtc.SipDTMF` (`.code` / `.digit`) → agent answers from an IVR menu | Script step `action: dtmf`, `digits: "1"` — the simulated caller publishes tones with `local_participant.publish_dtmf` |
| **Send** — agent presses keys | `local_participant.publish_dtmf(code, digit)` | Caller persona just talks; agent's tones are visible in the run log (`sim.script.dtmf`) |

## How DTMF works in LiveKit (research summary)

- Tones are **not audio** — they travel as `telephone-event/8000` RTP payloads /
  data packets per **RFC 4733**, so they work across codecs and even when the
  other side mutes.
- **Receive:** `@room.on("sip_dtmf_received")` fires for keypad tones from
  telephony (SIP) participants. `SipDTMF` carries `code` (int) and `digit` (str).
- **Send:** any participant can publish: `await room.local_participant.publish_dtmf(code=1, digit="1")`.
  Code table: `0-9 → 0-9`, `* → 10`, `# → 11`. Digits `*`/`#` must be sent as
  their numeric code (RFC 4733 §3.2).
- **Agents framework extras:** `AgentSession(ivr_detection=True)` lets an
  outbound agent detect IVR menus and relay tones; the prebuilt
  `GetDtmfTask` collects a fixed number of digits (spoken **or** keypad) with
  per-digit timeout and stop-event; `send_dtmf_events` tool sends tones
  from the model. This demo uses the raw room event instead, so it works with
  any model stack and shows the mechanism explicitly.
- **Sources:** [docs.livekit.io/telephony/features/dtmf](https://docs.livekit.io/telephony/features/dtmf/),
  [docs.livekit.io/agents/prebuilt/tasks/get-dtmf](https://docs.livekit.io/agents/prebuilt/tasks/get-dtmf/),
  [basic_dtmf_agent.py example](https://github.com/livekit/agents/blob/main/examples/telephony/basic_dtmf_agent.py).

## Layout

```
agent/dtmf_agent.py        # the voice agent (AgentServer + DtmfAgent, receive + send)
scenarios/dtmf-menu.yaml   # lks scenario: persona presses keys via script action=dtmf
.agent-sim/config.yaml     # lks simulator config (generated from .env — gitignored)
.env.example               # all secrets/keys — copy to .env and fill
scripts/bootstrap.sh       # one-shot setup: .env -> venv -> config.yaml
scripts/gen-config.py      # expand .env into .agent-sim/config.yaml
```

## Setup (driven by `.env`)

```bash
cd demo/dtmf-feature
./scripts/bootstrap.sh          # creates .env from example, creates .venv, generates config
# edit .env: LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET,
#            AGENT_NAME, GOOGLE_API_KEY (or OPENAI_API_KEY + SIM_CALLER_API_KEY)
source .venv/bin/activate
```

Requirements: a LiveKit Cloud project (or self-hosted server) and a Google
Gemini (or OpenAI) API key. The agent registers as `AGENT_NAME`; the simulator
must use the same name in `.agent-sim/config.yaml` (done automatically).

## Run

```bash
# terminal 1 — the agent worker
dtmf-agent dev --config livekit.toml --log-level info

# terminal 2 — the simulator
lks preflight --root .
lks run --root . dtmf-menu
```

Watch the agent log for `[dtmf] received digit '1' from ...` — that proves the
simulated caller's keypad press arrived end-to-end.

## Notes

- `.agent-sim/config.yaml` and `.env` are gitignored — never commit credentials.
- `scenarios/dtmf-menu.yaml` uses `action: dtmf` script steps, which require
  the `feat/dtmf-script-action` branch of livekit-agent-simulator (the lks
  installed in this repo's `.venv` is the editable local copy).
- `w` in a digits string = 120ms pause between tones (e.g. `"2w1w0#"`).
