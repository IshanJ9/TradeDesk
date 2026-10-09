"""One shared broker session, durable order facts and external-order notifications."""

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime

from app.api_models import ExternalOrderEvent
from app.broker.base import BrokerAdapter, BrokerTimeout
from app.db import Database
from app.events import EventHub
from app.history.store import ActivityStore, trading_day
from app.schemas import Order, OrderStatus

log = logging.getLogger("tradedesk.sync")
FINAL_ORDERS = {OrderStatus.FILLED, OrderStatus.REJECTED, OrderStatus.CANCELLED}
FINAL_EXECUTIONS = {"SENT", "REJECTED", "NOT_SENT"}


def _signature(order: Order) -> tuple:
    return order.status, order.filled_quantity, order.avg_fill_price


class ExternalOrderSync:
    def __init__(self, broker: BrokerAdapter, db: Database, history: ActivityStore,
                 hub: EventHub, clock: Callable[[], datetime], grace_seconds: float):
        self._broker, self._db, self._history = broker, db, history
        self._hub, self._clock, self._grace = hub, clock, grace_seconds
        self._baseline: dict[str, tuple] | None = None
        self._seen: dict[str, tuple] = {}
        self._lock = asyncio.Lock()

    async def poll(self) -> None:
        # Serialize even manual/racing calls. A new execution can begin during the
        # broker await, so read the execution ledger AFTER fetching the book.
        async with self._lock:
            orders = await self._broker.get_orders()
            executions = self._db.query("SELECT status,broker_order_id,created_at FROM executions")
            ours = {row["broker_order_id"] for row in executions if row["broker_order_id"]}
            now = self._clock()
            defer_unknown = any(
                (row["status"] not in FINAL_EXECUTIONS or not row["broker_order_id"])
                and (now - datetime.fromisoformat(row["created_at"])).total_seconds() < self._grace
                for row in executions
            )
            if self._baseline is None:
                self._baseline = {order.order_id: _signature(order) for order in orders}
            known = {record.order.order_id: record.source
                     for day in {trading_day(order.created_at) for order in orders}
                     for record in self._history.orders_on(day)}
            for order in orders:
                oid = order.order_id
                if oid not in ours and oid not in known and defer_unknown:
                    # The protocol has no UNKNOWN source. Wait before fixing an
                    # attribution that record_order deliberately never overwrites.
                    continue
                source = known.get(oid, "app" if oid in ours else "external")
                self._history.record_order(order, source)
                signature = _signature(order)
                previous = self._seen.get(oid)
                announce = (signature != previous if previous is not None else
                            oid not in self._baseline or order.status not in FINAL_ORDERS
                            or signature != self._baseline[oid])
                if source == "external" and announce:
                    self._hub.publish(ExternalOrderEvent, order=order)
                self._seen[oid] = signature

    async def run(self, interval: float) -> None:
        if interval <= 0:
            raise ValueError("external sync interval must be positive")
        while True:
            try:
                await self.poll()
            except BrokerTimeout:
                pass  # leave baseline/seen intact and try on the next scheduled pass
            except Exception:
                # Do not log raw broker exception text (it may contain outside text or credentials).
                log.warning("External order sync pass failed; retrying on the next poll")
            await asyncio.sleep(interval)


async def run_external_sync(app, interval: float) -> None:
    state = app.state
    sync = ExternalOrderSync(state.broker, state.db, state.history, state.hub, state.clock,
                             state.settings.reconcile_grace_seconds)
    await sync.run(interval)
