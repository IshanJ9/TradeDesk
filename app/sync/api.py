"""Read saved activity on refresh without an additional broker login or request."""

from datetime import date

from fastapi import APIRouter, Request

from app.history.store import trading_day
from app.schemas import Model, Order
from app.sync.external import FINAL_EXECUTIONS

router = APIRouter(prefix="/api/activity", tags=["activity"])


class ExternalActivity(Model):
    day: date
    orders: list[Order]
    attribution_pending: bool


@router.get("/external", response_model=ExternalActivity)
async def external_activity(request: Request) -> ExternalActivity:
    state = request.app.state
    day = trading_day(state.clock())
    ours = {row["broker_order_id"] for row in state.db.query(
        "SELECT broker_order_id FROM executions WHERE broker_order_id IS NOT NULL"
    )}
    records = state.history.orders_on(day)
    orders = [record.order for record in records if record.source == "external" and record.order.order_id not in ours]
    unresolved = any(row["status"] not in FINAL_EXECUTIONS for row in state.db.query("SELECT status FROM executions"))
    return ExternalActivity(day=day, orders=sorted(orders, key=lambda order: order.created_at, reverse=True),
                            attribution_pending=unresolved)
