# base-agent — general-purpose LiveKit voice agent demo

A general-purpose LiveKit voice agent that exercises the **normal** LiveKit agent
capabilities — a flexible black-box target for `lks` beyond the DTMF demo. Use it
to test many scenario types (handoff, tools, tasks, natural conversation) against
both OpenAI Realtime and Gemini Live caller brains.

## What it demos

| Capability | Agent side | lks can assert |
|---|---|---|
| **Multi-agent handoff** | `FrontDeskAgent` (triage) routes to `BillingAgent` / `SupportAgent` / `SalesAgent` via `@function_tool` returns | `handoff` / `no_unplanned_handoff` outcomes |
| **Function tools** | `check_hours`, `book_appointment`, `lookup_order`, `transfer_to_*` | `tool_order`, `tools` (min/max count, args) |
| **Supervisor task** | `GetAccountNumberTask` (`AgentTask`) collects + confirms the account number in a sub-conversation, returns a typed result | `tool.start`/`tool.end` for `account_number_collected` |
| **Natural greeting + conversation** | `on_enter` greets, LLM-driven flow | transcript, latency, recovery asserts |
| **Dual stack** | `AGENT_STACK=openai` (OpenAI Realtime, default) or `gemini` (Gemini Live) | `simulator.provider: openai` / `google` |

## Layout

```
agent/agent.py            # FrontDeskAgent + Billing/Support/Sales specialists + GetAccountNumberTask + AgentServer
scenarios/*.yaml          # lks scenarios (frontdesk-hours, billing-handoff, order-lookup)
.agent-sim/config.yaml    # lks simulator config (gitignored)
.env.example              # secrets/keys — copy to .env and fill
scripts/bootstrap.sh      # one-shot setup
```

## Setup

```bash
cd demo/base-agent
./scripts/bootstrap.sh      # creates .env + .venv + lks init
# edit .env: LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET,
#            AGENT_NAME, GOOGLE_API_KEY (or OPENAI_API_KEY), AGENT_STACK
```

## Run

```bash
# terminal 1 — agent worker
AGENT_STACK=openai .venv/Scripts/python.exe -m agent.agent dev

# terminal 2 — lks (from the livekit-agent-simulator package source)
lks preflight --root .
lks execute frontdesk-hours --root .
lks execute billing-handoff --root .   # exercises the handoff assert
lks execute order-lookup --root .      # exercises the supervisor task tools
```

## Scenarios

- **frontdesk-hours** — caller asks Saturday hours; agent uses `check_hours` tool. `verdict: pass` in testing.
- **billing-handoff** — caller has a billing question; agent routes to `BillingAgent` via handoff. `handoff` outcome assert passes (`handoffs: 2`).
- **order-lookup** — caller provides account 12345; `lookup_order` → `GetAccountNumberTask` collects it, agent confirms "order shipped".

## Notes

- Plugins are imported at **module level** (main thread) — LiveKit raises
  `Plugins must be registered on the main thread` if imported lazily inside the
  async `entrypoint`.
- `lks execute` runs until the scenario's `timeout_s`; do **not** wrap it in a
  shell `timeout` (on Windows that orphans the child and leaves the run stuck
  in `running`).
