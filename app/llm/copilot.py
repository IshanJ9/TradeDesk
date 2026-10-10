"""Runs one chat turn: the model asks for tools, code runs them, code checks what the model says.

What the trader reads is shaped by code, not just by the model:
- Order cards, and the sentence describing them, are written by code from the card itself.
- Any other answer is checked: every number must come from a tool result or the trader's own
  message, it must not claim an order was placed, and it must not give advice. Otherwise it is
  replaced with a plain rendering of the tool results.
- Text from outside the app that reads like instructions is withheld from the model, shown to
  the trader as a notice, and logged.
"""

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import datetime

from app.api_models import Card, ChatReply, NoticeCard
from app.audit import AuditLog
from app.broker.base import ReadOnlyBroker
from app.llm.injection import overrides_rules
from app.llm.grounding import claims_execution, gives_advice, plain_text, ungrounded_numbers
from app.llm.injection import scan
from app.llm.prompt import build_system_prompt
from app.llm.rules import HELP
from app.llm.tools import Tool, ToolContext
from app.llm.types import LLMClient, Message, ToolCall, ToolResult
from app.orders.cards import CardService
from app.plans.service import PlanAssistant
from app.rules.service import RuleService
from app.schemas import AuditKind

log = logging.getLogger("tradedesk.copilot")

MAX_STEPS = 6
HISTORY_LIMIT = 12  # messages kept between turns

NOT_PLACED = "I haven't placed anything. I can only prepare an order card for you to approve."
NO_ADVICE = "I can't give advice or predictions. I can show you facts from your account: your holdings, P&L, orders and prices."
NOT_GROUNDED = "I can only share numbers that come from your account data. Try asking about your holdings, P&L, orders or a stock's price."
OVERRIDE_REFUSAL = "I can't ignore my rules or act outside them. I can only prepare an order card for you to approve, and nothing is sent until you click Approve. Tell me which stock you mean and what you'd like to do."
GAVE_UP = "I couldn't finish that. Please try rephrasing, or ask for one thing at a time."


class Copilot:
    def __init__(
        self,
        llm: LLMClient,
        tools: dict[str, Tool],
        broker: ReadOnlyBroker,
        cards: CardService,
        rules: RuleService,
        plans: PlanAssistant,
        audit: AuditLog,
        clock: Callable[[], datetime],
    ):
        self._llm = llm
        self._tools = tools
        self._broker = broker
        self._cards = cards
        self._rules = rules
        self._plans = plans
        self._audit = audit
        self._clock = clock
        self._history: list[Message] = []
        self._lock = asyncio.Lock()

    async def handle(self, message: str, via_voice: bool = False) -> ChatReply:
        async with self._lock:  # one conversation; turns do not interleave
            return await self._turn(message, via_voice)

    # ------------------------------------------------------------------ #

    async def _turn(self, message: str, via_voice: bool = False) -> ChatReply:
        self._audit.record(AuditKind.USER_MESSAGE, "user", message, data={"message": message, "via_voice": via_voice})
        refusal = self._refuse_override(message)
        if refusal:
            return refusal
        ctx = self._context(message, via_voice)
        messages = [*self._history, Message("user", message)]
        specs = [t.spec for t in self._tools.values()]
        system = build_system_prompt(self._clock())

        final = ""
        for _ in range(MAX_STEPS):
            turn = await self._llm.complete(system=system, messages=messages, tools=specs)
            if not turn.tool_calls:
                final = turn.text
                break
            messages.append(Message("assistant", turn.text, tool_calls=turn.tool_calls))
            results = [await self._run_tool(ctx, call) for call in turn.tool_calls]
            messages.append(Message("tool", tool_results=results))
        else:
            final = GAVE_UP

        text = self._shape_text(ctx, final, message)
        return self._finish(ctx, message, text)

    # ---- steps shared with the graph orchestrator (app/agent/graph.py) ---- #

    def _refuse_override(self, message: str) -> ChatReply | None:
        override = overrides_rules(message)
        if not override:
            return None
        # answered by code: the model never sees it, and it is not kept in the conversation
        self._audit.record(
            AuditKind.INJECTION_BLOCKED, "system", "A message tried to change the assistant's rules; refused in code",
            data={"matched": list(override), "message": message[:300]},
        )
        return ChatReply(
            text=OVERRIDE_REFUSAL,
            cards=[NoticeCard(level="warning", message="That message looked like an attempt to change my rules, so I did not act on it.")],
        )

    def _context(self, message: str, via_voice: bool = False) -> ToolContext:
        recent_user_texts = [m.text for m in self._history if m.role == "user"][-6:]
        return ToolContext(
            broker=self._broker, cards=self._cards, rules=self._rules, plans=self._plans, clock=self._clock,
            user_texts=[*recent_user_texts, message], via_voice=via_voice,
        )

    def _finish(self, ctx: ToolContext, message: str, text: str) -> ChatReply:
        cards: list[Card] = list(ctx.reply_cards)
        cards += self._injection_notices(ctx)
        notice = getattr(self._llm, "take_notice", lambda: None)()  # app/llm/fallback.py: say when the stand-in answered
        if notice:
            cards.append(NoticeCard(level="info", message=notice))
        self._remember(message, text, cards)
        return ChatReply(text=text, cards=cards)

    async def _run_tool(self, ctx: ToolContext, call: ToolCall) -> ToolResult:
        tool = self._tools.get(call.name)
        if tool is None:
            return ToolResult(call.id, call.name, {"status": "error", "message": f"unknown tool {call.name}"})
        if not tool.read_only:
            self._audit.record(
                AuditKind.LLM_INTENT, "llm", f"{call.name}", data={"tool": call.name, "input": call.input}
            )
        try:
            output = await tool.run(ctx, dict(call.input))
        except (KeyError, ValueError, TypeError) as exc:  # a malformed call from the model; let it correct itself
            log.warning("tool %s failed: %r", call.name, exc)
            output = {"status": "error", "message": "that request could not be completed; check the arguments"}
        output = json.loads(json.dumps(output, default=str))
        if tool.read_only:
            ctx.read_results.append((call.name, output))
        return ToolResult(call.id, call.name, output)

    # ------------------------------------------------------------------ #

    def _fallback(self, ctx: ToolContext) -> str:
        lines = [self._tools[name].render(out) for name, out in ctx.read_results]
        return "\n".join(dict.fromkeys(line for line in lines if line))

    def _shape_text(self, ctx: ToolContext, final: str, user_text: str) -> str:
        return self._shape(ctx, final, user_text)[0]

    def _shape(self, ctx: ToolContext, final: str, user_text: str) -> tuple[str, str | None]:
        """The reply the trader reads, and which check replaced the model's words (None if none did)."""
        if ctx.proposal_texts:  # an order card was involved: the words come from the card, not the model
            return "\n".join(dict.fromkeys(ctx.proposal_texts)), None

        final = plain_text(final)  # the chat shows plain text: no markdown
        fallback = self._fallback(ctx)
        if claims_execution(final):
            self._audit.record(AuditKind.INJECTION_BLOCKED, "system", "Answer claimed an order was placed; replaced", data={"answer": final})
            return NOT_PLACED, "the answer claimed an order was placed"
        if gives_advice(final):
            self._audit.record(AuditKind.LIMIT_BLOCKED, "system", "Answer contained advice or a prediction; replaced", data={"answer": final})
            return NO_ADVICE, "the answer contained advice or a prediction"
        bad = ungrounded_numbers(final, [out for _, out in ctx.read_results], user_text)
        if bad:
            self._audit.record(
                AuditKind.LIMIT_BLOCKED,
                "system",
                f"Answer contained numbers not found in the account data ({', '.join(bad[:5])}); replaced",
                data={"answer": final, "ungrounded": bad},
            )
            return fallback or NOT_GROUNDED, f"numbers not in your account data ({', '.join(bad[:3])})"
        if not final.strip():
            return fallback or HELP, None
        return final, None

    def _injection_notices(self, ctx: ToolContext) -> list[NoticeCard]:
        notices = []
        for f in ctx.findings:
            self._audit.record(
                AuditKind.INJECTION_BLOCKED,
                "system",
                f"Suspicious text in {f.source} was treated as data and withheld from the assistant",
                data={"source": f.source, "text": f.text[:300], "matched": list(f.matched)},
            )
            quoted = f.text if len(f.text) <= 140 else f.text[:137] + "..."
            notices.append(
                NoticeCard(
                    level="warning",
                    message=(
                        f"Blocked: text in {f.source} looks like instructions, so it was treated as plain data "
                        f"and ignored. It said: “{quoted}”"
                    ),
                )
            )
        return notices

    def _remember(self, user_text: str, reply: str, cards: list[Card]) -> None:
        kinds = ", ".join(c.type for c in cards)
        note = f" [cards shown: {kinds}]" if kinds else ""
        # the reply can contain broker-supplied names; never feed flagged text back to the model
        safe_reply = reply if not scan(reply) else "[reply withheld from history: contained text flagged as instructions]"
        self._history += [Message("user", user_text), Message("assistant", safe_reply + note)]
        self._history = self._history[-HISTORY_LIMIT:]
