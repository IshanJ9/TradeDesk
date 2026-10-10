"""Actor-scoped notifications. No portfolio snapshots or approval commands."""
import asyncio


class ReviewHub:
    def __init__(self):
        self._queues: dict[str, set[asyncio.Queue]] = {}

    def subscribe(self, actor_id: str):
        queue = asyncio.Queue(maxsize=100)
        self._queues.setdefault(actor_id, set()).add(queue)
        return queue

    def unsubscribe(self, actor_id, queue):
        self._queues.get(actor_id, set()).discard(queue)

    def publish(self, actor_id: str, action: str, card_id: str | None = None):
        for queue in list(self._queues.get(actor_id, set())):
            try:
                queue.put_nowait(dict(type="cocaptain_update", action=action, card_id=card_id))
            except asyncio.QueueFull:
                self.unsubscribe(actor_id, queue)
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(None)


class UserNotifier:
    """Delivers the same notifications as ReviewHub, but over the app's per-user live feed (the /ws every desk already
    has open), so a Co-Captain's desk hears about a waiting card at once instead of on its next poll."""

    def __init__(self, events):
        self._events = events

    def publish(self, actor_id: str, action: str, card_id: str | None = None):
        from app.api_models import CoCaptainUpdateEvent

        self._events.publish(actor_id, CoCaptainUpdateEvent, action=action, card_id=card_id)
