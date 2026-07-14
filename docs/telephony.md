# Telephony (WebRTC / inbound SIP / outbound SIP)

Portable transport modes for lk-sim. **Mode is never set in `config.yaml`** — put it in the scenario.

## Breaking (2026-07-14)

| Old name | Current name | Meaning |
|---|---|---|
| `outbound_sip` (hairpin) | **`outbound_sim_callee`** | Gemini answers as SIP callee via sim DID (2 rooms) |
| `outbound_sip` (human pickup) | **`outbound_human_pickup`** | Human answers; Gemini WebRTC colocated (1 room) |

`outbound_sip` is **removed** (no alias). Update target scenarios.

## Modes

| `Caller.mode` | Gemini role | Rooms | Best for |
|---|---|---|---|
| `webrtc_sim` (default) | Caller (WebRTC) | 1 | Automated / CI |
| `inbound_sip` | Caller dials agent DID | 2 (Cloud hairpin) | Automated |
| **`outbound_human_pickup`** | Speaks after **human** answers | **1** (colocated) | Manual / attended |
| **`outbound_sim_callee`** | SIP **callee** via sim DID | **2** (Cloud hairpin) | Automated PSTN callee |
| `agent_dials` | Callee; agent process dials | 2 | Agent-owned dial |

## Precedence

```text
Scenario Telephony.*  >  config.yaml telephony.*  >  built-ins
```

| Field | Scenario | Config | Required for |
|---|---|---|---|
| mode | `Caller.mode` | — | all (default webrtc_sim) |
| call_to | `Telephony.call_to` | `telephony.sim_inbound_number` (**only** `outbound_sim_callee`) | `outbound_human_pickup`, `outbound_sim_callee` |
| dial_in | `Telephony.dial_in` | `telephony.dial_in` | inbound_sip |
| trunk | `Telephony.sip_trunk_id` | `telephony.outbound_trunk_id` | SIP modes |
| prepare_ms | optional | `telephony.prepare_ms` (default 3000) | outbound |
| wait_until_answered | optional | default `true` | SIP dial |
| handset_isolation | optional | `telephony.handset_isolation` | **outbound_human_pickup** only |

### `handset_isolation` (`outbound_human_pickup`)

| Value | Behavior |
|---|---|
| `mute_and_unsubscribe` (**default**) | Mute SIP uplink + deny handset subscribe to room audio |
| `mute_uplink` | Mute human mic only (handset may still hear agent) |
| `none` | Leave human in the mix |
| `remove` | Kick SIP participant (often hangs up agent — opt-in) |

## Config example (target `.agent-sim/config.yaml`)

```yaml
livekit:
  url: "wss://your-project.livekit.cloud"
  api_key: "APIxxxxxxxx"
  api_secret: "secretxxxxxxxx"
  agent_name: "your-agent-name"

simulator:
  google_api_key: "AIzaxxxxxxxx"

telephony:
  outbound_trunk_id: "ST_xxxxxxxxxxxx"
  dial_in: "+15551234567"              # agent inbound DID (inbound_sip)
  sim_inbound_number: "+15559876543"   # Gemini callee DID (outbound_sim_callee only)
  prepare_ms: 3000
  wait_until_answered: true
  handset_isolation: mute_and_unsubscribe  # outbound_human_pickup
```

## Scenario examples

- Human pickup: `templates/outbound-human-pickup.jsonl` — `Caller.mode: outbound_human_pickup` + `Telephony.call_to` (real phone)
- Gemini callee hairpin: `templates/outbound-callee-sim.jsonl` — `Caller.mode: outbound_sim_callee`
- Inbound: `templates/inbound-caller-sim.jsonl`

### Inbound room discovery

After dial answer, lk-sim resolves the agent room without latching leftovers:

1. `Telephony.agent_room` / `agent_room_name_template` (deterministic), else
2. SIP participant attrs containing CreateSIPParticipant `sip_call_id`, else
3. **Fresh** dial-digit room that did not exist in the pre-dial room snapshot (or was created at/after dial)

Leftover `call-+DID-*` rooms from aborted runs are ignored. Events emit
`inbound.agent_room_resolved.phase` (`sip_call_id` | `new_dial_room` | `newest_dial_room`).

### `outbound_human_pickup` sequence

```text
agent-room → dispatch agent → dial human (wait_until_answered)
→ human answers → Gemini WebRTC joins same room → isolate handset
→ Gemini ↔ agent converse
```

### `outbound_sim_callee` sequence

```text
sim-room (Gemini ready) + agent-room → dial sim DID → hairpin into sim-room
```

## Owner setup (outside package)

1. LiveKit outbound trunk (carrier termination URI, not LiveKit host).
2. Agent inbound DID + dispatch rule (for `inbound_sip`).
3. For `outbound_sim_callee`: sim/Gemini answer DID + dispatch into sim-room.
4. For `outbound_human_pickup`: any answerable E.164; leave handset connected (dead air if isolated).
5. Secrets only in gitignored target config.

## Asserts

```json
{"kind":"Assert","spec":{"sip":{"participant_present":true,"dial_answered":true,"call_status_any":["active"]}}}
```

## Design

- **Template Method** — one orchestrator pipeline.
- **Strategy** — `SimLeg` (`WebRtc` / `InboundSip` / `OutboundHumanPickup` / `OutboundSimCallee` / `AgentDials`).
- **Factory** — `sim_leg_factory(mode)`.

Package core stays target-agnostic: no product names, agent IDs, or dashboard keys in `src/`.
