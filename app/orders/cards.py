"""Creates order cards from intents and records them. Shared by the REST preview route and the
LLM's `propose_order` tool, so both go through exactly the same checks and audit trail.

Nothing here sends an order. A card is only ever a proposal waiting for the trader's click.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field

from app.api_models import AmbiguityCard, ChatReply, NoticeCard, PendingCreatedEvent, PendingOrderCard
from app.audit import AuditLog
from app.events import EventHub
from app.orders.builder import NeedsClarification, OrderBuilder
from app.orders.limits import HIGH_RISK_PAUSED, OrderBlocked, crosses_own_limit
from app.orders.readback import readback
from app.pending import PendingStore
from app.risk.guard import NoRiskGuard, RiskGuard
from app.schemas import AuditKind, Instrument, OrderIntent, PendingOrder, RejectionReason

_LOCK_REASONS = {RejectionReason.ANCHOR_ACTIVE, RejectionReason.CO_APPROVAL_REQUIRED}


@dataclass
class Proposal:
    status: str  # "card_created" | "needs_clarification" | "not_found" | "blocked"
    reply: ChatReply
    message: str = ""
    pending: PendingOrder | None = None
    candidates: list[Instrument] = field(default_factory=list)


class CardService:
    def __init__(
        self, builder: OrderBuilder, store: PendingStore, hub: EventHub, audit: AuditLog, risk: RiskGuard | None = None
    ):
        self._builder = builder
        self._store = store
        self._hub = hub
        self._audit = audit
        self._risk = risk or NoRiskGuard()

    async def resolve(self, query: str):
        return await self._builder.resolve(query)

    async def propose(
        self,
        intent: OrderIntent,
        *,
        client_order_id: str | None = None,
        rule_id: str | None = None,
        extra_warnings: Sequence[str] = (),
        ttl_seconds: int | None = None,
    ) -> Proposal:
        try:
            pending = await self._builder.build(
                intent, client_order_id=client_order_id, rule_id=rule_id, ttl_seconds=ttl_seconds
            )
        except NeedsClarification as need:
            res = need.resolution
            if res.status == "ambiguous":
                names = ", ".join(c.name or c.symbol for c in res.candidates)
                text = f"Which one do you mean by “{res.query}”? {names}"
                return Proposal(
                    "needs_clarification",
                    ChatReply(text=text, cards=[AmbiguityCard(query=res.query, candidates=res.candidates)]),
                    text,
                    candidates=res.candidates,
                )
            text = f"I couldn't find an instrument matching “{res.query}”."
            return Proposal("not_found", ChatReply(text=text, cards=[NoticeCard(level="warning", message=text)]), text)
        except OrderBlocked as blocked:
            kind = AuditKind.LOCK_BLOCKED if blocked.reason in _LOCK_REASONS else AuditKind.LIMIT_BLOCKED
            self._audit.record(kind, "system", blocked.message, data={"reason": blocked.reason.value})
            return Proposal(
                "blocked",
                ChatReply(text=blocked.message, cards=[NoticeCard(level="blocked", message=blocked.message)]),
                blocked.message,
            )

        verdict = await self._risk.check(pending, "preview")  # the trader's own limits (app/risk/guard.py)
        if verdict.block:
            self._audit.record(AuditKind.LIMIT_BLOCKED, "system", verdict.block, data={"reason": "RISK_LIMIT"})
            return Proposal(
                "blocked",
                ChatReply(text=verdict.block, cards=[NoticeCard(level="blocked", message=verdict.block)]),
                verdict.block,
            )
        if pending.risk_ack_required and crosses_own_limit(verdict.warnings):
            # past a limit the trader set themselves: no NEW futures / short options until they are back inside it
            self._audit.record(AuditKind.LIMIT_BLOCKED, "system", HIGH_RISK_PAUSED, data={"reason": "RISK_LIMIT"})
            return Proposal(
                "blocked",
                ChatReply(text=HIGH_RISK_PAUSED, cards=[NoticeCard(level="blocked", message=HIGH_RISK_PAUSED)]),
                HIGH_RISK_PAUSED,
            )
        extra = [*verdict.warnings, *extra_warnings]
        if extra:  # warnings are not part of the order hash
            pending = pending.model_copy(update={"warnings": [*extra, *pending.warnings]})
        self._store.put(pending)
        self._hub.publish(PendingCreatedEvent, pending=pending)
        text = readback(pending)
        self._audit.record(
            AuditKind.PENDING_CREATED,
            "system",
            text,
            subject_id=pending.id,
            data={"order_hash": pending.order_hash, "client_order_id": pending.client_order_id},
        )
        return Proposal(
            "card_created", ChatReply(text=text, cards=[PendingOrderCard(pending=pending)]), text, pending=pending
        )
