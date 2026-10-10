"""Sends approved orders to the broker, exactly once.

Guarantees
- Write-ahead: a row keyed by `client_order_id` is inserted BEFORE the broker is called. The
  primary key makes a second send of the same order impossible, even if a caller has a bug.
- No blind retry: on a timeout the order is never re-sent. The broker has no client order id for us
  to look up, so we read its order book for ONE order that looks exactly like what we sent (see
  `app/broker/matching.py`) and report SENT only if we find it. If we can't tell (nothing there yet,
  or several identical orders), the outcome is UNKNOWN and a background reconcile keeps checking.
  'Never placed' is only ever concluded after a clean read of the book plus a grace period.
- Everything is audited: request, response, timeout, reconcile.
"""

import asyncio
import json
import sqlite3
from collections.abc import Callable
from datetime import datetime

from app.account import build_account
from app.api_models import (
    AccountUpdateEvent,
    ExecutionResult,
    OrderUpdateEvent,
    PendingUpdatedEvent,
)
from app.audit import AuditLog
from app.broker.base import BrokerAdapter, BrokerRejected, BrokerTimeout
from app.broker.kind import broker_kind
from app.db import Database
from app.events import EventHub
from app.pending import PendingStore
from app.schemas import (
    AuditKind,
    MatchKind,
    Order,
    OrderAction,
    OrderStatus,
    PendingOrder,
    PendingState,
    SentOrderSpec,
    fmt_rupees,
)

UNRESOLVED = ("SENDING", "UNKNOWN")


class DuplicateExecution(Exception):
    """This client_order_id has already been sent (or is being sent)."""


class Executor:
    def __init__(
        self,
        broker: BrokerAdapter,
        db: Database,
        audit: AuditLog,
        store: PendingStore,
        hub: EventHub,
        clock: Callable[[], datetime],
        *,
        user_id: str,
        reconcile_attempts: int = 3,
        reconcile_delay: float = 0.2,
        grace_seconds: float = 120.0,
    ):
        self._broker = broker
        self._db = db
        self._audit = audit
        self._store = store
        self._hub = hub
        self._user_id = user_id  # this executor only ever reads and writes this user's rows of the shared ledger
        self._clock = clock
        self._attempts = reconcile_attempts
        self._delay = reconcile_delay
        self._grace = grace_seconds

    # ------------------------------------------------------------------ #
    # execute
    # ------------------------------------------------------------------ #

    async def execute(self, approved: PendingOrder) -> ExecutionResult:
        """`approved` must be in APPROVED state. Returns what actually happened."""
        cid = approved.client_order_id
        self._begin(approved)  # write-ahead; raises DuplicateExecution on a second send
        self._audit.record(
            AuditKind.BROKER_REQUEST,
            "system",
            f"{approved.action.value} {self._describe(approved)}",
            subject_id=approved.id,
            data={"client_order_id": cid, "order_hash": approved.order_hash},
        )

        order: Order | None = None
        try:
            order = await self._send(approved)
            outcome, message = "SENT", ""
            self._finish(cid, "SENT", order.order_id)
        except BrokerRejected as exc:
            order, outcome = exc.order, "REJECTED"
            message = exc.message or exc.reason.value
            self._finish(cid, "REJECTED", order.order_id if order else None)
        except BrokerTimeout:
            self._audit.record(
                AuditKind.BROKER_TIMEOUT,
                "broker",
                "Request timed out; checking the order book instead of retrying",
                subject_id=approved.id,
                data={"client_order_id": cid},
            )
            order = await self._reconcile_after_timeout(approved)
            if order is not None:
                outcome = "REJECTED" if order.status is OrderStatus.REJECTED else "SENT"
                message = "Confirmed in your order book after a timeout."
                self._finish(cid, "REJECTED" if outcome == "REJECTED" else "SENT", order.order_id)
            else:
                outcome = "UNKNOWN"
                message = (
                    "We couldn't confirm whether this order reached the broker. It has NOT been re-sent. "
                    "We're checking your order book; please don't place it again until you've seen the result."
                )
                self._finish(cid, "UNKNOWN", None)
        except Exception:
            self._finish(cid, "UNKNOWN", None)
            self._audit.record(
                AuditKind.BROKER_TIMEOUT, "system", "Unexpected error while sending", subject_id=approved.id
            )
            raise

        self._audit.record(
            AuditKind.BROKER_RESPONSE,
            "broker",
            f"{outcome}: {message or (order.status.value if order else '')}".strip(": "),
            subject_id=approved.id,
            data={"outcome": outcome, "order_id": order.order_id if order else None},
        )
        sent = self._store.put(approved.transition(PendingState.SENT))
        self._hub.publish(PendingUpdatedEvent, pending=sent)
        if order is not None:
            self._hub.publish(OrderUpdateEvent, order=order)
        await self._push_account()
        return ExecutionResult(pending=sent, outcome=outcome, order=order, message=message)

    async def _send(self, p: PendingOrder) -> Order:
        if p.action is OrderAction.PLACE:
            return await self._broker.place_order(p)
        if p.action is OrderAction.MODIFY:
            return await self._broker.modify_order(p)
        return await self._broker.cancel_order(p)

    # ------------------------------------------------------------------ #
    # timeout handling
    # ------------------------------------------------------------------ #

    async def _reconcile_after_timeout(self, p: PendingOrder) -> Order | None:
        for attempt in range(self._attempts):
            if attempt and self._delay:
                await asyncio.sleep(self._delay)
            try:
                found = await self._find(p)
            except BrokerTimeout:
                continue
            if found is not None:
                self._audit.record(
                    AuditKind.RECONCILE,
                    "system",
                    "Found in the order book after a timeout",
                    subject_id=p.id,
                    data={"order_id": found.order_id, "status": found.status.value},
                )
                return found
        return None

    def _claimed_order_ids(self) -> set[str]:
        """Broker order ids that already belong to one of our sends; never attribute them twice."""
        rows = self._db.query("SELECT broker_order_id FROM executions WHERE broker_order_id IS NOT NULL AND user_id = ?", (self._user_id,))
        return {r["broker_order_id"] for r in rows}

    async def _match(self, spec: SentOrderSpec, subject_id: str) -> Order | None:
        """The one order in the book that looks like `spec`, or None. Several look-alikes: None, and say so."""
        result = await self._broker.find_sent_order(spec, self._claimed_order_ids())
        if result.kind is MatchKind.AMBIGUOUS:
            self._audit.record(
                AuditKind.RECONCILE,
                "system",
                "Several identical orders are in the order book, so we can't tell which one is ours. "
                "Left as unknown; nothing was re-sent.",
                subject_id=subject_id,
            )
        return result.order

    async def _find(self, p: PendingOrder) -> Order | None:
        """Did the broker act on this request? A new order is matched by what it looks like."""
        if p.action is OrderAction.PLACE:
            return await self._match(SentOrderSpec.from_pending(p, self._started_at(p.client_order_id)), p.id)
        target = next((o for o in await self._broker.get_orders() if o.order_id == p.target_order_id), None)
        if target is None:
            return None
        if p.action is OrderAction.CANCEL:
            return target if target.status is OrderStatus.CANCELLED else None
        changed = (p.quantity is None or target.quantity == p.quantity) and (
            p.limit_price is None or target.limit_price == p.limit_price
        )
        return target if changed else None

    async def reconcile(self) -> int:
        """Re-check executions whose outcome is unresolved. Returns how many were resolved.

        Used at startup (crash recovery) and periodically. A new order that the broker's order
        book doesn't contain after the grace period is marked NOT_SENT; it is never re-sent. If
        look-alike orders make the match ambiguous, the row stays UNKNOWN (never guessed at).
        """
        rows = self._db.query(
            "SELECT * FROM executions WHERE user_id = ? AND status IN (?, ?) AND action = ?", (self._user_id, *UNRESOLVED, OrderAction.PLACE.value)
        )
        resolved = 0
        for row in rows:
            cid = row["client_order_id"]
            spec_json = json.loads(row["detail"]).get("spec")
            if spec_json is None:
                continue
            try:
                result = await self._broker.find_sent_order(SentOrderSpec(**spec_json), self._claimed_order_ids())
            except BrokerTimeout:
                break  # broker unreachable: leave everything as it is
            order = result.order
            if result.kind is MatchKind.AMBIGUOUS:
                continue
            if order is not None:
                status = "REJECTED" if order.status is OrderStatus.REJECTED else "SENT"
                self._finish(cid, status, order.order_id)
                self._audit.record(
                    AuditKind.RECONCILE,
                    "system",
                    f"Resolved {cid}: found in the order book as {order.status.value}",
                    subject_id=row["pending_id"],
                    data={"order_id": order.order_id},
                )
                self._hub.publish(OrderUpdateEvent, order=order)
                resolved += 1
                continue
            age = (self._clock() - datetime.fromisoformat(row["created_at"])).total_seconds()
            if age >= self._grace:
                self._finish(cid, "NOT_SENT", None)
                self._audit.record(
                    AuditKind.RECONCILE,
                    "system",
                    f"Resolved {cid}: after {int(age)}s the order book still has no such order, so it was never placed",
                    subject_id=row["pending_id"],
                )
                resolved += 1
        return resolved

    async def order_for(self, client_order_id: str) -> Order | None:
        """The broker order that our send `client_order_id` became, if we know its id."""
        row = self._db.query("SELECT broker_order_id FROM executions WHERE client_order_id = ? AND user_id = ?", (client_order_id, self._user_id))
        if not row or not row[0]["broker_order_id"]:
            return None
        return await self._broker.get_order(row[0]["broker_order_id"])

    def _started_at(self, client_order_id: str) -> datetime:
        row = self._db.query("SELECT created_at FROM executions WHERE client_order_id = ? AND user_id = ?", (client_order_id, self._user_id))
        return datetime.fromisoformat(row[0]["created_at"])

    def unresolved(self) -> list[dict]:
        rows = self._db.query("SELECT * FROM executions WHERE user_id = ? AND status IN (?, ?)", (self._user_id, *UNRESOLVED))
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    # write-ahead log
    # ------------------------------------------------------------------ #

    def _begin(self, p: PendingOrder) -> None:
        now = self._clock()
        detail: dict = {"order_hash": p.order_hash, "summary": self._describe(p), "account_kind": broker_kind(self._broker)}
        if p.action is OrderAction.PLACE:  # what the order will look like on the wire, for reconcile
            detail["spec"] = SentOrderSpec.from_pending(p, now).model_dump(mode="json")
        now = now.isoformat()
        try:
            self._db.execute(
                "INSERT INTO executions (client_order_id, user_id, pending_id, action, status, created_at, updated_at, detail)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (
                    p.client_order_id,
                    self._user_id,
                    p.id,
                    p.action.value,
                    "SENDING",
                    now,
                    now,
                    json.dumps(detail),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise DuplicateExecution(f"{p.client_order_id} has already been sent") from exc

    def _finish(self, client_order_id: str, status: str, broker_order_id: str | None) -> None:
        self._db.execute(
            "UPDATE executions SET status = ?, broker_order_id = COALESCE(?, broker_order_id), updated_at = ?"
            " WHERE client_order_id = ? AND user_id = ?",
            (status, broker_order_id, self._clock().isoformat(), client_order_id, self._user_id),
        )

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _describe(p: PendingOrder) -> str:
        if p.action is OrderAction.CANCEL:
            return f"cancel order {p.target_order_id} ({p.instrument.symbol})"
        price = f" @ {fmt_rupees(p.limit_price or p.protection_price)}" if (p.limit_price or p.protection_price) else ""
        side = p.side.value if p.side else ""
        return f"{side} {p.quantity} x {p.instrument.symbol}{price}".strip()

    async def _push_account(self) -> None:
        try:
            self._hub.publish(AccountUpdateEvent, account=await build_account(self._broker))
        except BrokerTimeout:
            pass
