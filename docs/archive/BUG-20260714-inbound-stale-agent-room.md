# Plan Report

## Summary (read this first)
- **You asked:** Investigate why `inbound-caller-script` fails (dial OK, then dead air / no agent speech).
- **What is going on:** SIP dial and answer work. The simulator then attaches its observer to a **leftover agent room** from an earlier aborted run (`call-_+61741581902_YFCimsMrM4yk`), instead of the room for the new dial. It listens to a zombie silent agent track while the caller speaks into a fresh sim-room SIP leg. Silence timer ends the run. Large `agent_samples` in the WAV are mostly **digital silence**, not speech.
- **We recommend:** Fix inbound room discovery in `livekit-agent-simulator` — do **not** accept Phase-2 dial-digit rooms without a matching `sip_call_id`; wire `resolve_by_sip_call_id` (fail-fast) as planned in `PLAN-20260713-simleg-refactor.md` R3. Before re-test, delete orphan `call-_+61741581902_*` rooms.
- **Risk:** Medium (telephony discover change; wrong rooms under `--parallel` today)
- **Status:** Implemented — reply **re-test** after wiping leftover `call-*` rooms; reinstall/use local package

## Bug investigation
- **Verified root cause:** Stale agent-room latch in `LiveKitAdapter.find_agent_room` Phase 2 (dial-in digit / name match) after Phase 1 `sip_call_id` does not bind. Inbound path uses this fuzzy finder; safer `room_resolve.resolve_by_sip_call_id` is **unused**.
- **Hypotheses ranked:**
  1. **Stale room via Phase-2 dial digits** (verified) — same room/identity/track across two distinct `SCL_*` dials.
  2. **Phase-1 sip_call_id hairpin mismatch** (partially verified) — outbound CreateSIPParticipant `SCL_*` may not appear on the *inbound* agent-room SIP attrs → falls through to Phase 2.
  3. **Agent genuinely silent on a correct room** (unlikely primary) — does not explain identical room + track SID reuse.
  4. **Observe-only gap / agent spoke but no transcript** (killed) — R channel of `conversation.wav` is near-zero energy (peak 1–2).
- **Sub-agents used:** yes (4 parallel read-only: repro/scope, code path, regression, proof/observability)
- **Citations checked:**
  - Reports `inbound-caller-script-20260714-100413-f743` + `…100531-2964`: same room `call-_+61741581902_YFCimsMrM4yk`, same `agent-AJ_DjohTTM4GHJz`, same track `TR_AMJrTNFqzqgnqL`; different `sip_call_id` (`SCL_Ebo…` vs `SCL_Gok…`); both use `inbound.agent_room_discover` (not deterministic).
  - Terminal 27: first run ended with `KeyboardInterrupt` → cleanup `delete_room` skipped → orphan room left.
  - `find_agent_room` Phase 2 (`adapter.py` ~176–195): returns first room whose name contains dial digits + any `agent-*`, **without** requiring this call’s `sip_call_id`.
  - `InboundSipSimLeg.connect` (`sim_leg/inbound.py` ~111–130): calls `find_agent_room(..., prefer_name_substr=dial_in, sip_call_id_substr=sip_call_id)`.
  - `resolve_by_sip_call_id` (`sim_leg/room_resolve.py`): defined, **never imported/called** elsewhere.
  - Commit `214716a` introduced A+B discover; plan R3 warned fail-fast / no silent multi-room pick.
  - WAV RMS on `…2964/conversation.wav`: L has speech around cue (~RMS 3k); R ≈ silence (RMS 0.1, `|x|>500` count = 0) → not an observe-gap.

## Evidence
1. **LiveKit / SIP:** CreateSIPParticipant returns `sip_call_id` (`SCL_*`). Inbound agent-room SIP attrs use `sip.callID` / `sip.callIDFull` per LiveKit SIP (`AttrSIPCallIDFull` in livekit/sip). Hairpin may mean the outbound API id is **not** the same string as the inbound leg attr — Phase 1 can miss.
2. **GitHub prior art:** livekit/sip callID / `callIDFull` attribute plumbing ([commit a094a63](https://github.com/livekit/sip/commit/a094a6395a9a8e09c5ecbae0b658411f2c0ec14f)) — confirms attrs exist, not that outbound CreateSIPParticipant id equals inbound room attr for PSTN hairpin.
3. **Our sim code:** `adapter.find_agent_room` Phase 2 dial-digit latch; unused `room_resolve.py`.
4. **Our reports:** identical room fingerprint across consecutive inbound runs; `dead_call_silence`; SIP asserts still pass (they only check dial answered, not room correlation).

## Steps (simple checklist)
1. [ ] **Ops:** List/delete leftover `call-_+61741581902_*` rooms in LiveKit Cloud before next inbound test.
2. [ ] **Fix:** Inbound discover must require `sip_call_id` match (use `resolve_by_sip_call_id`) OR deterministic `Telephony.agent_room` / template; **remove or harden** Phase-2 dial-digit “first agent wins” so it cannot return a room without this call’s SIP attrs.
3. [ ] Emit discover phase winner in events (`phase=sip_call_id|name|sip_any`) for forensics.
4. [ ] Test: two consecutive inbound runs after fixing → different `call-…` suffixes; agent finals > 0 when worker healthy.
5. [ ] Optional: if Phase 1 still never matches hairpin SCL ↔ inbound attrs, add alternate correlation (e.g. wait for new room created after dial with dial digits **and** SIP joined after dial timestamp / matching active SIP for *this* dial).

## Files to touch
- `src/livekit_agent_simulator/livekit/adapter.py` — harden `find_agent_room` (Phase 2)
- `src/livekit_agent_simulator/livekit/sim_leg/inbound.py` — prefer `resolve_by_sip_call_id` / fail-fast
- `src/livekit_agent_simulator/livekit/sim_leg/room_resolve.py` — wire into inbound path
- `tests/` — unmatched SCL + stale dial-named room must **not** win
- Target ops only: wipe orphan rooms; re-run `inbound-caller-script`

## If you want more detail
### Why SIP asserts still “pass”
`sip_dial_answered` / `active` come from `inbound.answered` on the **sim** outbound dial, not from proving the discovered agent-room owns that media.

### Why `agent_samples` looked large
Recorder keeps a full-length agent PCM buffer; silence still counts samples. RMS proves R is empty.

### Secondary (not root)
Judge `429` on `gemini-2.5-flash` free tier is postmortem noise. Scenario `Dispatch.customAgentId` does not bind Cloud SIP (discover path does not `dispatch_agent`). Local `agent_name: voice-ai-worker-local-1` is irrelevant for Cloud inbound rule (`voice-ai-worker`).
