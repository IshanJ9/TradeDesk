"""Turns an `OrderIntent` (the only order-shaped thing the LLM can emit) into an exact,
priced `PendingOrder`. All the money maths and every rule lives here, in plain code.

The builder never sends anything. It either returns a card for the trader to approve,
raises `NeedsClarification` (ambiguous or unknown instrument: ask, never guess), or raises
`OrderBlocked` (a hard limit or lock: tell the trader why).
"""

import re
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from fractions import Fraction

from app.broker.base import BrokerAdapter
from app.config import Settings
from app.orders.charges import compute_charges, estimated_total
from app.orders.owned import delivery_owned, owned_quantity
from app.orders.limits import (
    HardLimits,
    OrderBlocked,
    check_instrument,
    check_locks,
    check_price,
    check_size,
)
from app.schemas import (
    Instrument,
    OrderAction,
    OrderIntent,
    OrderStatus,
    OrderType,
    PendingOrder,
    Product,
    RejectionReason,
    ResolutionResult,
    Side,
    fmt_rupees,
)

FAR_FROM_LTP_PCT = 5.0  # warn when a limit price is this far from the current price

# Order words that a model sometimes leaves inside the stock name ("itc at market", "hdfc bank at 1450.50").
_ORDER_TAIL = re.compile(
    r"\s+(?:at|for|limit|market|mkt|cmp|price|intraday|delivery|rs\.?|inr)\b.*$|\s+[@₹].*$|\s+₹?\d[\d,.]*\s*$",
    re.IGNORECASE,
)


def strip_order_words(ref: str) -> str:
    """'itc at market' -> 'itc'. Only ever removes order wording after the name, never part of a name."""
    return _ORDER_TAIL.sub("", ref.strip()).strip()


class NeedsClarification(Exception):
    """The instrument is ambiguous or unknown. The trader must say which one."""

    def __init__(self, resolution: ResolutionResult):
        super().__init__(f"{resolution.status}: {resolution.query}")
        self.resolution = resolution


def _default_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


class OrderBuilder:
    def __init__(
        self,
        broker: BrokerAdapter,
        settings: Settings,
        clock: Callable[[], datetime],
        limits: HardLimits | None = None,
        new_id: Callable[[str], str] = _default_id,
    ):
        self._broker = broker
        self._settings = settings
        self._clock = clock
        self.limits = limits or HardLimits.from_settings(settings)
        self._new_id = new_id

    # ---- instrument resolution ------------------------------------------------ #

    async def resolve(self, ref: str) -> ResolutionResult:
        """Words -> exactly one instrument, or a list of candidates to ask about."""
        query = ref.strip()
        hits = await self._broker.search_instruments(query, limit=10) if query else []
        if not hits and query:  # a model may have left "at market" / "at 1450" in the name: try without it
            trimmed = strip_order_words(query)
            if trimmed and trimmed != query:
                query = trimmed
                hits = await self._broker.search_instruments(query, limit=10)
        if not hits:
            return ResolutionResult(status="not_found", query=query)
        exact = [i for i in hits if i.symbol.lower() == query.lower()]
        pool = exact or hits
        by_symbol: dict[str, Instrument] = {}
        for inst in pool:  # one listing per symbol; prefer NSE over BSE
            kept = by_symbol.get(inst.symbol)
            if kept is None or (inst.exchange.value == "NSE" and kept.exchange.value != "NSE"):
                by_symbol[inst.symbol] = inst
        unique = list(by_symbol.values())
        if len(unique) == 1:
            return ResolutionResult(status="resolved", query=query, instrument=unique[0])
        return ResolutionResult(status="ambiguous", query=query, candidates=unique)

    # ---- building cards -------------------------------------------------------- #

    async def build(
        self,
        intent: OrderIntent,
        *,
        instrument: Instrument | None = None,
        client_order_id: str | None = None,
        rule_id: str | None = None,
        plan_id: str | None = None,
        ttl_seconds: int | None = None,
        funded_by_plan: bool = False,
    ) -> PendingOrder:
        """`funded_by_plan`: this buy is paid for by an earlier sale in the same plan, so today's cash
        balance says nothing about whether it will work and the low-cash warning is skipped."""
        locks = await self._broker.get_account_locks()
        check_locks(locks, intent.action)  # fail fast: no need to look anything up under Anchor
        if intent.action is OrderAction.PLACE:
            return await self._build_place(
                intent, locks, instrument, client_order_id, rule_id, plan_id, ttl_seconds, funded_by_plan
            )
        return await self._build_amend(intent, locks, instrument, client_order_id, rule_id, plan_id, ttl_seconds)

    async def requote(self, p: PendingOrder) -> PendingOrder:
        """A fresh card for the same order at the current price (after price drift)."""
        if p.action is OrderAction.CANCEL:
            intent = OrderIntent(action=OrderAction.CANCEL, target_order_id=p.target_order_id)
        else:
            intent = OrderIntent(
                action=p.action,
                instrument_ref=p.instrument.symbol,
                side=p.side if p.action is OrderAction.PLACE else None,
                quantity=p.quantity,
                order_type=p.order_type,
                limit_price=p.limit_price if p.order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT) else None,
                trigger_price=p.trigger_price,
                product=p.product,
                validity=p.validity,
                target_order_id=p.target_order_id,
            )
        return await self.build(
            intent,
            instrument=p.instrument,
            client_order_id=p.client_order_id if p.rule_id else None,  # a rule's order id stays fixed
            rule_id=p.rule_id,
            plan_id=p.plan_id,
            ttl_seconds=self._settings.rule_card_ttl_seconds if p.rule_id else None,
        )

    # ---- internals --------------------------------------------------------------- #

    def _protection_price(self, inst: Instrument, side: Side, ltp: int) -> int:
        """A MARKET order is sent as a limit this far from LTP, so it can never fill wildly away."""
        bps = round(self._settings.market_protection_pct * 100)
        tick = inst.tick_size
        if side is Side.BUY:
            raw = -(-ltp * (10_000 + bps) // 10_000)
            price = -(-raw // tick) * tick
            if inst.price_band_high:
                price = min(price, inst.price_band_high)
        else:
            price = (ltp * (10_000 - bps) // 10_000) // tick * tick
            if inst.price_band_low:
                price = max(price, inst.price_band_low)
            price = max(price, tick)
        return price

    def _stop_prices(
        self, inst: Instrument, side: Side, ltp: int, trigger: int, limit: int | None
    ) -> tuple[int, int]:
        """(trigger, limit) for a stop-loss order. The trigger must be on the far side of today's price
        or the broker would fire it at once; the limit, if not given, sits a protection margin beyond it."""
        check_price(inst, trigger)
        if side is Side.SELL and trigger >= ltp:
            raise OrderBlocked(
                RejectionReason.INVALID_PRICE,
                f"A stop-loss to sell must be set below the current price ({fmt_rupees(ltp)}); "
                f"{fmt_rupees(trigger)} would trigger straight away.",
            )
        if side is Side.BUY and trigger <= ltp:
            raise OrderBlocked(
                RejectionReason.INVALID_PRICE,
                f"A stop order to buy must be set above the current price ({fmt_rupees(ltp)}); "
                f"{fmt_rupees(trigger)} would trigger straight away.",
            )
        if limit is None:
            return trigger, self._protection_price(inst, side, trigger)
        check_price(inst, limit)
        if (side is Side.SELL and limit > trigger) or (side is Side.BUY and limit < trigger):
            raise OrderBlocked(
                RejectionReason.INVALID_PRICE,
                f"For a {side.value.lower()} stop, the limit price must be "
                f"{'at or below' if side is Side.SELL else 'at or above'} the trigger ({fmt_rupees(trigger)}).",
            )
        return trigger, limit

    @staticmethod
    def _stop_warning(inst: Instrument, side: Side, trigger: int, limit: int) -> str:
        word = "falls to" if side is Side.SELL else "rises to"
        edge = "less than" if side is Side.SELL else "more than"
        return (
            f"Nothing is sold or bought now. This order waits until {inst.symbol} {word} {fmt_rupees(trigger)}, then "
            f"places a {side.value.lower()} order at no {edge} {fmt_rupees(limit)}. If the price jumps past that "
            "in one move, it may not fill."
        )

    async def _fraction_quantity(self, inst: Instrument, intent: OrderIntent) -> tuple[int, str]:
        """Whole shares for 'half / a third / 30% / all of what I hold', rounded DOWN so a sale never
        exceeds the fraction asked for. Returns the quantity and a sentence showing the sum."""
        if intent.product is Product.MIS:
            held = sum(
                p.quantity
                for p in await self._broker.get_positions()
                if p.instrument.key == inst.key and p.product is Product.MIS and p.quantity > 0
            )
            what = f"your intraday position of {held}"
        else:
            held = owned_quantity(await delivery_owned(self._broker), inst.key)  # holdings + today's delivery buys
            what = f"your {held}"
        # Exact fractions: a third of 3 shares is 1, not 0.9999999 rounded down to nothing.
        fraction = Fraction(str(intent.fraction_of_holding)).limit_denominator(1000)
        exact = held * fraction
        quantity = exact.numerator // exact.denominator  # rounded down
        label = {Fraction(1): "All", Fraction(1, 2): "Half", Fraction(1, 3): "A third", Fraction(1, 4): "A quarter"}.get(
            fraction, f"{float(fraction * 100):g}%"
        )
        if not held:
            raise OrderBlocked(RejectionReason.INVALID_QUANTITY, f"You don't hold any {inst.symbol}, so there is nothing to sell.")
        if quantity < 1:
            raise OrderBlocked(
                RejectionReason.INVALID_QUANTITY,
                f"{label} of {what} shares of {inst.symbol} is less than one share, so there is nothing to sell.",
            )
        if fraction == 1:
            return quantity, f"That is every share you hold: {what} of {inst.symbol}."
        return quantity, (
            f"{label} of {what} shares of {inst.symbol} is {float(exact):g}; "
            f"rounded down to whole shares, that is {quantity}."
        )

    def _expiry(self, ttl_seconds: int | None = None) -> tuple[datetime, datetime]:
        now = self._clock()
        return now, now + timedelta(seconds=ttl_seconds or self._settings.approval_ttl_seconds)

    async def _build_place(
        self, intent, locks, instrument, client_order_id, rule_id, plan_id, ttl_seconds=None, funded_by_plan=False
    ) -> PendingOrder:
        inst = instrument
        if inst is None:
            resolution = await self.resolve(intent.instrument_ref or "")
            if resolution.status != "resolved":
                raise NeedsClarification(resolution)
            inst = resolution.instrument
        check_instrument(inst, self.limits)

        quote = await self._broker.get_quote(inst.key)
        ltp, side = quote.ltp, intent.side
        warnings: list[str] = []

        trigger: int | None = None
        if intent.order_type is OrderType.STOP_LIMIT:
            trigger, limit_price = self._stop_prices(inst, side, ltp, intent.trigger_price, intent.limit_price)
            price, protection, sizing_price = limit_price, None, limit_price
            warnings.append(self._stop_warning(inst, side, trigger, limit_price))
        elif intent.order_type is OrderType.LIMIT:
            price, limit_price, protection = intent.limit_price, intent.limit_price, None
            check_price(inst, price)
            sizing_price = price
        else:
            protection = self._protection_price(inst, side, ltp)
            price, limit_price, sizing_price = protection, None, ltp

        quantity = intent.quantity
        if intent.fraction_of_holding is not None:  # "half my TCS": the code does the sum, not the model
            quantity, note = await self._fraction_quantity(inst, intent)
            warnings.append(note)
        elif quantity is None:  # "worth Rs 10k": whole shares, rounded down, shown to the trader
            quantity = intent.amount_paise // sizing_price
            if quantity < 1:
                raise OrderBlocked(
                    RejectionReason.INVALID_QUANTITY,
                    f"{fmt_rupees(intent.amount_paise)} is less than one share of {inst.symbol} "
                    f"({fmt_rupees(sizing_price)}).",
                )
            warnings.append(
                f"You asked for about {fmt_rupees(intent.amount_paise)}: that is {quantity} shares at "
                f"{fmt_rupees(sizing_price)}, about {fmt_rupees(quantity * sizing_price)}."
            )

        check_size(quantity, price, self.limits)
        check_locks(locks, OrderAction.PLACE, quantity * price)

        if side is Side.SELL and intent.product is Product.CNC:
            held = owned_quantity(await delivery_owned(self._broker), inst.key)  # holdings + today's delivery buys
            if quantity > held:
                have = f"You hold {held} shares of {inst.symbol}" if held else f"You don't hold any {inst.symbol}"
                if intent.amount_paise is not None:  # say how the number was read, so a rupee reading is not a mystery
                    ask = f"{fmt_rupees(intent.amount_paise)} of {inst.symbol} is about {quantity} shares at {fmt_rupees(sizing_price)}"
                    mine = f"you hold {held}" if held else "you don't hold any"
                    raise OrderBlocked(RejectionReason.INVALID_QUANTITY, f"{ask}, but {mine}.")
                raise OrderBlocked(RejectionReason.INVALID_QUANTITY, f"{have}, so you can't sell {quantity}.")

        charges = compute_charges(
            exchange=inst.exchange, side=side, product=intent.product, quantity=quantity, price=price
        )
        est_total = estimated_total(side, quantity, price, charges)

        if limit_price is not None and trigger is None:
            gap = (limit_price - ltp) * 100 / ltp
            if abs(gap) > FAR_FROM_LTP_PCT:
                warnings.append(
                    f"Your limit price {fmt_rupees(limit_price)} is {abs(gap):.1f}% "
                    f"{'above' if gap > 0 else 'below'} the current price {fmt_rupees(ltp)}."
                )
            if (side is Side.BUY and limit_price > ltp) or (side is Side.SELL and limit_price < ltp):
                warnings.append(
                    f"The current price is {fmt_rupees(ltp)}, which is better than your limit, so this "
                    "will fill straight away at the market price (never worse than your limit)."
                )
        if side is Side.BUY and not funded_by_plan:
            funds = await self._broker.get_funds()
            if est_total > funds.available_cash:
                warnings.append(
                    f"This needs about {fmt_rupees(est_total)} but your available cash is "
                    f"{fmt_rupees(funds.available_cash)}; the broker is likely to reject it."
                )

        created, expires = self._expiry(ttl_seconds)
        return PendingOrder(
            id=self._new_id("pend"),
            action=OrderAction.PLACE,
            instrument=inst,
            side=side,
            quantity=quantity,
            order_type=intent.order_type,
            limit_price=limit_price,
            protection_price=protection,
            trigger_price=trigger,
            product=intent.product,
            validity=intent.validity,
            client_order_id=client_order_id or self._new_id("td"),
            charges=charges,
            est_total=est_total,
            ref_ltp=ltp,
            created_at=created,
            expires_at=expires,
            rule_id=rule_id,
            plan_id=plan_id,
            warnings=warnings,
        )

    async def _build_amend(self, intent, locks, instrument, client_order_id, rule_id, plan_id, ttl_seconds=None) -> PendingOrder:
        orders = await self._broker.get_orders()
        target = next((o for o in orders if o.order_id == intent.target_order_id), None)
        if target is None:
            raise OrderBlocked(RejectionReason.OTHER, "I can't find that order in your order book.")
        if target.status not in (OrderStatus.OPEN, OrderStatus.PARTIAL):
            raise OrderBlocked(
                RejectionReason.OTHER, f"That order is already {target.status.value.lower()} and can't be changed."
            )
        inst = instrument or await self._broker.get_instrument(target.instrument.key) or target.instrument
        quote = await self._broker.get_quote(inst.key)
        created, expires = self._expiry(ttl_seconds)
        common = dict(
            id=self._new_id("pend"),
            action=intent.action,
            instrument=inst,
            side=target.side,
            product=target.product,
            validity=target.validity,
            target_order_id=target.order_id,
            client_order_id=client_order_id or self._new_id("td"),
            ref_ltp=quote.ltp,
            created_at=created,
            expires_at=expires,
            rule_id=rule_id,
            plan_id=plan_id,
        )

        if intent.action is OrderAction.CANCEL:
            return PendingOrder(
                **common,
                quantity=target.pending_quantity,
                order_type=target.order_type,
                limit_price=target.limit_price,
                trigger_price=target.trigger_price,
            )

        quantity = intent.quantity if intent.quantity is not None else target.quantity
        limit_price = intent.limit_price if intent.limit_price is not None else target.limit_price
        trigger = target.trigger_price
        if intent.trigger_price is not None:
            if target.order_type is not OrderType.STOP_LIMIT:
                raise OrderBlocked(RejectionReason.OTHER, "That order is not a stop-loss, so it has no trigger price to move.")
            # A new trigger comes with a limit just beyond it, unless the trader named one.
            trigger, limit_price = self._stop_prices(
                inst, target.side, quote.ltp, intent.trigger_price, intent.limit_price
            )
        elif target.order_type is OrderType.STOP_LIMIT and intent.limit_price is not None:
            trigger, limit_price = self._stop_prices(inst, target.side, quote.ltp, target.trigger_price, intent.limit_price)
        if quantity < target.filled_quantity:
            raise OrderBlocked(
                RejectionReason.INVALID_QUANTITY,
                f"{target.filled_quantity} shares of this order have already filled, so it can't go below that.",
            )
        check_instrument(inst, self.limits)
        if intent.limit_price is not None:
            check_price(inst, intent.limit_price)
        price = limit_price or quote.ltp
        check_size(quantity, price, self.limits)
        check_locks(locks, OrderAction.MODIFY, quantity * price)
        if target.order_type is OrderType.STOP_LIMIT:
            order_type = OrderType.STOP_LIMIT
        else:
            order_type = OrderType.LIMIT if limit_price is not None else target.order_type
        charges = compute_charges(
            exchange=inst.exchange, side=target.side, product=target.product, quantity=quantity, price=price
        )
        warnings = [self._stop_warning(inst, target.side, trigger, limit_price)] if trigger is not None else []
        return PendingOrder(
            **common,
            quantity=quantity,
            order_type=order_type,
            limit_price=limit_price,
            trigger_price=trigger,
            warnings=warnings,
            charges=charges,
            est_total=estimated_total(target.side, quantity, price, charges),
        )
