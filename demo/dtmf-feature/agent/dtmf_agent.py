"""DTMF demo agent — a tiny IVR that sends and receives DTMF.

Two DTMF directions, both real and observable by the lks simulator:

* **Receive** — the agent greets, then asks the caller to press 1..4. The
  caller's DTMF arrives on the ``sip_dtmf_received`` room event (LiveKit
  relays keypad tones as data packets, RFC 4733, not audio).
* **Send** — the agent dials into a phone system and, on the greeting from
  the other side, presses digits to navigate an IVR via
  ``local_participant.publish_dtmf``.

The menu is wired up in code with a plain ``@room.on("sip_dtmf_received")``
handler (no prebuilt tasks) so it works with any model stack and is easy to
read. See README.md for the GetDtmfTask-based alternative.

Run:
    uv run dtmf-agent dev --config livekit.toml --log-level info
"""

from __future__ import annotations

import asyncio
import os

from dotenv import load_dotenv
from livekit import rtc
from livekit.agents import (
    Agent,
    AgentSession,
    AgentServer,
    JobContext,
    MetricsCollectedEvent,
    cli,
    metrics,
)
from livekit.agents.llm.tool_context import ToolError, function_tool
from livekit.plugins import deepgram, openai, silero

load_dotenv()

# Codec table (RFC 4733 section 3.2): digits 0-9 map to 0-9, * -> 10, # -> 11.
DTMF_CODE = {str(d): d for d in range(10)} | {"*": 10, "#": 11}

IVR_MENU = {
    "1": "Plan details: you are on the $100 per month plan.",
    "2": "International data roaming is now enabled.",
    "3": "Your new plan will be $150 per month.",
    "4": "I will transfer you to a human agent now.",
}


def _dtmf_chat_message(digit: str) -> str:
    return f"[User pressed keypad key: {digit}]"


class DtmfAgent(Agent):
    """Receives keypad tones via the room event and replies from the menu."""

    def __init__(self) -> None:
        super().__init__(
            instructions=(
                "You are the DTMF demo assistant at Acme Support. Be friendly and concise. "
                "When the caller greets you, tell them they can use the phone keypad: "
                "press 1 for plan details, 2 for international data roaming, "
                "3 for upgrade options, or 4 to speak to a human agent. "
                "When the caller presses a key, the system tells you which key "
                "they pressed; reply with the matching outcome."
            ),
        )

    @function_tool
    async def start_ivr_menu(self, context) -> str:
        """Start the keypad menu: read the options to the caller, then wait for
        a single digit (1-4)."""
        return (
            "Read the following options to the caller now, then pause and wait "
            "for them to press a key: "
            "press 1 for plan details, 2 to enable international data roaming, "
            "3 to explore upgrade options, or 4 to speak to a human agent."
        )

    @function_tool
    async def press_one(self, context) -> str:
        """Press keypad key 1."""
        return await self._press("1")

    @function_tool
    async def press_two(self, context) -> str:
        """Press keypad key 2."""
        return await self._press("2")

    @function_tool
    async def press_pound(self, context) -> str:
        """Press keypad key # (pound)."""
        return await self._press("#")

    async def _press(self, digit: str) -> str:
        room = getattr(self, "room", None) or getattr(self, "_room", None)
        if room is None or room.local_participant is None:
            return "Not connected yet."
        await room.local_participant.publish_dtmf(code=DTMF_CODE[digit], digit=digit)
        return f"Pressed key {digit}."


server = AgentServer()


@server.rtc_session(agent_name=os.getenv("AGENT_NAME", "dtmf-demo-local"))
async def entrypoint(ctx: JobContext) -> None:
    session: AgentSession = AgentSession(
        vad=silero.VAD.load(),
        stt=deepgram.STT(model="nova-3") if os.getenv("DEEPGRAM_API_KEY") else openai.STT(),
        llm=openai.LLM(model="gpt-4.1-mini"),
        tts=openai.TTS(voice="nova"),
    )

    @session.on("metrics_collected")
    def _on_metrics(ev: MetricsCollectedEvent) -> None:
        metrics.log_metrics(ev.metrics)

    # ---------- receive side: keypad tones from the caller ----------
    @ctx.room.on("sip_dtmf_received")
    def _on_dtmf(dtmf: rtc.SipDTMF) -> None:
        digit = dtmf.digit
        sender = dtmf.participant.identity if dtmf.participant else "unknown"
        print(f"[dtmf] received digit {digit!r} from {sender} (code {dtmf.code})")
        reply = IVR_MENU.get(digit, "That key is not valid.")
        asyncio.create_task(
            session.generate_reply(
                instructions=_dtmf_chat_message(digit) + "\n" + reply,
                allow_interruptions=True,
            )
        )

    await session.start(
        agent=DtmfAgent(),
        room=ctx.room,
    )

    await asyncio.sleep(3600)


if __name__ == "__main__":
    cli.run_app(server)
