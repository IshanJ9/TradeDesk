"""Charges for one equity order, from 021's published pricing page (https://021.trade/pricing).

Rates below are 021's Cash/Equity rows. Two things to confirm against a real fill:
- GST: 021's note 2 says 18% on brokerage, stamp duty, exchange charges and investor
  protection fund charges (not STT, not SEBI fee). We follow that note literally.
- The DP charge (Rs 15.50 per ISIN per sell instruction) is treated as GST-able. 021's note 6
  says GST applies to all charges listed; note 2 is narrower. We overestimate slightly.
"""

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal

from app.schemas import Charges, Exchange, Product, Side

D = Decimal


@dataclass(frozen=True)
class ProductRates:
    brokerage_pct: Decimal
    brokerage_cap: int  # paise, per executed order ("whichever is lower")
    stt_buy_pct: Decimal
    stt_sell_pct: Decimal
    stamp_buy_pct: Decimal


RATES: dict[Product, ProductRates] = {
    Product.CNC: ProductRates(D("0.0003"), 2000, D("0.001"), D("0.001"), D("0.00015")),
    Product.MIS: ProductRates(D("0.0003"), 2000, D("0"), D("0.00025"), D("0.00003")),
}
EXCHANGE_TXN_PCT = {Exchange.NSE: D("0.0000297"), Exchange.BSE: D("0.0000375")}
SEBI_PCT = D("0.000001")
IPFT_PCT = D("0.000001")
GST_PCT = D("0.18")
DP_CHARGE = 1550  # paise; delivery sells only


def _r(x: Decimal) -> int:
    return int(x.quantize(D("1"), rounding=ROUND_HALF_UP))


def _leg(exchange: Exchange, side: Side, product: Product, quantity: int, price: int) -> Charges:
    rates = RATES[product]
    value = D(price * quantity)
    brokerage = min(_r(value * rates.brokerage_pct), rates.brokerage_cap)
    stt = _r(value * (rates.stt_buy_pct if side is Side.BUY else rates.stt_sell_pct))
    exchange_txn = _r(value * EXCHANGE_TXN_PCT[exchange])
    sebi = _r(value * SEBI_PCT)
    ipft = _r(value * IPFT_PCT)
    stamp = _r(value * rates.stamp_buy_pct) if side is Side.BUY else 0
    dp = DP_CHARGE if (product is Product.CNC and side is Side.SELL) else 0
    gst = _r(GST_PCT * D(brokerage + stamp + exchange_txn + ipft + dp))
    return Charges(
        brokerage=brokerage,
        stt=stt,
        exchange_txn=exchange_txn,
        sebi_fee=sebi,
        stamp_duty=stamp,
        gst=gst,
        clearing=0,
        ipft=ipft,
        dp_charge=dp,
    )


def compute_charges(*, exchange: Exchange, side: Side, product: Product, quantity: int, price: int) -> Charges:
    """Charges for this order's leg, plus the round-trip break-even price per share.

    Break-even assumes the opposite leg trades at the same price: a BUY must rise by the
    combined charges per share (rounded up) to break even; a SELL must be bought back that
    much lower (rounded down, never below one paisa).
    """
    this_leg = _leg(exchange, side, product, quantity, price)
    other = _leg(exchange, Side.SELL if side is Side.BUY else Side.BUY, product, quantity, price)
    per_share = int((D(this_leg.total + other.total) / D(quantity)).to_integral_value(rounding=ROUND_CEILING))
    break_even = price + per_share if side is Side.BUY else max(price - per_share, 1)
    return this_leg.model_copy(update={"break_even_price": break_even})


def estimated_total(side: Side, quantity: int, price: int, charges: Charges) -> int:
    """Buy: cost plus charges. Sell: proceeds minus charges."""
    value = price * quantity
    return value + charges.total if side is Side.BUY else value - charges.total
