"""Demo controls: make the judged failure cases happen on demand, on the MOCK broker only.

Every route here answers 404 unless DEMO_MODE=true AND the broker is the in-memory mock. They change the
fake market, never the safety rules: the same cards, approval checks and send log run as always. With the
real 021 broker these routes do not exist, so a demo switch can never touch a real account.

    price-jump     move a stock's price after a card was shown (-> the card must be re-quoted, nothing sent)
    timeout-next   the next order call times out, and 021 did or did not receive it (-> never sent twice)
    network        the broker becomes unreachable / reachable again
    poison         add a stock whose NAME carries a prompt-injection attempt (-> treated as plain data)
    losers         add two losing intraday positions (-> "exit all my losing intraday positions")
    anchor         switch 021's Anchor lock on or off (-> new orders refused while it is on)
    external-order an order placed in 021's OWN app, not through TradeDesk (-> shows in the "021 app" tab and counts
                   toward today's discipline numbers)
"""

import uuid
from datetime import timedelta

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field

from app.api_models import ChaosStatus, ChaosStatusEvent, LockUpdateEvent
from app.broker.mock import MockBroker
from app.desk import desk_router
from app.schemas import OrderAction, OrderType, PendingOrder, PendingState, Product, Side, paise

router = desk_router(prefix="/api/demo", tags=["demo"])


class DemoState(BaseModel):
    network_down: bool
    anchor_active: bool
    timeout_armed: bool


class PriceJump(BaseModel):
    symbol: str = Field(min_length=1, max_length=20)
    percent: float = Field(ge=-20, le=20)


class Switch(BaseModel):
    on: bool


class TimeoutNext(BaseModel):
    accepted: bool = Field(description="True: the broker received the order but the reply was lost (the dangerous case)")


def _mock(request: Request) -> MockBroker:
    broker = request.state.ws.broker
    if not request.state.ws.settings.demo_mode or not isinstance(broker, MockBroker):
        raise HTTPException(404, "not found")
    return broker


def _state(b: MockBroker) -> DemoState:
    return DemoState(network_down=b.network_down, anchor_active=b.locks.anchor_active, timeout_armed=b._timeout_place is not None)


@router.get("/status", response_model=DemoState)
async def status(request: Request):
    return _state(_mock(request))


@router.post("/price-jump", response_model=DemoState)
async def price_jump(body: PriceJump, request: Request):
    b = _mock(request)
    key = f"NSE:{body.symbol.upper()}"
    if key not in b._instruments:
        raise HTTPException(404, f"unknown symbol {body.symbol}")
    step = b._instruments[key].tick_size
    ltp = b._prices[key]
    b.set_price(key, max(step, round(ltp * (1 + body.percent / 100) / step) * step))
    return _state(b)


@router.post("/timeout-next", response_model=DemoState)
async def timeout_next(body: TimeoutNext, request: Request):
    b = _mock(request)
    b.timeout_next_place(accepted=body.accepted)
    return _state(b)


@router.post("/network", response_model=DemoState)
async def network(body: Switch, request: Request):
    b = _mock(request)
    b.network_down = body.on
    request.state.ws.hub.publish(ChaosStatusEvent, status=ChaosStatus(network_down=body.on))
    return _state(b)


@router.post("/poison", response_model=DemoState)
async def poison(request: Request):
    b = _mock(request)
    if "NSE:EVILCORP" not in b._instruments:
        b.add_poisoned_instrument()
    return _state(b)


@router.post("/losers", response_model=DemoState)
async def losers(request: Request):
    b = _mock(request)
    for symbol, qty, avg in (("INFY", 10, "1500"), ("ZOMATO", -20, "230")):  # a long and a short, both losing
        b._positions[f"NSE:{symbol}"] = (qty, paise(avg))
        b._position_product[f"NSE:{symbol}"] = Product.MIS
    return _state(b)


@router.post("/anchor", response_model=DemoState)
async def anchor(body: Switch, request: Request):
    b = _mock(request)
    b.locks = b.locks.model_copy(update={
        "anchor_active": body.on,
        "anchor_message": "Anchor is on: you chose to pause new orders in 021's app." if body.on else None,
    })
    request.state.ws.hub.publish(LockUpdateEvent, locks=b.locks)
    return _state(b)


@router.post("/external-order", response_model=DemoState)
async def external_order(request: Request):
    """Places 2 TCS straight on the mock broker, the way 021's own app would: no card, no TradeDesk send log."""
    b = _mock(request)
    key, now = "NSE:TCS", request.state.ws.clock()
    order = PendingOrder(
        id=f"ext-{uuid.uuid4().hex[:8]}", action=OrderAction.PLACE, instrument=b._instruments[key], side=Side.BUY,
        quantity=2, order_type=OrderType.LIMIT, limit_price=b._prices[key], client_order_id=f"ext-{uuid.uuid4().hex[:8]}",
        ref_ltp=b._prices[key], created_at=now, expires_at=now + timedelta(seconds=60), state=PendingState.APPROVED,
    )
    await b.place_order(order)
    return _state(b)
