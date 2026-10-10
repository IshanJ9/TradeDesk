"""Live order updates: a broker that has an orders socket wakes these loops, which then read REST.

The socket only says "something happened to an order"; the order book from GET /orders decides what. That keeps
one source of truth (021's guide says to treat REST as the truth and websockets as live updates) and means a
lost or garbled frame can never put a wrong status on screen: the next read, or the regular poll, corrects it.
"""

import asyncio
import logging

from app.api_models import OrderUpdateEvent
from app.broker.base import BrokerTimeout

log = logging.getLogger("tradedesk.order_events")


class OrderWake:
    """Fan-out of 'an order changed' signals; each consumer gets its own event so none can swallow another's."""

    def __init__(self):
        self._events: list[asyncio.Event] = []

    def subscribe(self) -> asyncio.Event:
        event = asyncio.Event()
        self._events.append(event)
        return event

    def notify(self, *_args) -> None:
        for event in self._events:
            event.set()


async def wait_or_wake(interval: float, wake: asyncio.Event | None) -> None:
    """Sleep for `interval`, or less if the orders socket reports something."""
    if wake is None:
        await asyncio.sleep(interval)
        return
    try:
        await asyncio.wait_for(wake.wait(), interval)
    except asyncio.TimeoutError:
        pass
    wake.clear()


class OrderPublisher:
    """Publishes an OrderUpdateEvent for each order whose status, fill, size or price changed since last time.
    Shared by the tick bridge and the socket watcher, so the screen never gets the same change twice."""

    def __init__(self, broker, hub):
        self._broker, self._hub = broker, hub
        self._seen: dict[str, tuple] | None = None
        self._lock = asyncio.Lock()

    async def publish_changes(self) -> None:
        async with self._lock:
            orders = await self._broker.get_orders()
            sigs = {o.order_id: (o.status, o.filled_quantity, o.quantity, o.limit_price) for o in orders}
            if self._seen is not None:  # the first read only records where we start
                for order in orders:
                    if self._seen.get(order.order_id) != sigs[order.order_id]:
                        self._hub.publish(OrderUpdateEvent, order=order)
            self._seen = sigs


async def run_order_watcher(app, wake: asyncio.Event, settle: float = 0.25) -> None:
    """On each socket event: resolve any send still waiting for an answer, and push changed orders to the screen."""
    state = app.state
    while True:
        await wake.wait()
        await asyncio.sleep(settle)  # one fill often comes as several frames: read once after they land
        wake.clear()
        try:
            await state.executor.reconcile()
            await state.order_publisher.publish_changes()
        except BrokerTimeout:
            pass  # the regular reconcile and poll loops try again
        except Exception:
            log.warning("order watcher pass failed; the regular poll will catch up")
