"""Our own record of the trader's activity, kept day by day.

021 only returns TODAY's orders, so anything the risk report compares against (your usual day, your
past days) has to be saved by us. Writers: the order sync (orders we sent and orders placed in 021's own
app) and the end-of-day snapshot. Readers: the risk engine and the discipline report.

`InMemoryActivityStore` is the reference version used by tests and until a database-backed one is wired in
app/main.py. Money is in paise, like everywhere else.
"""

from datetime import date, datetime, timedelta, timezone
from typing import Literal, Protocol

from app.schemas import Model, Order

IST = timezone(timedelta(hours=5, minutes=30))  # NSE trading days are Indian dates

Source = Literal["app", "external"]  # sent by us, or placed somewhere else (021's own app)


def trading_day(ts: datetime) -> date:
    return ts.astimezone(IST).date()


class OrderRecord(Model):
    order: Order  # the latest state we have seen
    source: Source
    day: date


class DaySummary(Model):
    """One trading day, for "today vs your usual" and "was it worth it"."""

    day: date
    orders: int
    turnover: int  # paise: value of everything filled that day
    pnl: int  # paise: realised + unrealised at the end of the day, before charges
    charges: int  # paise
    risk_score: int | None = None  # 0-100, filled in by the risk engine
    demo: bool = False  # seeded demo history; the UI must label it as such


class ActivityStore(Protocol):
    def record_order(self, order: Order, source: Source) -> None:
        """Insert or update by order id. The first source seen for an id is kept."""

    def orders_on(self, day: date) -> list[OrderRecord]: ...

    def save_day(self, summary: DaySummary) -> None:
        """Insert or replace the summary for that day."""

    def days(self, limit: int = 30) -> list[DaySummary]:
        """Most recent first."""


class InMemoryActivityStore:
    def __init__(self) -> None:
        self._orders: dict[str, OrderRecord] = {}
        self._days: dict[date, DaySummary] = {}

    def record_order(self, order: Order, source: Source) -> None:
        known = self._orders.get(order.order_id)
        self._orders[order.order_id] = OrderRecord(
            order=order, source=known.source if known else source, day=trading_day(order.created_at)
        )

    def orders_on(self, day: date) -> list[OrderRecord]:
        return sorted((r for r in self._orders.values() if r.day == day), key=lambda r: r.order.created_at)

    def save_day(self, summary: DaySummary) -> None:
        self._days[summary.day] = summary

    def days(self, limit: int = 30) -> list[DaySummary]:
        return [self._days[d] for d in sorted(self._days, reverse=True)[:limit]]
