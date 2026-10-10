"""The only path from 'trader clicked Approve' to 'an order is sent'.

Order of events in `approve`:
 1. The card must exist, be PENDING, be unexpired, and the echoed hash must match exactly.
 2. The card is claimed (PENDING -> APPROVED) before any `await`, so two simultaneous clicks
    cannot both pass: the second sees APPROVED and is refused.
 3. Things that may have changed since the card was shown are re-checked: locks, the
    instrument's status, the target order of a modify/cancel, and price drift.
 4. Only then does the executor send it.

Anything that stops the order after step 1 voids the card (or replaces it with a fresh one
for a price move) and says so. Nothing is ever silently retried.
"""

import hmac
from collections.abc import Callable
from datetime import datetime
from typing import NoReturn

from app.api_models import ExecutionResult, PendingCreatedEvent, PendingUpdatedEvent
from app.audit import AuditLog
from app.broker.base import BrokerAdapter, BrokerTimeout
from app.config import Settings
from app.events import EventHub
from app.orders.builder import OrderBuilder
from app.orders.executor import DuplicateExecution, Executor
from app.orders.limits import HIGH_RISK_PAUSED, OrderBlocked, check_instrument, check_locks, crosses_own_limit
from app.pending import PendingStore
from app.risk.guard import NoRiskGuard, RiskGuard
from app.schemas import (
    AuditKind,
    OrderAction,
    OrderStatus,
    PendingOrder,
    PendingState,
    fmt_rupees,
)


RISK_ACK_TEXT = "I UNDERSTAND"  # typed by the trader on cards that can lose more than they put in


class ApprovalNotFound(Exception):
    pass


class ApprovalError(Exception):
    """The order was NOT sent. `code` says why."""

    def __init__(self, code: str, message: str, pending: PendingOrder | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.pending = pending


class ApprovalService:
    def __init__(
        self,
        store: PendingStore,
        builder: OrderBuilder,
        executor: Executor,
        broker: BrokerAdapter,
        audit: AuditLog,
        hub: EventHub,
        settings: Settings,
        clock: Callable[[], datetime],
        risk: RiskGuard | None = None,
    ):
        self._risk = risk or NoRiskGuard()
        self._store = store
        self._builder = builder
        self._executor = executor
        self._broker = broker
        self._audit = audit
        self._hub = hub
        self._settings = settings
        self._clock = clock
        self.cocaptain = None  # set by app/main.py: app.cocaptain.service.CoCaptainService (None: no second approver)

    # ------------------------------------------------------------------ #

    async def approve(self, pending_id: str, order_hash: str, acknowledgment: str | None = None) -> ExecutionResult:
        p = self._store.get(pending_id)
        if p is None:
            raise ApprovalNotFound(pending_id)

        if p.state not in (PendingState.PENDING, PendingState.AWAITING_CO_APPROVAL):
            self._refuse(p, "NOT_PENDING", f"This card is already {p.state.value.lower()}.")
        if p.is_expired(self._clock()):
            self._refuse(p, "EXPIRED", "This card expired. Ask again for a fresh one.", PendingState.EXPIRED)
        if not hmac.compare_digest(order_hash, p.order_hash):
            self._refuse(
                p,
                "HASH_MISMATCH",
                "What you approved doesn't match this card, so it has been cancelled. Nothing was sent.",
                PendingState.VOID,
            )

        if p.risk_ack_required and (acknowledgment or "").strip() != RISK_ACK_TEXT:
            # the card stays open: nothing is claimed, nothing is sent, the trader can type it and try again
            self._refuse(p, "ACK_REQUIRED", f"This order needs you to type {RISK_ACK_TEXT} first. Nothing was sent.")

        if self.cocaptain is not None and p.action is OrderAction.PLACE:
            price = p.limit_price or p.protection_price or p.ref_ltp
            try:
                gate = await self.cocaptain.gate(orders=1, value=(p.quantity or 0) * price)
            except BrokerTimeout:
                self._void(p, "I couldn't reach the broker to double-check, so nothing was sent. Please try again.")
            p = self._fresh(p)  # the gate awaited: look again before acting on what we read earlier
            if gate.blocked:
                self._audit.record(AuditKind.LIMIT_BLOCKED, "system", gate.blocked, subject_id=p.id, data={"reason": "NO_COCAPTAIN"})
                self._refuse(p, "BLOCKED", gate.blocked)
            if gate.required:
                if p.state is PendingState.PENDING:  # the trader's own approval is saved; the card now waits for the second
                    self.cocaptain.store.record(p.id, "TRADER", "local", p.order_hash, gate.policy_version, "APPROVE", self._clock())
                    waiting = self._store.put(
                        p.transition(PendingState.AWAITING_CO_APPROVAL).model_copy(
                            update={"co_captain": gate.reviewer_id, "co_reasons": list(gate.reasons)}
                        )
                    )
                    self._hub.publish(PendingUpdatedEvent, pending=waiting)
                    self._audit.record(
                        AuditKind.COCAPTAIN, "user", f"Trader approved {p.instrument.symbol}; waiting for {gate.reviewer_id}",
                        subject_id=p.id, data={"reasons": list(gate.reasons), "policy": gate.policy_version},
                    )
                    raise ApprovalError("AWAITING_CO_CAPTAIN", self._waiting_text(gate.reviewer_id), waiting)
                if not self.cocaptain.co_decision_counts(p.id, p.order_hash, gate.policy_version):
                    raise ApprovalError("AWAITING_CO_CAPTAIN", self._waiting_text(gate.reviewer_id), p)
            # not required any more (the trader is back inside their limit): this click is the trader's fresh approval

        return await self._send(p)

    @staticmethod
    def _waiting_text(reviewer: str | None) -> str:
        return f"Waiting for your Co-Captain, {reviewer}, to approve the same order. Nothing has been sent."

    async def co_approve(self, pending_id: str, order_hash: str, reviewer_id: str) -> ExecutionResult:
        """The Co-Captain's click. It adds their approval; the order goes only if everything still holds."""
        p = self._store.get(pending_id)
        if p is None or self.cocaptain is None or not self.cocaptain.may_review(reviewer_id):
            raise ApprovalNotFound(pending_id)  # not theirs to see: the same answer as "no such card"
        if p.state is not PendingState.AWAITING_CO_APPROVAL:
            self._refuse(p, "NOT_PENDING", f"This card is {p.state.value.lower().replace('_', ' ')}, not waiting for you.")
        if p.is_expired(self._clock()):
            self._refuse(p, "EXPIRED", "This card expired. Ask for a fresh one.", PendingState.EXPIRED)
        if not hmac.compare_digest(order_hash, p.order_hash):
            self._refuse(p, "HASH_MISMATCH", "What you approved doesn't match this card, so it has been cancelled. "
                         "Nothing was sent.", PendingState.VOID)
        price = p.limit_price or p.protection_price or p.ref_ltp
        try:
            gate = await self.cocaptain.gate(orders=1, value=(p.quantity or 0) * price)
        except BrokerTimeout:
            self._void(p, "I couldn't reach the broker to double-check, so nothing was sent. Please try again.")
        p = self._fresh(p, only=PendingState.AWAITING_CO_APPROVAL)
        if not self.cocaptain.may_review(reviewer_id):  # the link may have been revoked while we looked
            raise ApprovalNotFound(pending_id)
        self.cocaptain.store.record(p.id, "CO_CAPTAIN", reviewer_id, p.order_hash, gate.policy_version, "APPROVE", self._clock())
        self._audit.record(AuditKind.COCAPTAIN, "user", f"Co-Captain {reviewer_id} approved {p.instrument.symbol}", subject_id=p.id,
                           data={"by": reviewer_id, "order_hash": p.order_hash})
        if not gate.required:
            # the trader is no longer past their limit: they must click Approve themselves, on this card, again
            raise ApprovalError("AWAITING_CO_CAPTAIN", "Thanks. The trader is back inside their limit, so they will "
                                "need to approve it again themselves. Nothing has been sent.", p)
        return await self._send(p)

    async def co_decline(self, pending_id: str, reviewer_id: str) -> PendingOrder:
        p = self._store.get(pending_id)
        if p is None or self.cocaptain is None or not self.cocaptain.may_review(reviewer_id):
            raise ApprovalNotFound(pending_id)
        if p.state is not PendingState.AWAITING_CO_APPROVAL:
            self._refuse(p, "NOT_PENDING", f"This card is {p.state.value.lower().replace('_', ' ')}, not waiting for you.")
        self.cocaptain.store.record(p.id, "CO_CAPTAIN", reviewer_id, p.order_hash, "", "DECLINE", self._clock())
        rejected = self._store.put(p.transition(PendingState.REJECTED))
        self._hub.publish(PendingUpdatedEvent, pending=rejected)
        self._audit.record(AuditKind.COCAPTAIN, "user", f"Co-Captain {reviewer_id} declined {p.instrument.symbol}", subject_id=p.id,
                           data={"by": reviewer_id})
        return rejected

    async def _send(self, p: PendingOrder) -> ExecutionResult:
        """Claim the card, re-check everything, and send. The same path whether one person approved or two."""
        p = self._fresh(p)  # a second click that raced this one finds the card already claimed
        approved = self._store.put(p.transition(PendingState.APPROVED))  # claim: no await above this line
        self._audit.record(
            AuditKind.APPROVAL,
            "user",
            f"Approved {approved.action.value} {approved.instrument.symbol}",
            subject_id=approved.id,
            data={"order_hash": approved.order_hash, "client_order_id": approved.client_order_id},
        )

        try:
            await self._recheck(approved)
        except BrokerTimeout:
            self._void(approved, "I couldn't reach the broker to double-check, so nothing was sent. Please try again.")

        try:
            return await self._executor.execute(approved)
        except DuplicateExecution:
            self._void(approved, "This exact order has already been sent.", code="NOT_PENDING")

    def _fresh(self, p: PendingOrder, only: PendingState | None = None) -> PendingOrder:
        """The stored card as it is right now, or a refusal if it is no longer waiting. No await inside."""
        current = self._store.get(p.id)
        waiting = (PendingState.PENDING, PendingState.AWAITING_CO_APPROVAL) if only is None else (only,)
        if current is None or current.state not in waiting:
            state = current.state.value.lower().replace("_", " ") if current else "gone"
            self._refuse(current or p, "NOT_PENDING", f"This card is already {state}.")
        return current

    async def reject(self, pending_id: str) -> PendingOrder:
        p = self._store.get(pending_id)
        if p is None:
            raise ApprovalNotFound(pending_id)
        if p.state not in (PendingState.PENDING, PendingState.AWAITING_CO_APPROVAL):
            self._refuse(p, "NOT_PENDING", f"This card is already {p.state.value.lower()}.")
        rejected = self._store.put(p.transition(PendingState.REJECTED))
        self._hub.publish(PendingUpdatedEvent, pending=rejected)
        self._audit.record(
            AuditKind.APPROVAL_REFUSED, "user", f"Trader declined {p.instrument.symbol}", subject_id=p.id
        )
        return rejected

    # ------------------------------------------------------------------ #

    async def _recheck(self, p: PendingOrder) -> None:
        """Re-validate what could have changed between the card being shown and the click."""
        price = p.limit_price or p.protection_price or p.ref_ltp
        value = p.quantity * price if p.quantity else None

        try:
            check_locks(await self._broker.get_account_locks(), p.action, value)
            fresh = await self._broker.get_instrument(p.instrument.key)
            if fresh is not None and p.action is not OrderAction.CANCEL:
                check_instrument(fresh, self._builder.limits)
        except OrderBlocked as exc:
            self._audit.record(AuditKind.LOCK_BLOCKED, "system", exc.message, subject_id=p.id)
            self._void(p, exc.message)

        verdict = await self._risk.check(p, "approve")  # limits can be crossed between the card and the click
        if verdict.block:
            self._audit.record(AuditKind.LIMIT_BLOCKED, "system", verdict.block, subject_id=p.id)
            self._void(p, verdict.block)
        if p.risk_ack_required:
            # the "approve" stage only reports hard stops, so ask again as a preview to see the trader's own warnings
            soft = await self._risk.check(p, "preview")
            if crosses_own_limit(soft.warnings):
                self._audit.record(AuditKind.LIMIT_BLOCKED, "system", HIGH_RISK_PAUSED, subject_id=p.id)
                self._void(p, HIGH_RISK_PAUSED)

        if p.action in (OrderAction.MODIFY, OrderAction.CANCEL):
            target = next((o for o in await self._broker.get_orders() if o.order_id == p.target_order_id), None)
            if target is None or target.status not in (OrderStatus.OPEN, OrderStatus.PARTIAL):
                state = target.status.value.lower() if target else "not in your order book"
                self._void(p, f"That order is {state}, so it can no longer be changed. Nothing was sent.")

        if p.action in (OrderAction.PLACE, OrderAction.MODIFY):
            quote = await self._broker.get_quote(p.instrument.key)
            drift = abs(quote.ltp - p.ref_ltp) * 100 / p.ref_ltp
            if drift > self._settings.drift_limit_pct:
                await self._requote(p, quote.ltp, drift)

    async def _requote(self, p: PendingOrder, new_ltp: int, drift: float) -> NoReturn:
        try:
            fresh_card = await self._builder.requote(p)
        except OrderBlocked as exc:
            self._void(p, exc.message)
        old = self._store.put(p.transition(PendingState.REQUOTE_REQUIRED))
        self._hub.publish(PendingUpdatedEvent, pending=old)
        self._store.put(fresh_card)
        self._hub.publish(PendingCreatedEvent, pending=fresh_card)
        message = (
            f"The price moved from {fmt_rupees(p.ref_ltp)} to {fmt_rupees(new_ltp)} ({drift:.1f}%) since you "
            "were shown this order. Nothing was sent. Please check the updated order and approve again."
        )
        self._audit.record(
            AuditKind.APPROVAL_REFUSED,
            "system",
            f"Price drift {drift:.2f}%: re-quote required",
            subject_id=p.id,
            data={"old_ltp": p.ref_ltp, "new_ltp": new_ltp, "new_pending_id": fresh_card.id},
        )
        raise ApprovalError("REQUOTE_REQUIRED", message, fresh_card)

    def _refuse(self, p: PendingOrder, code: str, message: str, new_state: PendingState | None = None) -> NoReturn:
        if new_state is not None:
            updated = self._store.put(p.transition(new_state))
            self._hub.publish(PendingUpdatedEvent, pending=updated)
        self._audit.record(
            AuditKind.APPROVAL_REFUSED, "system", f"{code}: {message}", subject_id=p.id, data={"code": code}
        )
        raise ApprovalError(code, message)

    def _void(self, p: PendingOrder, message: str, code: str = "BLOCKED") -> NoReturn:
        voided = self._store.put(p.transition(PendingState.VOID))
        self._hub.publish(PendingUpdatedEvent, pending=voided)
        self._audit.record(
            AuditKind.APPROVAL_REFUSED, "system", f"{code}: {message}", subject_id=p.id, data={"code": code}
        )
        raise ApprovalError(code, message)
