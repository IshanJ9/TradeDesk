"""Fan-out of server events to WebSocket clients.

Every event gets a global, increasing `seq`. A client that sees a gap refetches a snapshot.
A client that cannot keep up is dropped (its queue gets a `None` sentinel) rather than
allowed to grow memory without bound; on reconnect it gets a fresh snapshot.
"""

import asyncio
from typing import TypeVar

from app.api_models import _Event

E = TypeVar("E", bound=_Event)

QUEUE_LIMIT = 1000


class EventHub:
    def __init__(self, queue_limit: int = QUEUE_LIMIT) -> None:
        self.seq = 0
        self._queue_limit = queue_limit
        self._subs: set[asyncio.Queue[_Event | None]] = set()

    def subscribe(self) -> asyncio.Queue[_Event | None]:
        queue: asyncio.Queue[_Event | None] = asyncio.Queue(self._queue_limit)
        self._subs.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[_Event | None]) -> None:
        self._subs.discard(queue)

    def publish(self, event_cls: type[E], **fields) -> E:
        self.seq += 1
        event = event_cls(seq=self.seq, **fields)
        for queue in list(self._subs):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                self._drop_slow(queue)
        return event

    def _drop_slow(self, queue: asyncio.Queue[_Event | None]) -> None:
        self._subs.discard(queue)
        while not queue.empty():
            queue.get_nowait()
        queue.put_nowait(None)  # tells the websocket handler to close the connection
