"""Keeps the assistant working when the model provider is down.

If the model (Bedrock) is unavailable, the turn is answered by the built-in keyword stand-in instead of failing
with "assistant unavailable". For the next `cooldown` seconds the stand-in is used straight away, so a
provider outage doesn't make every message wait for a timeout first.

Nothing about safety changes: the stand-in makes the same tool calls, and every guard, card and approval check
runs exactly as with the model. The trader is told when the stand-in answered (`take_notice`).
"""

import logging
import time
from collections.abc import Callable

from app.llm.types import LLMClient, LLMTurn, LLMUnavailable, Message, ToolSpec

log = logging.getLogger("tradedesk.llm")

NOTICE = "The AI model is unavailable right now, so the built-in keyword reader answered. All the same checks applied."


class FallbackLLM:
    def __init__(self, primary: LLMClient, fallback: LLMClient, cooldown: float = 60.0, clock: Callable[[], float] = time.monotonic):
        self._primary, self._fallback = primary, fallback
        self._cooldown, self._clock = cooldown, clock
        self._skip_until = 0.0
        self._used = False

    async def complete(self, *, system: str, messages: list[Message], tools: list[ToolSpec]) -> LLMTurn:
        if self._clock() < self._skip_until:
            self._used = True
            return await self._fallback.complete(system=system, messages=messages, tools=tools)
        try:
            return await self._primary.complete(system=system, messages=messages, tools=tools)
        except LLMUnavailable as exc:
            log.warning("model unavailable, using the keyword stand-in: %s", exc)
            self._skip_until = self._clock() + self._cooldown
            self._used = True
            return await self._fallback.complete(system=system, messages=messages, tools=tools)

    def take_notice(self) -> str | None:
        """The notice for the trader if the stand-in answered since the last call, else None."""
        used, self._used = self._used, False
        return NOTICE if used else None
