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
from app.orders.limits import OrderBlocked, check_instrument, check_locks
from app.pending import PendingStore
from app.schemas import (
    AuditKind,
    OrderAction,
    OrderStatus,
    PendingOrder,
    PendingState,
    fmt_rupees,
)


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
    ):
        self._store = store
        self._builder = builder
        self._executor = executor
        self._broker = broker
        self._audit = audit
        self._hub = hub
        self._settings = settings
        self._clock = clock

    # ------------------------------------------------------------------ #

    async def approve(self, pending_id: str, order_hash: str) -> ExecutionResult:
        p = self._store.get(pending_id)
        if p is None:
            raise ApprovalNotFound(pending_id)

        if p.state is not PendingState.PENDING:
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

    async def reject(self, pending_id: str) -> PendingOrder:
        p = self._store.get(pending_id)
        if p is None:
            raise ApprovalNotFound(pending_id)
        if p.state is not PendingState.PENDING:
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
