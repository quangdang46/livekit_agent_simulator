"""General-purpose LiveKit voice agent — a flexible black-box target for lks.

Exercises the normal LiveKit agent capabilities so lks can test many scenarios
(not just DTMF):

- **Multi-agent handoff**: a FrontDeskAgent (triage) routes to BillingAgent /
  SupportAgent / SalesAgent via ``@function_tool`` returns — the LLM decides
  when to hand off. lks can assert ``handoff`` / ``no_unplanned_handoff``.
- **Function tools**: ``check_hours``, ``book_appointment``, ``lookup_order``,
  ``transfer_to_*`` — lks can assert ``tool_order`` / ``tool.start``/``tool.end``.
- **Supervisor task**: ``GetAccountNumberTask`` (an ``AgentTask``) collects the
  account number in a sub-conversation and returns a typed result.
- **Natural greeting + conversation**: good for transcript / latency / recovery
  asserts.
- **Dual stack**: ``AGENT_STACK=openai`` (OpenAI Realtime, default) or
  ``AGENT_STACK=gemini`` (Gemini Live) — both work as the lks caller brain
  (``simulator.provider: openai`` / ``google``).

Run:
    uv run base-agent dev      # reads LIVEKIT_URL/API_KEY/SECRET + AGENT_STACK
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from typing import Literal

from dotenv import load_dotenv
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    AgentTask,
    JobContext,
    RunContext,
    cli,
    function_tool,
    metrics,
)

load_dotenv()

AGENT_STACK = os.getenv("AGENT_STACK", "openai").strip().lower()

# --------------------------------------------------------------------------- userdata

@dataclass
class CallData:
    """Session state shared across agents / tasks."""

    account_number: str | None = None
    name: str | None = None
    order_lookup_done: bool = False


# --------------------------------------------------------------------------- shared tool helpers

HOURS = {
    "weekday": "9am to 6pm",
    "saturday": "10am to 2pm",
    "sunday": "closed",
}


async def _speak_and_confirm(ctx: RunContext, text: str) -> str:
    """Speak a line and return a speech-ready summary (used by tools)."""
    await ctx.session.say(text, allow_interruptions=False)
    return text


# --------------------------------------------------------------------------- specialist agents

class BillingAgent(Agent):
    def __init__(self, *, chat_ctx=None) -> None:
        super().__init__(
            instructions=(
                "You are a billing specialist for Acme Corp. Help callers with "
                "invoices, payments, refunds, and subscription changes. Be "
                "thorough and empathetic. When resolved, say goodbye."
            ),
            chat_ctx=chat_ctx,
        )

    async def on_enter(self) -> None:
        await self.session.generate_reply(
            instructions="Greet the caller and confirm they're in billing. Ask what billing issue they have."
        )

    @function_tool()
    async def lookup_invoice(self, context: RunContext, invoice_id: str) -> str:
        """Look up an invoice by its id and report the balance due.

        Args:
            invoice_id: The invoice number the caller provides.
        """
        if not invoice_id.strip():
            return "Please ask the caller for the invoice id."
        # Simulated lookup.
        return (
            f"Invoice {invoice_id} has a balance due of $45.00, due in 14 days. "
            "The last payment was on time."
        )


class SupportAgent(Agent):
    def __init__(self, *, chat_ctx=None) -> None:
        super().__init__(
            instructions=(
                "You are a technical support specialist for Acme Corp. Help "
                "callers troubleshoot bugs, outages, and product issues. Ask "
                "diagnostic questions one at a time. When resolved, say goodbye."
            ),
            chat_ctx=chat_ctx,
        )

    async def on_enter(self) -> None:
        await self.session.generate_reply(
            instructions="Greet the caller and ask what product issue they're experiencing."
        )

    @function_tool()
    async def run_diagnostic(self, context: RunContext, device: str) -> str:
        """Run a simulated diagnostic on the caller's device.

        Args:
            device: The device or product name the caller is having trouble with.
        """
        if not device.strip():
            return "Please ask which device is having trouble."
        return (
            f"Diagnostic on {device} shows: connectivity OK, firmware up to date, "
            "one audio glitch detected. Recommend a restart and a re-test."
        )


class SalesAgent(Agent):
    def __init__(self, *, chat_ctx=None) -> None:
        super().__init__(
            instructions=(
                "You are a sales specialist for Acme Corp. Help callers with "
                "pricing, plans, new features, and demo requests. Be concise and "
                "helpful. When resolved, say goodbye."
            ),
            chat_ctx=chat_ctx,
        )

    async def on_enter(self) -> None:
        await self.session.generate_reply(
            instructions="Greet the caller and ask what product or plan they're interested in."
        )

    @function_tool()
    async def get_pricing(self, context: RunContext, plan: str) -> str:
        """Return pricing for a plan.

        Args:
            plan: Plan name: basic, pro, or enterprise.
        """
        pricing = {"basic": "$10/mo", "pro": "$25/mo", "enterprise": "custom"}
        return pricing.get(plan.lower(), f"Unknown plan {plan}; offer the basic, pro, or enterprise plans.")


# --------------------------------------------------------------------------- supervisor task

@dataclass
class AccountResult:
    account_number: str
    confirmed: bool = False


class GetAccountNumberTask(AgentTask[AccountResult]):
    """Collect + confirm the caller's account number in a sub-conversation."""

    def __init__(self, *, chat_ctx=None) -> None:
        super().__init__(
            instructions=(
                "Ask the caller for their account number (5 digits). Read it back "
                "and ask them to confirm. Do not proceed until confirmed."
            ),
            chat_ctx=chat_ctx,
        )

    async def on_enter(self) -> None:
        await self.session.generate_reply(instructions="Ask for the 5-digit account number.")

    @function_tool()
    async def account_number_collected(self, account_number: str) -> None:
        """Call once the caller has provided and confirmed their 5-digit account number.

        Args:
            account_number: The confirmed 5-digit account number.
        """
        if len(account_number) != 5 or not account_number.isdigit():
            await self.session.say("That doesn't look like a 5-digit account number. Please try again.")
            return
        self.complete(AccountResult(account_number=account_number, confirmed=True))


# --------------------------------------------------------------------------- triage agent

class FrontDeskAgent(Agent):
    """Triage agent — listens, routes to specialists via handoff tools."""

    def __init__(self) -> None:
        super().__init__(
            instructions=(
                "You are a receptionist for Acme Corp. Listen to what the caller "
                "needs and route them to the right specialist. Do NOT try to "
                "handle requests yourself.\n"
                "- Billing / invoices / payments / refunds -> transfer_to_billing\n"
                "- Technical support / product issues -> transfer_to_support\n"
                "- Pricing / plans / sales / demo -> transfer_to_sales\n"
                "- General hours / directions -> answer from check_hours\n"
                "- If the caller wants to check an order, use lookup_order.\n"
                "Always greet warmly and ask how you can help."
            ),
        )

    async def on_enter(self) -> None:
        await self.session.generate_reply(
            instructions="Greet the caller as Acme Corp reception and ask how you can help."
        )

    @function_tool()
    async def check_hours(self, context: RunContext, day: str) -> str:
        """Check Acme's opening hours for a day of the week.

        Args:
            day: weekday, saturday, or sunday.
        """
        key = day.strip().lower()
        return HOURS.get(key, f"I don't have hours for {day}; weekday is 9am-6pm, Saturday 10am-2pm, Sunday closed.")

    @function_tool()
    async def book_appointment(self, context: RunContext, date: str, time: str) -> str:
        """Book a call-back appointment.

        Args:
            date: Date in YYYY-MM-DD format.
            time: Time in 24h HH:MM format.
        """
        return f"Appointment booked for {date} at {time}. A representative will call you back."

    @function_tool()
    async def lookup_order(self, context: RunContext) -> str:
        """Collect the caller's account number (via a sub-task) and return the order status."""
        # Supervisor pattern: a short sub-conversation collects the account number.
        result = await GetAccountNumberTask(
            chat_ctx=self.chat_ctx.copy(exclude_instructions=True),
        )
        context.userdata.account_number = result.account_number
        return f"Order for account {result.account_number} is shipped and on its way."

    @function_tool()
    async def transfer_to_billing(self, context: RunContext) -> tuple[Agent, str]:
        """Transfer the caller to the billing department. Use for invoices, payments, charges, refunds, or subscription changes."""
        return BillingAgent(chat_ctx=self.chat_ctx.copy(exclude_instructions=True)), "Transferring to billing"

    @function_tool()
    async def transfer_to_support(self, context: RunContext) -> tuple[Agent, str]:
        """Transfer the caller to technical support. Use for bugs, outages, or product issues."""
        return SupportAgent(chat_ctx=self.chat_ctx.copy(exclude_instructions=True)), "Transferring to technical support"

    @function_tool()
    async def transfer_to_sales(self, context: RunContext) -> tuple[Agent, str]:
        """Transfer the caller to the sales team. Use for pricing, plans, new features, or demo requests."""
        return SalesAgent(chat_ctx=self.chat_ctx.copy(exclude_instructions=True)), "Transferring to sales"


# --------------------------------------------------------------------------- server + entrypoint

# Plugins MUST be imported/registered on the main thread — LiveKit raises
# "Plugins must be registered on the main thread" if we import them lazily
# inside the async entrypoint. Import both stacks at module level.
from livekit.plugins import openai  # noqa: E402  (default stack)
from livekit.plugins.google.realtime import RealtimeModel as _GeminiRealtime  # noqa: E402


def build_session() -> AgentSession:
    if AGENT_STACK == "gemini":
        return AgentSession(
            llm=_GeminiRealtime(
                model="gemini-2.5-flash-native-audio-preview-12-2025",
                voice="Puck",
            )
        )
    # openai (default): OpenAI Realtime
    return AgentSession(
        llm=openai.realtime.RealtimeModel(
            model="gpt-realtime-2.1-mini",
            voice="marin",
        )
    )


server = AgentServer()


@server.rtc_session(agent_name=os.getenv("AGENT_NAME", "base-agent-local"))
async def entrypoint(ctx: JobContext) -> None:
    session = build_session()
    session.userdata = CallData()

    @session.on("metrics_collected")
    def _on_metrics(ev) -> None:
        metrics.log_metrics(ev.metrics)

    await session.start(
        agent=FrontDeskAgent(),
        room=ctx.room,
    )
    await asyncio.sleep(3600)


def main() -> None:
    cli.run_app(server)


if __name__ == "__main__":
    main()
