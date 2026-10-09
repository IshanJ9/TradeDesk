"""Durable ActivityStore. First attribution wins; subsequent polls refresh order facts."""

from datetime import date

from app.db import Database
from app.history.store import DaySummary, OrderRecord, Source, trading_day
from app.schemas import Order


class SqliteActivityStore:
    def __init__(self, db: Database):
        self._db = db
        db.execute("""CREATE TABLE IF NOT EXISTS activity_orders (
            order_id TEXT PRIMARY KEY, source TEXT NOT NULL CHECK(source IN ('app','external')),
            day TEXT NOT NULL, data TEXT NOT NULL
        )""")
        db.execute("CREATE INDEX IF NOT EXISTS activity_orders_day ON activity_orders(day)")
        db.execute("""CREATE TABLE IF NOT EXISTS activity_days (
            day TEXT PRIMARY KEY, data TEXT NOT NULL
        )""")

    def record_order(self, order: Order, source: Source) -> None:
        record = OrderRecord(order=order, source=source, day=trading_day(order.created_at))
        self._db.execute(
            "INSERT INTO activity_orders(order_id,source,day,data) VALUES (?,?,?,?) "
            "ON CONFLICT(order_id) DO UPDATE SET day=excluded.day, data=excluded.data",
            (order.order_id, record.source, record.day.isoformat(), order.model_dump_json(round_trip=True)),
        )

    def orders_on(self, day: date) -> list[OrderRecord]:
        # Retain the first observation in storage, but never report an order as
        # external when the durable execution ledger proves this app sent it.
        records = [OrderRecord(order=Order.model_validate_json(row["data"]), source=row["source"], day=day)
                   for row in self._db.query(
                       "SELECT CASE WHEN EXISTS (SELECT 1 FROM executions e WHERE e.broker_order_id=a.order_id) "
                       "THEN 'app' ELSE a.source END AS source,a.data "
                       "FROM activity_orders a WHERE a.day=? ORDER BY a.rowid", (day.isoformat(),))]
        # Sort actual instants, not ISO strings (which can have different UTC offsets).
        return sorted(records, key=lambda record: record.order.created_at)

    def save_day(self, summary: DaySummary) -> None:
        self._db.execute(
            "INSERT INTO activity_days(day,data) VALUES (?,?) "
            "ON CONFLICT(day) DO UPDATE SET data=excluded.data",
            (summary.day.isoformat(), summary.model_dump_json(round_trip=True)),
        )

    def days(self, limit: int = 30) -> list[DaySummary]:
        if limit >= 0:
            rows = self._db.query("SELECT data FROM activity_days ORDER BY day DESC LIMIT ?", (limit,))
        else:
            # Preserve the reference store's Python slicing behaviour for negative limits.
            rows = self._db.query("SELECT data FROM activity_days ORDER BY day DESC")[:limit]
        return [DaySummary.model_validate_json(row["data"]) for row in rows]
