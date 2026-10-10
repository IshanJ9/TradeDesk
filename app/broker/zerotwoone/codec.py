"""Translating between our models and 021's REST payloads. Pure functions: no network, easy to test.

Units (the guide's 'number one source of mistakes'): prices are integer paise on both sides; the SIGN of
`qty` is the side (positive buy, negative sell); quantities are units, not lots.
"""

from datetime import datetime, timezone
from typing import Any

from app.broker.base import BrokerError
from app.broker.errors import reason_from_text
from app.broker.zerotwoone.instruments import Listing
from app.schemas import (
    Order,
    OrderStatus,
    OrderType,
    PendingOrder,
    Product,
    RejectionReason,
    Side,
    Validity,
)

PRODUCT_TO_WIRE = {Product.CNC: "CNC", Product.MIS: "INTRADAY", Product.NRML: "NRML"}
PRODUCT_FROM_WIRE = {"CNC": Product.CNC, "INTRADAY": Product.MIS, "NRML": Product.NRML}  # NRML: F&O carried overnight
VALIDITY_TO_WIRE = {Validity.DAY: "Day", Validity.IOC: "IOC"}
VALIDITY_FROM_WIRE = {"Day": Validity.DAY, "IOC": Validity.IOC}


class MalformedResponse(BrokerError):
    """021 answered, but not in a shape we understand. Never treated as success."""


# ---- envelope ---------------------------------------------------------------------------------- #


def unwrap(payload: Any) -> Any:
    """`{data, success, error}` -> data. A plain list (list endpoints) is returned as is."""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict) or "success" not in payload:
        raise MalformedResponse("unexpected response from the broker")
    if payload["success"] is not True:
        raise MalformedResponse(str(payload.get("error") or "the broker reported a failure"))
    return payload.get("data")


# ---- requests ---------------------------------------------------------------------------------- #


def _signed(side: Side, quantity: int) -> int:
    return quantity if side is Side.BUY else -quantity


def place_body(p: PendingOrder, listing: Listing) -> dict:
    """The body for POST /orders. We never send a true market order: a 'market' order carries its
    protection price as a limit, so it cannot fill wildly away from the price the trader saw."""
    price = p.limit_price or p.protection_price
    return {
        "exchange": listing.request_exchange,
        "token": listing.token,
        "qty": _signed(p.side, p.quantity),
        "price": price,
        "book": "SL" if p.order_type is OrderType.STOP_LIMIT else "RL",
        "trigger": p.trigger_price or 0,
        "discQuantity": 0,
        "product": PRODUCT_TO_WIRE[p.product],
        "validity": VALIDITY_TO_WIRE[p.validity],
        "amo": False,
    }


def modify_body(current: Order, listing: Listing, p: PendingOrder) -> dict:
    """PUT /orders/{id} wants the FULL new state, so start from the order as it stands and overlay the change."""
    quantity = p.quantity if p.quantity is not None else current.quantity
    price = p.limit_price if p.limit_price is not None else current.limit_price
    trigger = p.trigger_price if p.trigger_price is not None else current.trigger_price
    return {
        "exchange": listing.request_exchange,
        "token": listing.token,
        "qty": _signed(current.side, quantity),
        "price": price or 0,
        "book": "SL" if current.order_type is OrderType.STOP_LIMIT else "RL",
        "trigger": trigger or 0,
        "discQuantity": 0,
        "product": PRODUCT_TO_WIRE[current.product],
        "validity": VALIDITY_TO_WIRE[current.validity],
        "amo": False,
    }


def cancel_body(current: Order, listing: Listing) -> dict:
    return {"exchange": listing.request_exchange, "token": listing.token, "product": PRODUCT_TO_WIRE[current.product]}


# ---- orders ------------------------------------------------------------------------------------ #


def _when(seconds: Any) -> datetime:
    try:
        return datetime.fromtimestamp(int(seconds), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return datetime.now(timezone.utc)


def status_of(row: dict) -> OrderStatus:
    """021 has no 'partial' status: an order that is part-filled and still open is derived from quantities."""
    wire = row.get("status")
    traded, remaining = abs(int(row.get("qtyTraded") or 0)), abs(int(row.get("qtyRemaining") or 0))
    match wire:
        case "Executed":
            return OrderStatus.FILLED
        case "Rejected":
            return OrderStatus.REJECTED
        case "Cancelled":
            return OrderStatus.CANCELLED
        case "Received" | "Frozen" | "AcceptedForAMO":
            return OrderStatus.PENDING
        case "Placed" | "Pending" | "SentForModification" | "SentForCancellation":
            return OrderStatus.PARTIAL if traded > 0 and remaining > 0 else OrderStatus.OPEN
        case _:
            return OrderStatus.UNKNOWN  # a status we don't know is never shown as success


def order_from_row(
    row: dict,
    listing: Listing,
    *,
    trades: list[dict] | None = None,
    fallback: tuple[Side, int] | None = None,
) -> Order | None:
    """One row of GET /orders -> Order. None if we cannot say what side and size it was.

    The size is `qtyTraded + qtyRemaining`. The side is the sign of `qtyRemaining`; once nothing remains the
    row may not say, so it comes from the order's own trades (their `quantity` is signed), then from what
    we sent if this is our order (`fallback`: a rejected or cancelled row can show zero for both sizes).
    We would rather leave an order out than guess whether it was a buy or a sell."""
    raw_traded, raw_remaining = int(row.get("qtyTraded") or 0), int(row.get("qtyRemaining") or 0)
    traded, remaining = abs(raw_traded), abs(raw_remaining)
    quantity = traded + remaining

    side: Side | None = None
    if raw_remaining:
        side = Side.SELL if raw_remaining < 0 else Side.BUY
    elif trades:
        first = int(trades[0].get("quantity") or 0)
        side = Side.SELL if first < 0 else Side.BUY if first > 0 else None
    elif raw_traded < 0:
        side = Side.SELL
    if (side is None or not quantity) and fallback is not None:
        side, quantity = side or fallback[0], quantity or fallback[1]
    if side is None or not quantity:
        return None

    price, trigger = int(row.get("price") or 0), int(row.get("triggerPrice") or 0)
    is_stop = row.get("book") == "SL"
    status = status_of(row)
    reason = (row.get("reason") or "").strip()
    rejection = (reason_from_text(reason) or RejectionReason.OTHER) if status is OrderStatus.REJECTED else None
    return Order(
        order_id=str(row["orderId"]),
        instrument=listing.instrument,
        side=side,
        quantity=quantity,
        filled_quantity=min(traded, quantity),
        avg_fill_price=average_price(trades or []) if traded else None,
        order_type=OrderType.STOP_LIMIT if is_stop else (OrderType.LIMIT if price else OrderType.MARKET),
        limit_price=price or None,
        trigger_price=(trigger or None) if is_stop else None,
        product=PRODUCT_FROM_WIRE.get(row.get("product"), Product.CNC),
        validity=VALIDITY_FROM_WIRE.get(row.get("validity"), Validity.DAY),
        status=status,
        rejection_reason=rejection,
        rejection_message=reason or None,
        created_at=_when(row.get("time")),
        updated_at=_when(row.get("lastActivity") or row.get("time")),
    )


def average_price(trades: list[dict]) -> int | None:
    """Volume-weighted average fill price (paise, rounded half-up) from an order's trades."""
    qty = sum(abs(int(t.get("quantity") or 0)) for t in trades)
    if not qty:
        return None
    value = sum(abs(int(t["quantity"])) * int(t["price"]) for t in trades)
    return (value * 2 + qty) // (2 * qty)
