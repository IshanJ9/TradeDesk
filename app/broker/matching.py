"""Finding our own order in the broker's order book when we have no id to look it up by.

021's place-order call takes no client order id, so after a timeout ("did it go through?") the only
evidence is the order book itself. We look for an order that matches what we sent, was created after
we started sending, and is not already accounted for by another of our own sends.

The rules are deliberately strict about being sure:
- one match            -> FOUND
- no match             -> NONE (the caller still waits before saying 'never placed')
- two or more matches  -> AMBIGUOUS. Identical orders cannot be told apart, so we do NOT guess; the
                          outcome stays UNKNOWN and the trader is told to look at the order book.
"""

from collections.abc import Collection, Iterable
from datetime import timedelta

from app.schemas import MatchKind, MatchResult, Order, SentOrderSpec

# The broker's clock and ours can differ a little. Anything created this much before we started sending
# can still be ours.
CLOCK_TOLERANCE = timedelta(seconds=5)


def looks_like(order: Order, spec: SentOrderSpec) -> bool:
    return (
        order.instrument.key == spec.instrument_key
        and order.side is spec.side
        and order.quantity == spec.quantity
        and order.limit_price == spec.price
        and order.trigger_price == spec.trigger_price
        and order.product is spec.product
        and order.validity is spec.validity
        and order.created_at >= spec.sent_after - CLOCK_TOLERANCE
    )


def match_sent_order(
    spec: SentOrderSpec,
    orders: Iterable[Order],
    exclude_order_ids: Collection[str] = (),
) -> MatchResult:
    """`exclude_order_ids`: order ids already attributed to one of our other sends."""
    hits = [o for o in orders if o.order_id not in exclude_order_ids and looks_like(o, spec)]
    if not hits:
        return MatchResult(kind=MatchKind.NONE)
    if len(hits) > 1:
        return MatchResult(kind=MatchKind.AMBIGUOUS)
    return MatchResult(kind=MatchKind.FOUND, order=hits[0])
