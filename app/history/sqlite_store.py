"""Durable ActivityStore. First attribution wins; subsequent polls refresh order facts."""

from datetime import date
from typing import Literal

from app.db import Database
from app.history.store import DaySummary, OrderRecord, Source, trading_day
from app.schemas import Order


class SqliteActivityStore:
    def __init__(self, db: Database, *, user_id: str,
                 recording_source: Literal["unknown", "mock", "zerotwoone"] = "unknown"):
        self._db = db
        self._user_id = user_id
        self.recording_source = recording_source

    def record_order(self, order: Order, source: Source) -> None:
        record = OrderRecord(order=order, source=source, day=trading_day(order.created_at))
        self._db.execute(
            "INSERT INTO activity_orders(user_id,order_id,source,day,data,recording_source) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(user_id,order_id) DO UPDATE SET day=excluded.day, data=excluded.data, recording_source=excluded.recording_source",
            (self._user_id, order.order_id, record.source, record.day.isoformat(), order.model_dump_json(round_trip=True), self.recording_source),
        )

    def orders_on(self, day: date) -> list[OrderRecord]:
        # Retain the first observation in storage, but never report an order as
        # external when the durable execution ledger proves this app sent it.
        records = [OrderRecord(order=Order.model_validate_json(row["data"]), source=row["source"], day=day,
                               recording_source=row["recording_source"])
                   for row in self._db.query(
                       "SELECT CASE WHEN EXISTS (SELECT 1 FROM executions e WHERE e.user_id=a.user_id AND e.broker_order_id=a.order_id) "
                       "THEN 'app' ELSE a.source END AS source,a.data,a.recording_source "
                       "FROM activity_orders a WHERE a.user_id=? AND a.day=? ORDER BY a.rowid", (self._user_id, day.isoformat()))]
        # Sort actual instants, not ISO strings (which can have different UTC offsets).
        return sorted(records, key=lambda record: record.order.created_at)

    def save_day(self, summary: DaySummary) -> None:
        self._db.execute(
            "INSERT INTO activity_days(user_id,day,data) VALUES (?,?,?) "
            "ON CONFLICT(user_id,day) DO UPDATE SET data=excluded.data",
            (self._user_id, summary.day.isoformat(), summary.model_dump_json(round_trip=True)),
        )

    def days(self, limit: int = 30) -> list[DaySummary]:
        if limit >= 0:
            rows = self._db.query("SELECT data FROM activity_days WHERE user_id=? ORDER BY day DESC LIMIT ?", (self._user_id, limit))
        else:
            # Preserve the reference store's Python slicing behaviour for negative limits.
            rows = self._db.query("SELECT data FROM activity_days WHERE user_id=? ORDER BY day DESC", (self._user_id,))[:limit]
        return [DaySummary.model_validate_json(row["data"]) for row in rows]
