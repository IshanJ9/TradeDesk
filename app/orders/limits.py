"""Hard limits and lock checks. Plain code; the LLM has no way to override any of it.

Every function raises `OrderBlocked` with a RejectionReason and a plain-English message
that is shown to the trader as-is.
"""

from dataclasses import dataclass

from app.config import Settings
from app.schemas import (
    AccountLocks,
    Instrument,
    OrderAction,
    RejectionReason,
    fmt_rupees,
    paise,
)


class OrderBlocked(Exception):
    def __init__(self, reason: RejectionReason, message: str):
        super().__init__(message)
        self.reason = reason
        self.message = message


@dataclass(frozen=True)
class HardLimits:
    max_quantity: int = 100_000  # 021's published limit
    max_order_value: int = paise(10_000_000)  # Rs 1 crore, in paise (021's published limit)
    allowed_series: frozenset[str] = frozenset({"EQ", "BE"})  # equity; no futures, no indices
    allow_options: bool = True  # buying options, selling ones held, and writing (the builder adds the extra checks)
    allow_futures: bool = True
    max_fo_lots: int = 2  # new futures / written-option exposure per order, in lots; closing is never capped

    @classmethod
    def from_settings(cls, s: Settings) -> "HardLimits":
        return cls(max_quantity=s.max_order_quantity, max_order_value=paise(s.max_order_value_rupees),
                   max_fo_lots=s.max_fo_lots_per_order)


def check_instrument(inst: Instrument, limits: HardLimits) -> None:
    allowed = (
        (inst.is_option and limits.allow_options)
        or (inst.is_future and limits.allow_futures)
        or (not inst.is_derivative and inst.series in limits.allowed_series)
    )
    if not allowed:
        raise OrderBlocked(
            RejectionReason.SEGMENT_NOT_ALLOWED,
            f"{inst.symbol} can't be traded here: only shares, options and futures are supported.",
        )
    if inst.suspended:
        raise OrderBlocked(RejectionReason.SUSPENDED, f"{inst.symbol} is suspended and can't be traded right now.")


# app/risk words every warning about the trader's own limits this way ("You set 5 orders a day; ...").
_OWN_LIMIT_PREFIXES = ("You set", "You switched")
HIGH_RISK_PAUSED = (
    "You are past a limit you set for yourself, so new futures and short options are paused. "
    "Closing a position you already hold is still allowed."
)


def crosses_own_limit(warnings) -> bool:
    return any(w.startswith(_OWN_LIMIT_PREFIXES) for w in warnings)


def check_size(quantity: int, price: int, limits: HardLimits) -> None:
    if quantity > limits.max_quantity:
        raise OrderBlocked(
            RejectionReason.QUANTITY_LIMIT,
            f"{quantity:,} units is above the limit of {limits.max_quantity:,} units per order.",
        )
    if quantity * price > limits.max_order_value:
        raise OrderBlocked(
            RejectionReason.VALUE_LIMIT,
            f"An order of {fmt_rupees(quantity * price)} is above the limit of "
            f"{fmt_rupees(limits.max_order_value)} per order.",
        )


def check_price(inst: Instrument, price: int) -> None:
    """A limit price must sit on the tick grid and inside the day's price band."""
    if price % inst.tick_size:
        raise OrderBlocked(
            RejectionReason.INVALID_PRICE,
            f"{fmt_rupees(price)} isn't a valid price for {inst.symbol}; prices move in steps of "
            f"{fmt_rupees(inst.tick_size)}.",
        )
    low, high = inst.price_band_low, inst.price_band_high
    if (low is not None and price < low) or (high is not None and price > high):
        raise OrderBlocked(
            RejectionReason.PRICE_BAND,
            f"{fmt_rupees(price)} is outside today's price band for {inst.symbol} "
            f"({fmt_rupees(low or 0)} to {fmt_rupees(high or 0)}).",
        )


def check_locks(locks: AccountLocks, action: OrderAction, order_value: int | None = None) -> None:
    """Anchor and Co-Captain block new exposure. Cancelling an open order is always allowed.

    Whether sells/exits should pass while Anchor is on is an open question for 021; until
    answered, a new order of any side is blocked and the trader is told why.
    """
    if action is OrderAction.CANCEL:
        return
    if locks.anchor_active:
        note = f" Your note to yourself: “{locks.anchor_message}”" if locks.anchor_message else ""
        raise OrderBlocked(
            RejectionReason.ANCHOR_ACTIVE,
            f"Anchor is on, so new orders are paused. You can still view your account and cancel orders.{note}",
        )
    if locks.co_captain_locked:
        note = f" Message: “{locks.co_captain_message}”" if locks.co_captain_message else ""
        raise OrderBlocked(
            RejectionReason.CO_APPROVAL_REQUIRED,
            f"Your Co-Captain has locked this account, so new orders are paused.{note}",
        )
    if locks.co_approval_limit is not None and order_value is not None and order_value > locks.co_approval_limit:
        raise OrderBlocked(
            RejectionReason.CO_APPROVAL_REQUIRED,
            f"Orders above {fmt_rupees(locks.co_approval_limit)} need your Co-Captain's approval, "
            "which isn't available yet.",
        )
