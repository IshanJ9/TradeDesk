"""Creating and cancelling standing instructions.

A rule is only ever a promise to *prepare something for approval* (an alert, or an order card)
when a price condition is met. Creating one moves no money and sends nothing, and when it fires
the trader still has to approve the card.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from pydantic import ValidationError

from app.api_models import AmbiguityCard, ChatReply, CreateRuleRequest, NoticeCard, RuleCard, RuleUpdateEvent
from app.audit import AuditLog
from app.broker.base import ReadOnlyBroker
from app.config import Settings
from app.events import EventHub
from app.orders.cards import CardService
from app.orders.limits import HardLimits, OrderBlocked, check_instrument, check_price, check_size
from app.orders.owned import delivery_owned, owned_average_price
from app.rules.store import RuleStore
from app.schemas import (
    AuditKind,
    Comparator,
    Instrument,
    OrderAction,
    OrderIntent,
    OrderType,
    Rule,
    RuleBasis,
    RuleCondition,
    RuleKind,
    Side,
    fmt_rupees,
    paise,
)


class RuleNotFound(Exception):
    pass


class RuleNotActive(Exception):
    pass


@dataclass
class RuleOutcome:
    status: str  # "rule_created" | "needs_clarification" | "not_found" | "blocked"
    reply: ChatReply
    message: str = ""
    rule: Rule | None = None
    candidates: list[Instrument] = field(default_factory=list)


_BASIS_WORDS = {
    RuleBasis.AVG_BUY: "your average buy price",
    RuleBasis.PREV_CLOSE: "the previous close",
    RuleBasis.AT_CREATION: "its price right now",
}


def default_order_type(side: Side, comparator: Comparator) -> OrderType:
    """A limit order at the trigger can fill when buying on a dip or selling into a rise. A stop-loss
    sale (below) or a breakout buy (above) must fill as the price moves away, so those go as
    price-protected market orders."""
    marketable_as_limit = (side is Side.BUY and comparator is Comparator.BELOW) or (
        side is Side.SELL and comparator is Comparator.ABOVE
    )
    return OrderType.LIMIT if marketable_as_limit else OrderType.MARKET


def _to_tick(price: int, tick: int, *, up: bool) -> int:
    return -(-price // tick) * tick if up else price // tick * tick


class RuleService:
    def __init__(
        self,
        store: RuleStore,
        broker: ReadOnlyBroker,
        cards: CardService,
        limits: HardLimits,
        audit: AuditLog,
        hub: EventHub,
        settings: Settings,
        clock: Callable[[], datetime],
    ):
        self._store = store
        self._broker = broker
        self._cards = cards
        self._limits = limits
        self._audit = audit
        self._hub = hub
        self._settings = settings
        self._clock = clock

    # ------------------------------------------------------------------ #

    def _blocked(self, message: str) -> RuleOutcome:
        reply = ChatReply(text=message, cards=[NoticeCard(level="blocked", message=message)])
        return RuleOutcome("blocked", reply, message)

    async def create(self, req: CreateRuleRequest) -> RuleOutcome:
        active = self._store.active_count()
        if active >= self._settings.max_active_rules:
            return self._blocked(
                f"You already have {active} active rules, the most I can keep. Cancel one first."
            )

        res = await self._cards.resolve(req.instrument)
        if res.status == "ambiguous":
            names = ", ".join(c.name or c.symbol for c in res.candidates)
            text = f"Which one do you mean by “{res.query}”? {names}"
            return RuleOutcome(
                "needs_clarification",
                ChatReply(text=text, cards=[AmbiguityCard(query=res.query, candidates=res.candidates)]),
                text,
                candidates=res.candidates,
            )
        if res.status == "not_found":
            text = f"I couldn't find an instrument matching “{res.query}”."
            return RuleOutcome("not_found", ChatReply(text=text, cards=[NoticeCard(level="warning", message=text)]), text)
        inst = res.instrument
        try:
            check_instrument(inst, self._limits)
        except OrderBlocked as exc:
            return self._blocked(exc.message)

        quote = await self._broker.get_quote(inst.key)
        condition = await self._condition(req, inst, quote)
        if isinstance(condition, RuleOutcome):
            return condition
        trigger, ltp = condition.trigger_price, quote.ltp
        direction = "below" if condition.comparator is Comparator.BELOW else "above"
        if trigger <= 0:
            return self._blocked("That trigger works out to a price of zero or less.")
        if condition.is_met(ltp):
            return self._blocked(
                f"{inst.symbol} is already at {fmt_rupees(ltp)}, which is {direction} {fmt_rupees(trigger)}. "
                "This rule would fire straight away. If you want to act now, ask me for the order directly."
            )

        template = None
        if req.kind is RuleKind.TRIGGER_ORDER:
            template = self._template(req, inst, trigger)
            if isinstance(template, RuleOutcome):
                return template

        rule = Rule(
            id=f"r-{uuid.uuid4().hex[:10]}",
            kind=req.kind,
            description=self._describe(inst.symbol, condition, req.kind, template),
            condition=condition,
            order_template=template,
            created_at=self._clock(),
        )
        self._store.add(rule)
        await self._broker.watch([inst.key])  # a live broker streams only what it is asked to follow
        self._audit.record(
            AuditKind.RULE_CREATED,
            "system",
            rule.description,
            subject_id=rule.id,
            data={"trigger_price": trigger, "basis": condition.basis.value, "kind": rule.kind.value},
        )
        self._hub.publish(RuleUpdateEvent, rule=rule)
        return RuleOutcome("rule_created", ChatReply(text=rule.description, cards=[RuleCard(rule=rule)]), rule.description, rule)

    def list(self, status=None) -> list[Rule]:
        return self._store.list(status)

    async def cancel(self, rule_id: str) -> Rule:
        cancelled = self._store.cancel(rule_id)
        if cancelled is None:
            raise RuleNotActive(rule_id) if self._store.get(rule_id) else RuleNotFound(rule_id)
        self._audit.record(AuditKind.RULE_CANCELLED, "user", f"Cancelled rule: {cancelled.description}", subject_id=rule_id)
        self._hub.publish(RuleUpdateEvent, rule=cancelled)
        return cancelled

    # ------------------------------------------------------------------ #

    async def _condition(self, req: CreateRuleRequest, inst: Instrument, quote) -> RuleCondition | RuleOutcome:
        if req.price_rupees is not None:
            return RuleCondition(
                instrument_key=inst.key,
                comparator=req.comparator,
                basis=RuleBasis.ABSOLUTE,
                absolute_price=paise(req.price_rupees),
            )
        basis = req.basis or RuleBasis.AT_CREATION
        if basis is RuleBasis.AVG_BUY:
            reference = owned_average_price(await delivery_owned(self._broker), inst.key)  # holdings + today's buys
            if reference is None:
                return self._blocked(f"You don't hold {inst.symbol}, so there's no buy price to measure from.")
        elif basis is RuleBasis.PREV_CLOSE:
            reference = quote.prev_close
        else:
            reference = quote.ltp
        pct = -req.percent if req.comparator is Comparator.BELOW else req.percent
        if pct <= -100:
            return self._blocked("A fall of 100% or more isn't a price I can watch for.")
        return RuleCondition(
            instrument_key=inst.key,
            comparator=req.comparator,
            basis=basis,
            reference_price=reference,
            change_pct=pct,
        )

    def _template(self, req: CreateRuleRequest, inst: Instrument, trigger: int) -> OrderIntent | RuleOutcome:
        order_type = req.order_type or (
            OrderType.LIMIT if req.limit_price_rupees is not None else default_order_type(req.side, req.comparator)
        )
        limit = None
        if order_type is OrderType.LIMIT:
            limit = (
                paise(req.limit_price_rupees)
                if req.limit_price_rupees is not None
                else _to_tick(trigger, inst.tick_size, up=req.side is Side.SELL)
            )
            try:
                check_price(inst, limit)
            except OrderBlocked as exc:
                return self._blocked(exc.message)
        try:
            template = OrderIntent(
                action=OrderAction.PLACE,
                instrument_ref=inst.symbol,
                side=req.side,
                quantity=req.quantity,
                amount_paise=paise(req.amount_rupees) if req.amount_rupees is not None else None,
                order_type=order_type,
                limit_price=limit,
                product=req.product,
                validity=req.validity,
            )
        except ValidationError as exc:
            return self._blocked(exc.errors()[0]["msg"].removeprefix("Value error, "))
        if req.quantity is not None:
            try:
                check_size(req.quantity, limit or trigger, self._limits)
            except OrderBlocked as exc:
                return self._blocked(exc.message)
        return template

    @staticmethod
    def _describe(symbol: str, cond: RuleCondition, kind: RuleKind, template: OrderIntent | None) -> str:
        below = cond.comparator is Comparator.BELOW
        trigger = fmt_rupees(cond.trigger_price)
        if cond.basis is RuleBasis.ABSOLUTE:
            when = f"{symbol} {'falls below' if below else 'rises above'} {trigger}"
        else:
            when = (
                f"{symbol} {'falls' if below else 'rises'} {abs(cond.change_pct):g}% from {_BASIS_WORDS[cond.basis]} "
                f"({fmt_rupees(cond.reference_price)}), which is {'below' if below else 'above'} {trigger}"
            )
        if kind is RuleKind.ALERT:
            return f"I'll tell you when {when}."
        if template.quantity:
            size = f"{template.quantity} {'share' if template.quantity == 1 else 'shares'}"
        else:
            size = f"about {fmt_rupees(template.amount_paise)} worth"
        how = (
            f"limit {fmt_rupees(template.limit_price)}"
            if template.order_type is OrderType.LIMIT
            else "at the market, price-protected"
        )
        return (
            f"When {when}, I'll prepare an order to {template.side.value.lower()} {size} of {symbol} ({how}) "
            "and ask you to approve it. Nothing is sent without your approval."
        )
