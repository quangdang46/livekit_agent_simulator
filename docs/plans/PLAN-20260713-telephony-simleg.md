# Plan Report — SimLeg telephony (unified inbound / WebRTC / outbound)

## Summary (read this first)

- **You asked:** Research kỹ (Exa + sub-agents) và tạo plan cho telephony — **Gemini luôn thay người**, log/assert/replay giữ như hiện tại; inbound / WebRTC / outbound dùng **chung design pattern**.
- **What is going on:** Hôm nay lk-sim chỉ có `WebRtcSimLeg` ngầm (`connect_simulator` + `GeminiCallerBridge`). SIP outbound/inbound cần cùng pipeline 7 phase nhưng **đổi transport leg** — không fork Observer/converse/judge.
- **We recommend:** **Template Method + Strategy** — `SimLeg` protocol; `Caller.mode` trong scenario (không trong config); `telephony.*` chỉ defaults hạ tầng; T0 refactor → T1 contract → **T2 spike SIP↔Gemini bridge** (critical path) → outbound → inbound → SIP asserts.
- **Risk:** **High** on T2 (audio bridge). **Low** on T0/T1. Medium on PSTN flake/cost — mitigate bằng loopback recipe + sim endpoint.
- **Status:** Waiting for your OK — reply **go ahead** to implement

## Feature planning

### Recommended approach (one paragraph)

Giữ `run_orchestrator` làm **template method** (7 phase). Tách Phase 3 thành `SimLeg.connect()` — factory từ `scenario.effective_caller_mode()`. **SimBrain** (`GeminiCallerBridge`, `ScriptRunner`, `ParallelMicMixer`) và **ObserveReport** (`Observer`, `EventWriter`, `LocalConversationRecorder`) **không nhân bản** theo mode. Chỉ thay:

1. Cách **Gemini audio** tới agent (WebRTC mic publish vs SIP RTP bridge)
2. Cách **agent audio** tới Gemini (vẫn subscribe agent WebRTC track trong room — verified)
3. Thứ tự telephony (outbound: `prepare_ms` → `CreateSIPParticipant`)

**Product rule:** Gemini = simulated human trên mọi topology. Không ship observe-only PSTN.

### Three topologies — same stack

| `Caller.mode` | Gemini role | Harness action | Agent hears sim via |
|---|---|---|---|
| `webrtc_sim` (default) | Caller | Join WebRTC `lk-sim-caller` | WebRTC audio track |
| `outbound_sip` | Callee (nhấc máy) | `lkapi.sip.create_sip_participant` sau agent join | SIP participant in room |
| `inbound_sip` | Caller (gọi PSTN) | Originate call tới `Telephony.dial_in` (outbound trunk → inbound DID) | SIP participant in room |

**Shared unchanged:** Persona, Script, Behavior, Execute, Assert, PassCriteria judge, `events.jsonl`, `summary.json`, `conversation.wav`, suite gate, `--repeat` / `pass@k`.

### Design pattern diagram

```text
RunOrchestrator
  ├─ RoomLifecycle     LiveKitAdapter.create_room_and_dispatch + wait_for_agent
  ├─ SimLeg.connect    Strategy: WebRtc | OutboundSip | InboundSip
  ├─ SimBrain          GeminiCallerBridge + ScriptRunner (+ nudge)
  ├─ Converse          _conversation_loop (unchanged)
  ├─ ObserveReport     Observer + EventWriter + recorder
  └─ VerifyJudge       asserts + script_verify + judge
```

### Prior art (GitHub / industry)

| Source | What we reuse | What we avoid |
|---|---|---|
| [livekit/sip `lktest-sip-outbound`](https://github.com/livekit/sip/tree/main/test/lktest-sip-outbound) | Loopback SIP E2E without PSTN; attr/audio checks | Copying Go test into Python core |
| [LiveKit make_call recipe](https://docs.livekit.io/reference/recipes/make_call/) | `dispatch` → `create_sip_participant` | Dial-before-agent (bad for sim) |
| [outbound-caller-python](https://github.com/livekit-examples/outbound-caller-python) | `session.start` before dial; `wait_until_answered`; `SipCallError` | Pattern B as **default** (not black-box) |
| [voice-ai-worker `scripts/outbound.ts`](C:\Users\ADMIN\Documents\Projects\voice-ai-worker\scripts\outbound.ts) | `prepare_ms` after dispatch | Verbatim port; parsing `customAgentId` in core |
| [sip2ai](https://github.com/dmitry-sinina/sip2ai) / [didww-voice-agent](https://github.com/edwinux/didww-voice-agent) | RTP ↔ Gemini Live bridge patterns for **T2 spike** | Full drachtio stack inside lk-sim v1 |
| [Hamming](https://hamming.ai/) | `call_path` separated from sim brain; forensic replay | Closed SaaS features (50k load, accent matrix) |
| [Coval outbound](https://docs.coval.dev/guides/outbound-voice) | Trigger → agent dials → sim answers (inverted inbound) | PSTN correlation webhooks in v1 |
| [Bluejay telephony](https://docs.getbluejay.ai/simulation-integrations/telephony) | Outbound = sim calls agent; Inbound = agent calls sim number | Cloud-only execution model |
| [Sipfront AI voicebots](https://sipfront.com/blog/2025/08/sipfront-launches-ai-voicebots-for-testing/) | Bidirectional SIP + realtime AI on RTP path | Commercial product scope |

### Integration points (verified in repo)

| File | Role in plan |
|---|---|
| `src/livekit_agent_simulator/run_orchestrator.py:54–215` | Phase 3 seam: replace `connect_simulator` + `room.disconnect` with `SimLegHandle` |
| `src/livekit_agent_simulator/livekit/adapter.py:57–135` | Keep dispatch/wait; move connect to `WebRtcSimLeg`; add `create_sip_participant` |
| `src/livekit_agent_simulator/gemini/live_session.py:80–118` | `watch_agent_tracks` unchanged; `publish_mic` → pluggable `SimAudioSink` |
| `src/livekit_agent_simulator/livekit/observer.py:76–91` | Pass SIP `sim_identity`; role assignment unchanged |
| `src/livekit_agent_simulator/scenario.py:28–39,94–109` | Add `Caller`/`Telephony` kinds + merge helpers |
| `src/livekit_agent_simulator/config.py:139–274` | Optional `telephony:` section + snapshot redaction |

### Sub-agents used

Yes — 4 parallel read-only agents:

1. LiveKit SDK / SIP API patterns ([agent](b51c8c45-3cac-406c-823a-b4d634768f2f))
2. Codebase integration map ([agent](b8318e2d-4184-414c-beb8-f2dc71871eda))
3. GitHub prior art ([agent](39285ab7-2831-4227-b006-97b352c49029))
4. Tests / risk / rollout ([agent](dc6e02ec-788b-4513-9da2-5b7f3e71945f))

Plus Exa: LiveKit outbound/testing docs, Hamming/Coval/Bluejay/Sipfront telephony QA, `sip2ai` / outbound-caller examples.

### Option B (deferred)

**`agent_dials` (Pattern B):** lk-sim chỉ dispatch + observe; worker gọi `create_sip_participant` trong entrypoint. Ship as **T5** — cần target agent hợp tác, không black-box. Không thay Pattern A làm default.

### Open questions (owner — blocking T2)

1. **Callee endpoint for outbound:** Số/URI nào nhận dial và chạy Gemini? (sidecar sip2ai, hairpin inbound rule, hay build `lk-sim callee-bridge` subcommand?)
2. **CI:** Có trunk + sim endpoint dùng chung không? E.164 allowlist?
3. **`wait_until_answered` default:** `true` (strict) vs `false` (match `outbound.ts`)?
4. **Inline trunk in v1:** Chỉ `sip_trunk_id` trước, hay cần inline `SIPOutboundConfig` ngay?
5. **Reference target:** `voice-ai-worker` `.agent-sim/` làm telephony suite mẫu?

## Evidence

1. **LiveKit docs:** [Outbound calls](https://docs.livekit.io/telephony/making-calls/outbound-calls/) — `CreateSIPParticipant`, inline vs stored trunk, `wait_until_answered`, agent must be dispatched separately when harness dials. [Testing](https://docs.livekit.io/telephony/testing/) — verify `kind=SIP`, `sip.callStatus`. [SIP participant](https://docs.livekit.io/reference/telephony/sip-participant/) — attrs `dialing|ringing|active|hangup`.
2. **Python SDK (verified upstream):** `LiveKitAPI.sip.create_sip_participant` — no separate `SipClient`; `SipCallError` with `sip_status_code` — [sip_service.py](https://github.com/livekit/python-sdks/blob/main/livekit-api/livekit/api/sip_service.py). Locked in repo: `livekit-api 1.1.1` (`uv.lock`).
3. **Our code:** `adapter.connect_simulator` (`adapter.py:127–135`) — WebRTC only today. `run_orchestrator` Phase 3 (`run_orchestrator.py:139–168`) — hard-coded `SIM_IDENTITY`.
4. **Reference harness:** `outbound.ts` — room create → dispatch metadata → `prepare_ms` → `createSipParticipant(..., waitUntilAnswered: false)`.

## T2 spike — SIP ↔ Gemini bridge (critical path)

**Problem:** Agent outbound nghe **SIP participant**, không nghe WebRTC `lk-sim-caller`. Gemini hôm nay publish WebRTC mic — **không tự động** lên nhánh PSTN.

**Ranked options (pick one in spike before full T2):**

| Rank | Approach | Pros | Cons |
|---|---|---|---|
| **1** | Harness dial → **user-deployed Gemini SIP bridge** (sip2ai / didww pattern) at `Telephony.call_to` | Matches Pattern A; lk-sim stays thin; black-box | Extra service; user ops |
| **2** | **LiveKit hairpin** (lktest-sip-outbound): outbound trunk → own inbound DID → same/dispatch room | No PSTN; CI-friendly | Trunk setup; bridge still needed at answer point |
| **3** | **`lk-sim callee-bridge` subcommand** (optional package extra): minimal SIP UA + reuse `GeminiCallerBridge` brain | Unified UX; one vendor story | Largest build; RTP/codec work |
| **4** | Pattern B only (agent dials) | LiveKit canonical for product agents | Not generic black-box |

**Spike acceptance (before T2 feature-complete):**

- One end-to-end call: agent speaks ↔ Gemini persona audible on SIP leg
- `events.jsonl` shows `outbound.dial_answered` + alternating `transcript.*`
- Document chosen approach in `docs/telephony.md`

**Codec note:** SIP default G.711 @ 8 kHz; Gemini bridge uses 16 kHz PCM — resample at bridge ([codecs doc](https://docs.livekit.io/reference/telephony/codecs-negotiation/)).

## Steps (implementation checklist)

### T0 — Extract `WebRtcSimLeg` (refactor only)

- [ ] Add `src/livekit_agent_simulator/livekit/sim_leg.py` — protocol + `WebRtcSimLeg`
- [ ] Move `connect_simulator` usage from orchestrator into leg
- [ ] `pytest -q` green; smoke scenarios unchanged

**Acceptance:** Zero behavior change; `sim.connected` identity still `lk-sim-caller`.

### T1 — Contract + factory + docs

- [ ] Parse `Caller` / `Telephony` in `scenario.py` + `scenario_from_dict.py`
- [ ] `effective_caller_mode()`, `effective_telephony(cfg)`
- [ ] `sim_leg_factory(mode)` — SIP modes raise clear "not implemented" until T2/T3
- [ ] `TelephonyConfig` in `config.py` (optional `telephony:` block)
- [ ] `docs/telephony.md` — topology table, config/scenario examples, preflight checklist
- [ ] Unit tests: parse, merge, factory, backward compat (no `Caller` line)

**Acceptance:** Invalid mode / missing `call_to` fails at parse; WebRTC identical to T0.

### T2 — Outbound SIP (`outbound_sip`)

- [ ] `LiveKitAdapter.create_sip_participant()` wrapping `lkapi.sip.create_sip_participant`
- [ ] `OutboundSipSimLeg`: agent join → `prepare_ms` → dial → wait active
- [ ] Events: `outbound.dial_started`, `outbound.dial_answered`, `outbound.dial_failed`, `sip.participant_connected`
- [ ] `SimAudioSink` abstraction; wire Gemini output to bridge (spike result)
- [ ] Observer `sim_identity` = SIP participant identity
- [ ] Template scenario `templates/outbound-customer-sim.jsonl`
- [ ] Manual smoke doc; optional hairpin CI recipe

**Acceptance:** Full persona conversation on outbound path; WAV L=sim R=agent; asserts pass.

### T3 — Inbound SIP (`inbound_sip`)

- [ ] `InboundSipSimLeg`: originate to `Telephony.dial_in` (outbound trunk → inbound DID)
- [ ] Gemini as PSTN caller; `first_speaker: user` default
- [ ] Inbound events; same bridge reuse from T2
- [ ] Document owner setup: inbound trunk + dispatch rule

**Acceptance:** Inbound scenario completes with same forensic artifacts as WebRTC.

### T4 — SIP asserts + suite

- [ ] Assert kinds: `sip_call_status`, `sip_participant_present`
- [ ] `summary.json` / suite columns: mode, dial_ms, sip_status
- [ ] Fixture-based unit tests (no trunk)

### T5 — Pattern B `agent_dials` (optional)

- [ ] `Caller.mode: agent_dials` — dispatch only, wait for SIP participant event
- [ ] Doc example; opaque `Dispatch.metadata` only

## Scenario / config contract

**`config.yaml`** (shared — no mode):

```yaml
livekit: { url, api_key, api_secret, agent_name }
simulator: { google_api_key, ... }
telephony:                    # optional defaults
  sip_trunk_id: ST_xxxx
  prepare_ms: 3000
  wait_until_answered: true
  krisp_enabled: false
```

**Outbound scenario:**

```jsonl
{"kind":"Scenario","spec":{"id":"outbound-customer-sim","tags":["telephony","outbound"]}}
{"kind":"Caller","spec":{"mode":"outbound_sip"}}
{"kind":"Persona","spec":{"name":"Skeptical callee","brief":"You answered a sales call. Be brief."}}
{"kind":"Telephony","spec":{"call_to":"+1..."}}
{"kind":"Execute","spec":{"timeout_s":120,"first_speaker":"user","max_turns":10}}
```

**Inbound scenario:**

```jsonl
{"kind":"Caller","spec":{"mode":"inbound_sip"}}
{"kind":"Telephony","spec":{"dial_in":"+1..."}}
{"kind":"Persona","spec":{"name":"Billing caller"}}
{"kind":"Execute","spec":{"first_speaker":"user"}}
```

**Merge rules:**

```text
effective_mode       = Caller.mode ?? "webrtc_sim"
effective_call_to    = Telephony.call_to   (required if outbound_sip)
effective_dial_in    = Telephony.dial_in   (required if inbound_sip)
effective_trunk        = Telephony.sip_trunk_id ?? config.telephony.sip_trunk_id
effective_prepare_ms = Telephony.prepare_ms ?? config.telephony.prepare_ms
```

## Files to touch

| File | Change |
|---|---|
| `src/livekit_agent_simulator/livekit/sim_leg.py` | **New** — protocol, WebRtc/Outbound/Inbound legs |
| `src/livekit_agent_simulator/livekit/adapter.py` | SIP API methods |
| `src/livekit_agent_simulator/run_orchestrator.py` | Phase 3 via factory |
| `src/livekit_agent_simulator/scenario.py` | Caller/Telephony kinds |
| `src/livekit_agent_simulator/scenario_from_dict.py` | Same |
| `src/livekit_agent_simulator/config.py` | `TelephonyConfig` |
| `src/livekit_agent_simulator/gemini/live_session.py` | `SimAudioSink` hook |
| `src/livekit_agent_simulator/preflight.py` | Telephony checks when SIP scenario |
| `src/livekit_agent_simulator/asserts.py` | SIP assert kinds (T4) |
| `docs/telephony.md` | **New** |
| `templates/outbound-customer-sim.jsonl` | **New** |
| `tests/test_sim_leg*.py`, `tests/test_scenario_caller_telephony.py` | **New** |
| `WIP.md` | Mark implementation in progress when started |

## CI / test strategy

| Tier | What | Trunk? |
|---|---|---|
| **PR (required)** | Unit: scenario parse, factory, config, mocked SIP API, assert fixtures | No |
| **Manual / nightly** | `lk-sim execute --tag telephony` with real trunk + sim endpoint | Yes |
| **Never in public CI** | Dial personal mobile; production trunks without isolation | — |

Extend `tests/test_dispatch_mock.py` pattern for `create_sip_participant` mocks.

Mark integration: `@pytest.mark.telephony` — skip unless config fixture has `sip_trunk_id`.

## Risk matrix

| Risk | Level | Mitigation |
|---|---|---|
| SIP↔Gemini bridge | **High** | T2 spike gate; cite sip2ai; don't ship observe-only |
| PSTN cost / wrong number | **High** | Allowlist doc; dedicated test trunk; hairpin for CI |
| Agent join vs dial race | Med | `prepare_ms` default 3000; dial only after `dispatch.agent_joined` |
| WebRTC regression | Med | T0 zero behavior change; default `webrtc_sim` |
| Opaque metadata parsing | **High** if done | Forbidden — `Telephony.*` only for dial params |
| `sip.callStatus` empty on SDK | Med | Prefer `wait_until_answered=True`; poll fallback |

## Anti-patterns

- Put `mode` in shared `config.yaml`
- Observe-only outbound without Gemini
- Duplicate converse/observe per mode
- Hardcode `customAgentId` / dashboard env in `src/`
- Port `outbound.ts` verbatim into core

## If you want more detail

### Recommended harness sequence (outbound Pattern A)

```text
create_room → [room_prepare_ms] → create_dispatch → wait_agent
→ connect_observer (WebRTC, auto_subscribe)
→ [telephony.prepare_ms]
→ create_sip_participant(wait_until_answered=True)
→ wire Gemini ↔ SIP via chosen bridge
→ converse until end condition
```

### Inbound note (verified)

LiveKit has **no** RPC to inject inbound SIP without real INVITE. `inbound_sip` mode = harness originates call to `dial_in` via outbound trunk (same as Coval trigger inversion), or external softphone — not a separate "simulate inbound" API.

### Competitive positioning

lk-sim niche unchanged: **open, local-first, forensic, MCP-native**. Telephony closes gap vs Hamming/Coval on `call_path` while keeping portable scenario JSONL + self-host reports.

---

**Next step:** Reply **go ahead** to implement **T0 + T1** first (safe refactor + contract), then **T2 spike** with your answers to open questions 1–2.
