"""Where PendingOrders live between 'card shown' and 'trader clicked'.

In-memory for now. Step 4 puts the approval service on top; the audit trail and
durable rule storage come with their own steps.
"""

from app.schemas import PendingOrder, PendingState


class PendingStore:
    def __init__(self) -> None:
        self._orders: dict[str, PendingOrder] = {}

    def put(self, order: PendingOrder) -> PendingOrder:
        """Insert or replace by id."""
        self._orders[order.id] = order
        return order

    def get(self, order_id: str) -> PendingOrder | None:
        return self._orders.get(order_id)

    def awaiting_approval(self) -> list[PendingOrder]:
        """Cards the trader can still act on, oldest first."""
        return sorted(
            (o for o in self._orders.values() if o.state is PendingState.PENDING),
            key=lambda o: o.created_at,
        )

    def all(self) -> list[PendingOrder]:
        return list(self._orders.values())
