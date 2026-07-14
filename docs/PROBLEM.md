# Known Problems & Research Items

## Gemini "answering" calls (SIP callee) on LiveKit Cloud

### Problem

Mode **`outbound_sim_callee`**: `call_to` must be a **sim DID** that routes into the **sim-room** where Gemini already sits (Cloud hairpin).

Calling a **real PSTN number** without that DID:

```
lk-sim dial +84xxxxxxxxx  ──►  Cloud Trunk  ──►  PSTN  ──►  Real phone rings
                                                              │
                                                            Human picks up
                                                              │
                                                            Agent-room hears human;
                                                            Gemini is in a *different* room
                                                            → audio paths split (2 rooms, no hairpin)
```

### Mitigation (implemented)

| Mode | Use when |
|---|---|
| **`outbound_human_pickup`** | Manual: human answers to connect; Gemini joins **same** agent-room and speaks (no sim DID) |
| **`outbound_sim_callee`** | Automated: true Gemini-as-SIP-callee via sim DID + dispatch (2-room hairpin) |

See: `docs/telephony.md`

### Potential solutions (sim-callee path)

| Approach | Description | Requires |
|---|---|---|
| **DID routing to sim-room** | Buy a number / DID + dispatch rule pointing to sim-room | LiveKit provisioning |
| **Self-host SIP + sip-to-ai** | LiveKit SIP docker local + in-process callee | Docker + vendored `sip-to-ai` Apache-2.0 code |
| **Twilio SIP trunk hairpin** | Twilio routing loops back into sim-room | Twilio config |

### Related references

- Plan: `docs/plans/PLAN-20260713-simleg-refactor.md` (T6 vendor sip-to-ai)
- Telephony docs: `docs/telephony.md`
- Cloned ref: `references/sip-to-ai/` (Apache-2.0 licensed SIP/RTP stack)

### Preflight (lk-sim)

`lk-sim preflight --root <target>` reports:

| Check | Meaning |
|---|---|
| `telephony` | trunk / dial_in / sim_inbound presence |
| `telephony.outbound_sim_callee` | **warn** if trunk without `sim_inbound_number`; **pass** when both set (still requires correct LiveKit dispatch rule) |

Scenario parse/run still **fail-fast** via `validate_telephony_for_mode` when `Caller.mode=outbound_sim_callee` and no `call_to` / `sim_inbound_number`.

Portable: no carrier-specific branches — only LiveKit trunk id + E.164/DID strings from config/scenario.
