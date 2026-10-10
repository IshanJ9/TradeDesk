"""Where PendingOrders live between 'card shown' and 'trader clicked'.

With a database, every change is written through to SQLite and the cards are loaded back at startup, so a restart
loses nothing that is waiting for approval. A reloaded card is byte-for-byte the card the trader saw: its order
hash is recomputed from the same fields, so approving it still has to match exactly, and an expired card is
still refused by the approval checks. Without a database (tests) it is in memory only.
"""

from app.db import Database
from app.schemas import PendingOrder, PendingState

KEEP = 500  # cards loaded back at startup, newest first: enough for any day, small enough to stay fast

class PendingStore:
    def __init__(self, db: Database | None = None, *, user_id: str) -> None:
        self._db = db
        self._user_id = user_id
        self._orders: dict[str, PendingOrder] = {}
        if db is not None:
            rows = db.query("SELECT data FROM pending_cards WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
                            (user_id, KEEP))
            for row in rows:
                order = PendingOrder.model_validate_json(row["data"])
                self._orders[order.id] = order

    def put(self, order: PendingOrder) -> PendingOrder:
        """Insert or replace by id."""
        self._orders[order.id] = order
        if self._db is not None:
            self._db.execute(
                "INSERT INTO pending_cards (id, user_id, state, created_at, data) VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET state = excluded.state, data = excluded.data WHERE user_id = excluded.user_id",
                (order.id, self._user_id, order.state.value, order.created_at.isoformat(), order.model_dump_json(round_trip=True)),
            )
        return order

    def get(self, order_id: str) -> PendingOrder | None:
        return self._orders.get(order_id)

    def awaiting_approval(self) -> list[PendingOrder]:
        """Cards the trader can still act on, oldest first (including ones waiting for their Co-Captain)."""
        return sorted(
            (o for o in self._orders.values()
             if o.state in (PendingState.PENDING, PendingState.AWAITING_CO_APPROVAL)),
            key=lambda o: o.created_at,
        )

    def all(self) -> list[PendingOrder]:
        return list(self._orders.values())
