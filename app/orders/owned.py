"""What the trader owns and could sell, as one list.

021 keeps a delivery (CNC) share you bought TODAY under *positions*, and moves it to *holdings* only later (the
live sandbox returned an empty holdings list with five freshly bought TCS shares sitting in positions as product
CNC). Anything that asks "does the trader own this?" (selling, "sell half", plans, a rule priced from the buy
price) must look at both, or it would refuse to sell shares bought minutes ago.
"""

from app.broker.base import ReadOnlyBroker
from app.schemas import Holding, Position, Product


async def delivery_owned(broker: ReadOnlyBroker) -> list[Holding | Position]:
    """Holdings, plus today's long delivery (CNC) positions. Intraday positions are not 'owned' delivery shares."""
    holdings = await broker.get_holdings()
    todays = [p for p in await broker.get_positions() if p.product is Product.CNC and p.quantity > 0]
    return [*holdings, *todays]


def owned_quantity(owned: list[Holding | Position], instrument_key: str) -> int:
    return sum(v.quantity for v in owned if v.instrument.key == instrument_key)


def owned_average_price(owned: list[Holding | Position], instrument_key: str) -> int | None:
    """Average buy price across everything held in this stock (weighted by quantity), or None if none is held."""
    rows = [v for v in owned if v.instrument.key == instrument_key]
    qty = sum(v.quantity for v in rows)
    return round(sum(v.avg_price * v.quantity for v in rows) / qty) if qty else None
