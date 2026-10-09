"""Turns an `OrderIntent` (the only order-shaped thing the LLM can emit) into an exact,
priced `PendingOrder`. All the money maths and every rule lives here, in plain code.

The builder never sends anything. It either returns a card for the trader to approve,
raises `NeedsClarification` (ambiguous or unknown instrument: ask, never guess), or raises
`OrderBlocked` (a hard limit or lock: tell the trader why).
"""

import uuid
from collections.abc import Callable
from datetime import datetime, timedelta

from app.broker.base import BrokerAdapter
from app.config import Settings
from app.orders.charges import compute_charges, estimated_total
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
                limit_price=p.limit_price if p.order_type is OrderType.LIMIT else None,
                product=p.product,
                validity=p.validity,
                validity_minutes=p.validity_minutes,
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

        if intent.order_type is OrderType.LIMIT:
            price, limit_price, protection = intent.limit_price, intent.limit_price, None
            check_price(inst, price)
            sizing_price = price
        else:
            protection = self._protection_price(inst, side, ltp)
            price, limit_price, sizing_price = protection, None, ltp

        quantity = intent.quantity
        if quantity is None:  # "worth Rs 10k": whole shares, rounded down, shown to the trader
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
            held = sum(h.quantity for h in await self._broker.get_holdings() if h.instrument.key == inst.key)
            if quantity > held:
                have = f"You hold {held} shares of {inst.symbol}" if held else f"You don't hold any {inst.symbol}"
                raise OrderBlocked(RejectionReason.INVALID_QUANTITY, f"{have}, so you can't sell {quantity}.")

        charges = compute_charges(
            exchange=inst.exchange, side=side, product=intent.product, quantity=quantity, price=price
        )
        est_total = estimated_total(side, quantity, price, charges)

        if limit_price is not None:
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
            product=intent.product,
            validity=intent.validity,
            validity_minutes=intent.validity_minutes,
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
            )

        quantity = intent.quantity if intent.quantity is not None else target.quantity
        limit_price = intent.limit_price if intent.limit_price is not None else target.limit_price
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
        order_type = OrderType.LIMIT if limit_price is not None else target.order_type
        charges = compute_charges(
            exchange=inst.exchange, side=target.side, product=target.product, quantity=quantity, price=price
        )
        return PendingOrder(
            **common,
            quantity=quantity,
            order_type=order_type,
            limit_price=limit_price,
            charges=charges,
            est_total=estimated_total(target.side, quantity, price, charges),
        )
