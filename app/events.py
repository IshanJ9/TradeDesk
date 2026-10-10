"""Fan-out of server events to WebSocket clients, one stream per user.

An event is published for one user and delivered only to that user's sockets (a user with two tabs gets both). Each
user's events get their own increasing `seq`; a client that sees a gap refetches a snapshot. A client that cannot
keep up is dropped (its queue gets a `None` sentinel) rather than allowed to grow memory without bound; on
reconnect it gets a fresh snapshot.

Services never choose who receives an event: they are handed a `UserHub` that is already bound to one user, so
"publish to the wrong person" is not something they can express.
"""

import asyncio
from typing import TypeVar

from app.api_models import _Event

E = TypeVar("E", bound=_Event)

QUEUE_LIMIT = 1000


class EventHub:
    def __init__(self, queue_limit: int = QUEUE_LIMIT) -> None:
        self._queue_limit = queue_limit
        self._seq: dict[str, int] = {}
        self._subs: dict[str, set[asyncio.Queue[_Event | None]]] = {}

    def for_user(self, user_id: str) -> "UserHub":
        return UserHub(self, user_id)

    def seq(self, user_id: str) -> int:
        return self._seq.get(user_id, 0)

    def subscribe(self, user_id: str) -> asyncio.Queue[_Event | None]:
        queue: asyncio.Queue[_Event | None] = asyncio.Queue(self._queue_limit)
        self._subs.setdefault(user_id, set()).add(queue)
        return queue

    def unsubscribe(self, user_id: str, queue: asyncio.Queue[_Event | None]) -> None:
        subs = self._subs.get(user_id)
        if subs is not None:
            subs.discard(queue)
            if not subs:
                del self._subs[user_id]

    def subscriber_count(self, user_id: str) -> int:
        return len(self._subs.get(user_id, ()))

    def publish(self, user_id: str, event_cls: type[E], **fields) -> E:
        seq = self._seq[user_id] = self._seq.get(user_id, 0) + 1
        event = event_cls(seq=seq, user_id=user_id, **fields)
        for queue in list(self._subs.get(user_id, ())):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                self._drop_slow(user_id, queue)
        return event

    def _drop_slow(self, user_id: str, queue: asyncio.Queue[_Event | None]) -> None:
        self.unsubscribe(user_id, queue)
        while not queue.empty():
            queue.get_nowait()
        queue.put_nowait(None)  # tells the websocket handler to close the connection


class UserHub:
    """The hub as one user's code sees it: same calls as before accounts existed, but bound to a single user."""

    def __init__(self, hub: EventHub, user_id: str) -> None:
        self._hub, self.user_id = hub, user_id

    @property
    def seq(self) -> int:
        return self._hub.seq(self.user_id)

    def subscribe(self) -> asyncio.Queue[_Event | None]:
        return self._hub.subscribe(self.user_id)

    def unsubscribe(self, queue: asyncio.Queue[_Event | None]) -> None:
        self._hub.unsubscribe(self.user_id, queue)

    def publish(self, event_cls: type[E], **fields) -> E:
        return self._hub.publish(self.user_id, event_cls, **fields)
