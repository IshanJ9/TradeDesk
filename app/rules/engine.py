"""Evaluates active rules against the live price feed.

Exactly-once, in layers:
 1. Ticks carry a per-instrument `seq`. A tick whose seq is not newer than the last one seen is a
    duplicate or arrived out of order, and is dropped before any rule is looked at.
 2. Whatever gets past that, firing is the store's conditional UPDATE (ACTIVE -> FIRED): only the
    caller whose update took effect proceeds.
 3. A fired rule's order carries the fixed client order id `rule-<id>`, and the executor's
    write-ahead log refuses to send the same id twice. So even a duplicated card cannot buy twice.

A rule never sends an order. It tells the trader, and for TRIGGER_ORDER rules it prepares a fresh
approval card, priced now.
"""

from collections.abc import Callable
from datetime import datetime

from app.api_models import RuleFiredEvent, RuleUpdateEvent
from app.audit import AuditLog
from app.broker.base import BrokerTimeout, ReadOnlyBroker
from app.config import Settings
from app.events import EventHub
from app.orders.cards import CardService
from app.rules.store import RuleStore
from app.schemas import AuditKind, Comparator, PendingOrder, Rule, RuleKind, Tick, fmt_rupees


class RuleEngine:
    def __init__(
        self,
        store: RuleStore,
        cards: CardService,
        broker: ReadOnlyBroker,
        audit: AuditLog,
        hub: EventHub,
        settings: Settings,
        clock: Callable[[], datetime],
    ):
        self._store = store
        self._cards = cards
        self._broker = broker
        self._audit = audit
        self._hub = hub
        self._settings = settings
        self._clock = clock
        self._last_seq: dict[str, int] = {}

    def reset_feed(self) -> None:
        """Call when the price feed reconnects and its sequence numbers may start over."""
        self._last_seq.clear()

    async def on_tick(self, tick: Tick) -> None:
        if tick.seq <= self._last_seq.get(tick.instrument_key, -1):
            return  # duplicate or out-of-order tick
        self._last_seq[tick.instrument_key] = tick.seq
        for rule in self._store.active_for(tick.instrument_key):
            if rule.condition.is_met(tick.ltp):
                await self._fire(rule, tick.ltp, tick.seq)

    async def _fire(self, rule: Rule, ltp: int, seq: int) -> None:
        fired = self._store.mark_fired(rule.id, self._clock())
        if fired is None:
            return  # another tick (or worker) already fired it
        self._audit.record(
            AuditKind.RULE_FIRED,
            "system",
            f"Rule fired: {fired.description}",
            subject_id=fired.id,
            data={"ltp": ltp, "tick_seq": seq, "trigger_price": fired.condition.trigger_price},
        )
        self._hub.publish(RuleUpdateEvent, rule=fired)
        await self._deliver(fired, ltp)
        self._store.mark_delivered(fired.id)

    async def recover(self) -> int:
        """Deliver rules that fired but whose notification/card was lost (e.g. a crash in between)."""
        done = 0
        for rule in self._store.undelivered_fired():
            try:
                ltp = (await self._broker.get_quote(rule.condition.instrument_key)).ltp
                await self._deliver(rule, ltp, late=True)
            except (BrokerTimeout, KeyError):
                continue  # try again on the next pass
            self._store.mark_delivered(rule.id)
            done += 1
        return done

    # ------------------------------------------------------------------ #

    async def _deliver(self, rule: Rule, ltp: int, late: bool = False) -> None:
        cond = rule.condition
        symbol = cond.instrument_key.split(":", 1)[1]
        below = cond.comparator is Comparator.BELOW
        trigger = cond.trigger_price
        where = f"{symbol} is now {fmt_rupees(ltp)}, {'below' if below else 'above'} your trigger of {fmt_rupees(trigger)}"
        prefix = "While the app was restarting, " if late else ""

        pending: PendingOrder | None = None
        if rule.kind is RuleKind.ALERT:
            message = f"Alert: {prefix}{where}."
        else:
            gap = abs(ltp - trigger) * 100 / trigger
            warnings = []
            if gap > rule.max_gap_pct:
                warnings.append(
                    f"The price is already {gap:.1f}% {'below' if below else 'above'} your trigger of "
                    f"{fmt_rupees(trigger)}; it moved quickly past it, so check the price before approving."
                )
            proposal = await self._cards.propose(
                rule.order_template,
                client_order_id=rule.client_order_id,
                rule_id=rule.id,
                extra_warnings=warnings,
                ttl_seconds=self._settings.rule_card_ttl_seconds,
            )
            pending = proposal.pending
            if pending is not None:
                message = f"Your rule fired: {prefix}{where}. I've prepared this order for your approval: {proposal.message}"
            else:
                message = f"Your rule fired: {prefix}{where}, but I couldn't prepare the order. {proposal.message}"
        self._hub.publish(RuleFiredEvent, rule=rule, message=message, ltp=ltp, pending=pending)
