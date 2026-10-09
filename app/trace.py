"""Live "what is the assistant doing" events for the activity panel.

Each chat message is one run. Every node, tool call and guard reports start and end (or blocked / error)
as a TraceEvent on the websocket. This is display only: nothing here decides anything.

    tracer = Tracer(hub)
    with tracer.step("router"):
        ...
    tracer.emit("output_guard", "guard", "blocked", "removed an unsupported number")
"""

import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Literal

from app.api_models import TraceEvent
from app.events import EventHub

Kind = Literal["node", "tool", "guard"]
Status = Literal["start", "end", "blocked", "error"]

DETAIL_LIMIT = 160


def _short(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= DETAIL_LIMIT else text[: DETAIL_LIMIT - 1] + "…"


class Tracer:
    def __init__(self, hub: EventHub, run_id: str | None = None):
        self._hub = hub
        self.run_id = run_id or uuid.uuid4().hex[:12]

    def emit(self, node: str, kind: Kind, status: Status, detail: str = "", ms: int | None = None) -> None:
        self._hub.publish(TraceEvent, run_id=self.run_id, node=node, kind=kind, status=status, detail=_short(detail), ms=ms)

    @contextmanager
    def step(self, node: str, kind: Kind = "node", detail: str = "") -> Iterator[None]:
        """Emits start, then end; or error (and re-raises) if the block fails. Works inside async code."""
        started = time.perf_counter()
        self.emit(node, kind, "start", detail)
        try:
            yield
        except Exception as exc:
            self.emit(node, kind, "error", type(exc).__name__, _elapsed(started))
            raise
        self.emit(node, kind, "end", "", _elapsed(started))


def _elapsed(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)
