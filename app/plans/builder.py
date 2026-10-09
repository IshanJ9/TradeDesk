"""Turns a `ProposePlanRequest` into a priced `Plan` of order cards.

Every step is built by the same `OrderBuilder` as a single order, so each one gets the same
instrument checks, hard limits, lock checks, charges and warnings. What a plan adds:

- "half my Infosys" is sized here, in code, as a whole number of shares from the holdings.
- A buy paid for by an earlier sale ("use the money to buy ITC") is sized from that sale's
  estimated net proceeds. The card shows the estimate and the plan carries hard caps
  (`max_quantity`, `max_spend`) that are part of the approval hash: at run time the step is sized
  from the *actual* proceeds and can never exceed what the trader was shown.
"""

import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from fractions import Fraction

from pydantic import ValidationError

from app.api_models import ProposePlanRequest
from app.broker.base import ReadOnlyBroker
from app.config import Settings
from app.orders.builder import OrderBuilder
from app.orders.limits import OrderBlocked
from app.schemas import (
    OrderAction,
    OrderIntent,
    OrderType,
    Plan,
    PlanLeg,
    QuantityBasis,
    RejectionReason,
    ResolutionResult,
    Side,
    fmt_rupees,
    paise,
)


class PlanNeedsClarification(Exception):
    """A step's stock name is ambiguous or unknown. The trader must say which."""

    def __init__(self, leg_index: int, resolution: ResolutionResult):
        super().__init__(f"step {leg_index + 1}: {resolution.status}")
        self.leg_index = leg_index
        self.resolution = resolution


def _default_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


class PlanBuilder:
    def __init__(
        self,
        orders: OrderBuilder,
        broker: ReadOnlyBroker,
        settings: Settings,
        clock: Callable[[], datetime],
        new_id: Callable[[str], str] = _default_id,
    ):
        self._orders = orders
        self._broker = broker
        self._settings = settings
        self._clock = clock
        self._new_id = new_id

    async def build(self, req: ProposePlanRequest) -> Plan:
        plan_id = self._new_id("plan")
        holdings = await self._broker.get_holdings()
        legs: list[PlanLeg] = []
        for i, lr in enumerate(req.legs):
            try:
                legs.append(await self._build_leg(plan_id, i, lr, legs, holdings))
            except OrderBlocked as exc:
                raise OrderBlocked(exc.reason, f"Step {i + 1}: {exc.message}") from exc

        title = req.title or self._title(legs)
        now = self._clock()
        return Plan(
            id=plan_id,
            title=title,
            legs=legs,
            on_leg_failure=req.on_leg_failure,
            created_at=now,
            expires_at=now + timedelta(seconds=self._settings.approval_ttl_seconds),
        )

    # ------------------------------------------------------------------ #

    async def _build_leg(self, plan_id: str, i: int, lr, built: list[PlanLeg], holdings) -> PlanLeg:
        res = await self._orders.resolve(lr.instrument)
        if res.status != "resolved":
            raise PlanNeedsClarification(i, res)
        inst = res.instrument

        order_type = lr.order_type or (OrderType.LIMIT if lr.limit_price_rupees is not None else OrderType.MARKET)
        base = dict(
            action=OrderAction.PLACE,
            instrument_ref=inst.symbol,
            side=lr.side,
            order_type=order_type,
            limit_price=paise(lr.limit_price_rupees) if lr.limit_price_rupees is not None else None,
            product=lr.product,
            validity=lr.validity,
        )

        basis, from_leg, max_quantity, max_spend = QuantityBasis.FIXED, None, None, None
        try:
            if lr.proceeds_of_leg is not None:
                source = built[lr.proceeds_of_leg].order
                probe = await self._orders.build(
                    OrderIntent(**base, quantity=1), instrument=inst, plan_id=plan_id, funded_by_plan=True
                )
                price = probe.limit_price or probe.protection_price
                estimate = source.est_total // price
                if estimate < 1:
                    raise OrderBlocked(
                        RejectionReason.INVALID_QUANTITY,
                        f"the money from step {lr.proceeds_of_leg + 1} (about {fmt_rupees(source.est_total)}) is less "
                        f"than one share of {inst.symbol} ({fmt_rupees(price)}).",
                    )
                basis, from_leg = QuantityBasis.FROM_PROCEEDS, lr.proceeds_of_leg
                max_quantity, max_spend = estimate, estimate * price
                intent = OrderIntent(**base, quantity=estimate)
            elif lr.fraction_of_holding is not None:
                held = sum(h.quantity for h in holdings if h.instrument.key == inst.key)
                exact = held * Fraction(str(lr.fraction_of_holding)).limit_denominator(1000)  # a third of 3 is 1
                quantity = exact.numerator // exact.denominator  # whole shares, rounded down
                if quantity < 1:
                    have = f"your {held} shares" if held else "the shares you hold (you hold none)"
                    raise OrderBlocked(
                        RejectionReason.INVALID_QUANTITY,
                        f"{lr.fraction_of_holding:g} of {have} of {inst.symbol} is less than one share.",
                    )
                intent = OrderIntent(**base, quantity=quantity)
            elif lr.amount_rupees is not None:
                intent = OrderIntent(**base, amount_paise=paise(lr.amount_rupees))
            else:
                intent = OrderIntent(**base, quantity=lr.quantity)
        except ValidationError as exc:
            raise OrderBlocked(RejectionReason.OTHER, exc.errors()[0]["msg"].removeprefix("Value error, ")) from exc

        order = await self._orders.build(
            intent, instrument=inst, plan_id=plan_id, funded_by_plan=basis is QuantityBasis.FROM_PROCEEDS
        )
        return PlanLeg(
            index=i,
            order=order,
            quantity_basis=basis,
            proceeds_from_leg=from_leg,
            max_quantity=max_quantity,
            max_spend=max_spend,
        )

    @staticmethod
    def _title(legs: list[PlanLeg]) -> str:
        words = [f"{'sell' if leg.order.side is Side.SELL else 'buy'} {leg.order.instrument.symbol}" for leg in legs]
        text = ", then ".join(words)
        return text[0].upper() + text[1:]
