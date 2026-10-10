"""Approving and running plans.

`approve` is the plan's equivalent of the single-order approval gate:
 1. The plan must exist, be PENDING, unexpired, and the echoed hash must match exactly (the hash
    covers every step, its caps, and the failure policy).
 2. It is claimed (PENDING -> APPROVED) before any `await`, so a double click cannot run it twice.
 3. Everything that may have changed is re-checked for *every* step: locks, the stocks, the shares
    to be sold, the trader's own hard limits, and price drift. A drifted plan is replaced by a fresh
    one and nothing runs.

Every step is also checked against the trader's own limits (app/risk/guard.py) when the plan is made,
counting the plan's earlier steps as orders, so a 3-step plan is 3 orders toward "orders a day".
 4. Then the steps run in order in the background, each through the same executor as a single
    order (write-ahead log, no blind retry, audit trail).

While running, the next step is only sent if the ones before it did what it depends on:
 - a buy paid for by a sale is sized from the sale's *actual* proceeds and capped at what the
   trader was shown, and is skipped unless the sale completely filled;
 - with the default HALT policy, the first step that does not complete stops everything after it.
Every step ends up in the report with an honest status: nothing is hidden.
"""

import asyncio
import hmac
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import NoReturn

from app.api_models import (
    AmbiguityCard,
    ChatReply,
    NoticeCard,
    PlanCard,
    PlanCreatedEvent,
    PlanReportUpdateEvent,
    PlanUpdatedEvent,
    ProposePlanRequest,
)
from app.audit import AuditLog
from app.broker.base import BrokerAdapter, BrokerTimeout
from app.config import Settings
from app.events import EventHub
from app.orders.builder import OrderBuilder
from app.orders.charges import compute_charges
from app.orders.executor import DuplicateExecution, Executor
from app.orders.limits import OrderBlocked, check_instrument, check_locks
from app.orders.owned import delivery_owned
from app.plans.builder import PlanBuilder, PlanNeedsClarification
from app.plans.readback import leg_label, plan_readback, render_report
from app.plans.store import PlanStore
from app.risk.guard import NoRiskGuard, RiskGuard
from app.schemas import (
    AuditKind,
    Instrument,
    LegFailurePolicy,
    LegStatus,
    Order,
    OrderAction,
    OrderStatus,
    PendingOrder,
    PendingState,
    Plan,
    PlanLeg,
    PlanLegResult,
    PlanReport,
    PlanState,
    Product,
    QuantityBasis,
    RejectionReason,
    Side,
    fmt_rupees,
)

log = logging.getLogger("tradedesk.plans")

_LOCK_REASONS = {RejectionReason.ANCHOR_ACTIVE, RejectionReason.CO_APPROVAL_REQUIRED}
_NOT_RUN = {PlanState.REQUOTE_REQUIRED, PlanState.EXPIRED, PlanState.VOID, PlanState.REJECTED}
_LEG_STATUS = {
    OrderStatus.PENDING: LegStatus.OPEN,
    OrderStatus.OPEN: LegStatus.OPEN,
    OrderStatus.PARTIAL: LegStatus.PARTIAL,
    OrderStatus.FILLED: LegStatus.FILLED,
    OrderStatus.REJECTED: LegStatus.REJECTED,
    OrderStatus.CANCELLED: LegStatus.CANCELLED,
    OrderStatus.UNKNOWN: LegStatus.UNKNOWN,
}


class PlanNotFound(Exception):
    pass


class PlanApprovalError(Exception):
    """The plan was NOT run. `code` says why."""

    def __init__(self, code: str, message: str, plan: Plan | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.plan = plan


@dataclass
class PlanProposal:
    status: str  # "plan_created" | "needs_clarification" | "not_found" | "blocked"
    reply: ChatReply
    message: str = ""
    plan: Plan | None = None
    candidates: list[Instrument] = field(default_factory=list)


class PlanService:
    def __init__(
        self,
        store: PlanStore,
        builder: PlanBuilder,
        orders: OrderBuilder,
        executor: Executor,
        broker: BrokerAdapter,
        audit: AuditLog,
        hub: EventHub,
        settings: Settings,
        clock: Callable[[], datetime],
        risk: RiskGuard | None = None,
    ):
        self._risk = risk or NoRiskGuard()
        self.cocaptain = None  # set by app/main.py
        self._store = store
        self._builder = builder
        self._orders = orders
        self._executor = executor
        self._broker = broker
        self._audit = audit
        self._hub = hub
        self._settings = settings
        self._clock = clock
        self._tasks: dict[str, asyncio.Task] = {}

    # ------------------------------------------------------------------ #
    # creating a plan card
    # ------------------------------------------------------------------ #

    async def propose(self, req: ProposePlanRequest) -> PlanProposal:
        try:
            plan = await self._builder.build(req)
        except PlanNeedsClarification as need:
            res = need.resolution
            if res.status == "ambiguous":
                names = ", ".join(c.name or c.symbol for c in res.candidates)
                text = f"For step {need.leg_index + 1}, which one do you mean by “{res.query}”? {names}"
                return PlanProposal(
                    "needs_clarification",
                    ChatReply(text=text, cards=[AmbiguityCard(query=res.query, candidates=res.candidates)]),
                    text,
                    candidates=res.candidates,
                )
            text = f"For step {need.leg_index + 1}, I couldn't find an instrument matching “{res.query}”."
            return PlanProposal("not_found", ChatReply(text=text, cards=[NoticeCard(level="warning", message=text)]), text)
        except OrderBlocked as blocked:
            kind = AuditKind.LOCK_BLOCKED if blocked.reason in _LOCK_REASONS else AuditKind.LIMIT_BLOCKED
            self._audit.record(kind, "system", blocked.message, data={"reason": blocked.reason.value})
            return PlanProposal(
                "blocked",
                ChatReply(text=blocked.message, cards=[NoticeCard(level="blocked", message=blocked.message)]),
                blocked.message,
            )

        checked = await self._with_risk_warnings(plan)
        if isinstance(checked, str):  # a hard limit the trader switched on: no plan card
            self._audit.record(AuditKind.LIMIT_BLOCKED, "system", checked, data={"reason": "RISK_LIMIT"})
            return PlanProposal("blocked", ChatReply(text=checked, cards=[NoticeCard(level="blocked", message=checked)]), checked)
        plan = checked

        self._store.put(plan)
        self._store.put_request(plan.id, req)
        self._store.put_report(self._initial_report(plan))
        self._hub.publish(PlanCreatedEvent, plan=plan)
        text = plan_readback(plan)
        self._audit.record(
            AuditKind.PENDING_CREATED,
            "system",
            f"Plan: {plan.title}",
            subject_id=plan.id,
            data={"plan_hash": plan.plan_hash, "steps": [leg.order.client_order_id for leg in plan.legs]},
        )
        return PlanProposal("plan_created", ChatReply(text=text, cards=[PlanCard(plan=plan)]), text, plan=plan)

    # ------------------------------------------------------------------ #
    # approving
    # ------------------------------------------------------------------ #

    async def approve(self, plan_id: str, plan_hash: str) -> PlanReport:
        plan = self._store.get(plan_id)
        if plan is None:
            raise PlanNotFound(plan_id)

        if plan.state is not PlanState.PENDING:
            self._refuse(plan, "NOT_PENDING", f"This plan is already {plan.state.value.lower().replace('_', ' ')}.")
        if self._clock() >= plan.expires_at:
            self._refuse(plan, "EXPIRED", "This plan expired. Ask again for a fresh one.", PlanState.EXPIRED)
        if not hmac.compare_digest(plan_hash, plan.plan_hash):
            self._refuse(
                plan,
                "HASH_MISMATCH",
                "What you approved doesn't match this plan, so it has been cancelled. Nothing was sent.",
                PlanState.VOID,
            )

        if self.cocaptain is not None:
            # A plan is several orders at once. Two-person approval of a whole plan is not built yet, so while the
            # trader is past their own limit AND has a Co-Captain, a plan is not approved at all (never a way round it).
            total = sum((leg.order.quantity or 0) * (leg.order.limit_price or leg.order.protection_price or leg.order.ref_ltp)
                        for leg in plan.legs)
            try:
                gate = await self.cocaptain.gate(orders=len(plan.legs), value=total)
            except BrokerTimeout:
                self._void(plan, "I couldn't reach the broker to double-check, so nothing was sent. Please try again.")
            if gate.blocked or gate.required:
                self._refuse(plan, "BLOCKED", gate.blocked or (
                    "You are past a limit you set and have a Co-Captain. A plan can't be approved by two people yet, "
                    "so please ask for single orders instead. Nothing was sent."))
        legs = [leg.model_copy(update={"order": leg.order.transition(PendingState.APPROVED)}) for leg in plan.legs]
        approved = self._store.put(plan.model_copy(update={"state": PlanState.APPROVED, "legs": legs}))  # claim: no await above
        self._hub.publish(PlanUpdatedEvent, plan=approved)
        self._audit.record(
            AuditKind.APPROVAL, "user", f"Approved plan: {approved.title}", subject_id=approved.id,
            data={"plan_hash": approved.plan_hash},
        )

        try:
            await self._recheck(approved)
        except OrderBlocked as exc:
            kind = AuditKind.LOCK_BLOCKED if exc.reason in _LOCK_REASONS else AuditKind.LIMIT_BLOCKED
            self._audit.record(kind, "system", exc.message, subject_id=approved.id)
            self._void(approved, exc.message)
        except BrokerTimeout:
            self._void(approved, "I couldn't reach the broker to double-check, so nothing was sent. Please try again.")

        report = self._store.put_report(self._store.report(approved.id).model_copy(update={"state": PlanState.APPROVED}))
        self._tasks[approved.id] = asyncio.create_task(self._run(approved))
        return report

    async def reject(self, plan_id: str) -> Plan:
        plan = self._store.get(plan_id)
        if plan is None:
            raise PlanNotFound(plan_id)
        if plan.state is not PlanState.PENDING:
            self._refuse(plan, "NOT_PENDING", f"This plan is already {plan.state.value.lower().replace('_', ' ')}.")
        rejected = self._set_state(plan, PlanState.REJECTED)
        self._audit.record(AuditKind.APPROVAL_REFUSED, "user", f"Trader declined plan: {plan.title}", subject_id=plan.id)
        return rejected

    async def _with_risk_warnings(self, plan: Plan) -> Plan | str:
        """Each step checked against the trader's own limits, counting the plan's earlier steps as orders.
        Returns the plan with warnings on its steps (warnings are not part of the plan hash), or the block message."""
        legs = []
        for leg in plan.legs:
            verdict = await self._risk.check(leg.order, "preview", extra_orders=leg.index)
            if verdict.block:
                return f"Step {leg.index + 1}: {verdict.block}"
            if verdict.warnings:
                leg = leg.model_copy(update={"order": leg.order.model_copy(update={"warnings": [*verdict.warnings, *leg.order.warnings]})})
            legs.append(leg)
        return plan.model_copy(update={"legs": legs})

    async def _recheck(self, plan: Plan) -> None:
        """Re-validate every step against the account as it is now."""
        for leg in plan.legs:  # limits can be crossed between the card and the click
            verdict = await self._risk.check(leg.order, "approve", extra_orders=leg.index)
            if verdict.block:
                raise OrderBlocked(RejectionReason.RISK_CHECK, f"Step {leg.index + 1}: {verdict.block}")
        locks = await self._broker.get_account_locks()
        holdings = await delivery_owned(self._broker)  # holdings + today's delivery buys
        worst_drift: tuple[float, int, int, int] | None = None
        for leg in plan.legs:
            o = leg.order
            price = o.limit_price or o.protection_price or o.ref_ltp
            try:
                check_locks(locks, OrderAction.PLACE, o.quantity * price)
                fresh = await self._broker.get_instrument(o.instrument.key)
                if fresh is not None:
                    check_instrument(fresh, self._orders.limits)
                if o.side is Side.SELL and o.product is Product.CNC:
                    held = sum(h.quantity for h in holdings if h.instrument.key == o.instrument.key)
                    if o.quantity > held:
                        raise OrderBlocked(
                            RejectionReason.INVALID_QUANTITY,
                            f"you now hold only {held} shares of {o.instrument.symbol}, not the {o.quantity} to be sold.",
                        )
            except OrderBlocked as exc:
                raise OrderBlocked(exc.reason, f"Step {leg.index + 1}: {exc.message}") from exc
            ltp = (await self._broker.get_quote(o.instrument.key)).ltp
            drift = abs(ltp - o.ref_ltp) * 100 / o.ref_ltp
            if drift > self._settings.drift_limit_pct and (worst_drift is None or drift > worst_drift[0]):
                worst_drift = (drift, leg.index, o.ref_ltp, ltp)
        if worst_drift is not None:
            await self._requote(plan, *worst_drift)

    async def _requote(self, plan: Plan, drift: float, index: int, old: int, new: int) -> NoReturn:
        request = self._store.request(plan.id)
        try:
            fresh = await self._builder.build(request)
        except (OrderBlocked, PlanNeedsClarification) as exc:
            self._void(plan, getattr(exc, "message", str(exc)))
        checked = await self._with_risk_warnings(fresh)
        if isinstance(checked, str):
            self._void(plan, checked)
        fresh = checked
        self._set_state(plan, PlanState.REQUOTE_REQUIRED)
        self._store.put(fresh)
        self._store.put_request(fresh.id, request)
        self._store.put_report(self._initial_report(fresh))
        self._hub.publish(PlanCreatedEvent, plan=fresh)
        symbol = plan.legs[index].order.instrument.symbol
        message = (
            f"The price of {symbol} moved from {fmt_rupees(old)} to {fmt_rupees(new)} ({drift:.1f}%) since you were "
            "shown this plan. Nothing was sent. Please check the updated plan and approve it again."
        )
        self._audit.record(
            AuditKind.APPROVAL_REFUSED, "system", f"Price drift {drift:.2f}% on {symbol}: plan re-quote required",
            subject_id=plan.id, data={"new_plan_id": fresh.id},
        )
        raise PlanApprovalError("REQUOTE_REQUIRED", message, fresh)

    # ------------------------------------------------------------------ #
    # running
    # ------------------------------------------------------------------ #

    async def join(self, plan_id: str) -> None:
        """Wait for a plan's run to finish (used by tests and shutdown)."""
        task = self._tasks.get(plan_id)
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    async def shutdown(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)

    def recover(self) -> list[str]:
        """At startup: a plan that was approved or running when the app stopped has lost its runner. It is marked
        HALTED, and its report says so. Steps already sent stay in the send log and are reconciled like any other
        order; steps not yet sent are never sent. Returns the ids of the plans it halted."""
        halted = []
        for plan in self._store.all():
            if plan.state not in (PlanState.APPROVED, PlanState.RUNNING) or plan.id in self._tasks:
                continue
            report = self._store.report(plan.id) or self._initial_report(plan)
            note = (" The app restarted while this plan was running, so it stopped there: steps not yet sent were "
                    "not sent, and any step already sent is in your order book.")
            self._store.put_report(PlanReport(
                plan_id=plan.id, state=PlanState.HALTED, legs=report.legs,
                summary=render_report(plan, PlanState.HALTED, report.legs) + note,
            ))
            self._set_state(plan, PlanState.HALTED)
            self._audit.record(AuditKind.PLAN_LEG_RESULT, "system", f"Plan halted by a restart: {plan.title}", subject_id=plan.id)
            halted.append(plan.id)
        return halted

    async def _run(self, plan: Plan) -> None:
        results = {r.index: r for r in self._store.report(plan.id).legs}
        sent: dict[int, Order] = {}
        stop_reason: str | None = None
        plan = self._set_state(plan, PlanState.RUNNING)
        self._audit.record(AuditKind.PLAN_STARTED, "system", f"Running plan: {plan.title}", subject_id=plan.id)
        try:
            for leg in plan.legs:
                i, label = leg.index, leg_label(leg)
                if stop_reason is not None:
                    results[i] = self._skipped(leg, f"{stop_reason}, and the plan stops at the first step that doesn't complete.")
                    self._leg_done(plan, results)
                    continue

                if leg.quantity_basis is QuantityBasis.FROM_PROCEEDS:
                    source = sent.get(leg.proceeds_from_leg)
                    if source is None or source.status is not OrderStatus.FILLED:
                        results[i] = self._skipped(
                            leg, f"it needs the money from step {leg.proceeds_from_leg + 1}, which did not completely fill."
                        )
                        if plan.on_leg_failure is LegFailurePolicy.HALT:
                            stop_reason = f"Step {leg.proceeds_from_leg + 1} did not complete"
                        self._leg_done(plan, results)
                        continue

                try:
                    await self._recheck_leg(leg)
                except OrderBlocked as exc:
                    results[i] = self._skipped(leg, exc.message)
                    if plan.on_leg_failure is LegFailurePolicy.HALT:
                        stop_reason = f"Step {i + 1} ({label}) was blocked"
                    self._leg_done(plan, results)
                    continue

                to_send = self._materialize(leg, sent)
                if to_send is None:
                    results[i] = self._skipped(leg, "the money from the earlier step is less than one share.")
                    if plan.on_leg_failure is LegFailurePolicy.HALT:
                        stop_reason = f"Step {i + 1} ({label}) could not be sized"
                    self._leg_done(plan, results)
                    continue

                result, order = await self._send_leg(leg, to_send)
                results[i] = result
                if order is not None:
                    sent[i] = order
                if result.status is not LegStatus.FILLED and plan.on_leg_failure is LegFailurePolicy.HALT:
                    stop_reason = f"Step {i + 1} ({label}) did not complete"
                self._leg_done(plan, results)
        except asyncio.CancelledError:
            for leg in plan.legs:
                if results[leg.index].status is LegStatus.NOT_SENT:
                    results[leg.index] = self._skipped(leg, "the app was shutting down.")
            self._finish(plan, results)
            raise
        except Exception:
            log.exception("plan %s failed", plan.id)
            for leg in plan.legs:
                if results[leg.index].status is LegStatus.NOT_SENT:
                    results[leg.index] = self._skipped(leg, "an unexpected error stopped the plan.")
            self._finish(plan, results)
            return
        self._finish(plan, results)

    async def _recheck_leg(self, leg: PlanLeg) -> None:
        """Between steps: has an Anchor/Co-Captain lock come on, or has the stock been suspended?"""
        o = leg.order
        price = o.limit_price or o.protection_price or o.ref_ltp
        try:
            check_locks(await self._broker.get_account_locks(), OrderAction.PLACE, o.quantity * price)
            fresh = await self._broker.get_instrument(o.instrument.key)
            if fresh is not None:
                check_instrument(fresh, self._orders.limits)
        except BrokerTimeout:
            raise OrderBlocked(RejectionReason.OTHER, "I couldn't reach the broker to check this step, so it was not sent.")

    @staticmethod
    def _materialize(leg: PlanLeg, sent: dict[int, Order]) -> PendingOrder | None:
        """The order to send now. A funded buy is sized from the sale's actual proceeds, never above the caps."""
        if leg.quantity_basis is QuantityBasis.FIXED:
            return leg.order
        source = sent[leg.proceeds_from_leg]
        sale = compute_charges(
            exchange=source.instrument.exchange, side=Side.SELL, product=source.product,
            quantity=source.filled_quantity, price=source.avg_fill_price, option=source.instrument.is_option, future=source.instrument.is_future,
        )
        net = source.filled_quantity * source.avg_fill_price - sale.total
        price = leg.order.limit_price or leg.order.protection_price
        quantity = min(leg.max_quantity, min(net, leg.max_spend) // price)
        return leg.order.model_copy(update={"quantity": quantity}) if quantity >= 1 else None

    async def _send_leg(self, leg: PlanLeg, to_send: PendingOrder) -> tuple[PlanLegResult, Order | None]:
        label = leg_label(leg)
        try:
            outcome = await self._executor.execute(to_send)
        except DuplicateExecution:
            return (
                PlanLegResult(index=leg.index, label=label, status=LegStatus.UNKNOWN, requested_quantity=to_send.quantity,
                              message="This step's order id has already been used."),
                None,
            )
        order = outcome.order
        if outcome.outcome == "SENT" and order is not None:
            order = await self._await_fill(order)
        if order is None:  # UNKNOWN: timed out and not found in the order book; never re-sent
            return (
                PlanLegResult(index=leg.index, label=label, status=LegStatus.UNKNOWN, requested_quantity=to_send.quantity,
                              message=outcome.message),
                None,
            )
        return self._result_from_order(leg, label, to_send.quantity, order), order

    async def _await_fill(self, order: Order) -> Order:
        """Give a just-sent step a moment to fill; report whatever it is at the end."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._settings.plan_fill_timeout_seconds
        while order.status in (OrderStatus.PENDING, OrderStatus.OPEN, OrderStatus.PARTIAL) and loop.time() < deadline:
            await asyncio.sleep(self._settings.plan_poll_interval)
            try:
                fresh = await self._broker.get_order(order.order_id)
            except BrokerTimeout:
                break
            if fresh is not None:
                order = fresh
        return order

    @staticmethod
    def _result_from_order(leg: PlanLeg, label: str, requested: int, order: Order) -> PlanLegResult:
        return PlanLegResult(
            index=leg.index,
            label=label,
            status=_LEG_STATUS[order.status],
            requested_quantity=order.quantity if order.quantity else requested,
            filled_quantity=order.filled_quantity,
            avg_fill_price=order.avg_fill_price,
            rejection_reason=order.rejection_reason,
            message=(order.rejection_message or "") if order.status is OrderStatus.REJECTED else "",
        )

    # ------------------------------------------------------------------ #
    # reports
    # ------------------------------------------------------------------ #

    def _initial_report(self, plan: Plan) -> PlanReport:
        legs = [
            PlanLegResult(index=leg.index, label=leg_label(leg), status=LegStatus.NOT_SENT, requested_quantity=leg.order.quantity)
            for leg in plan.legs
        ]
        return PlanReport(plan_id=plan.id, state=plan.state, legs=legs, summary=render_report(plan, plan.state, legs))

    @staticmethod
    def _skipped(leg: PlanLeg, why: str) -> PlanLegResult:
        return PlanLegResult(
            index=leg.index, label=leg_label(leg), status=LegStatus.SKIPPED,
            requested_quantity=leg.order.quantity, message=why[0].upper() + why[1:] if why else "",
        )

    def _leg_done(self, plan: Plan, results: dict[int, PlanLegResult]) -> None:
        ordered = [results[leg.index] for leg in plan.legs]
        last = next((r for r in reversed(ordered) if r.status is not LegStatus.NOT_SENT), None)
        if last is not None:
            self._audit.record(
                AuditKind.PLAN_LEG_RESULT, "system", f"Step {last.index + 1}: {last.label} -> {last.status.value}",
                subject_id=plan.id, data={"filled": last.filled_quantity, "requested": last.requested_quantity},
            )
        report = self._store.put_report(
            PlanReport(plan_id=plan.id, state=PlanState.RUNNING, legs=ordered, summary=render_report(plan, PlanState.RUNNING, ordered))
        )
        self._hub.publish(PlanReportUpdateEvent, report=report)

    def _finish(self, plan: Plan, results: dict[int, PlanLegResult]) -> None:
        ordered = [results[leg.index] for leg in plan.legs]
        skipped = any(r.status in (LegStatus.SKIPPED, LegStatus.NOT_SENT, LegStatus.UNKNOWN) for r in ordered)
        state = PlanState.HALTED if skipped else PlanState.COMPLETED
        report = self._store.put_report(PlanReport(plan_id=plan.id, state=state, legs=ordered, summary=render_report(plan, state, ordered)))
        self._set_state(plan, state)
        self._hub.publish(PlanReportUpdateEvent, report=report)

    async def report(self, plan_id: str) -> PlanReport:
        """The plan's report, with any step that was still open refreshed from the broker."""
        plan, report = self._store.get(plan_id), self._store.report(plan_id)
        if plan is None or report is None:
            raise PlanNotFound(plan_id)
        if plan.state in (PlanState.PENDING, PlanState.APPROVED, PlanState.RUNNING):
            return report
        legs, changed = list(report.legs), False
        for r in report.legs:
            if r.status in (LegStatus.OPEN, LegStatus.PARTIAL):
                try:
                    fresh = await self._executor.order_for(plan.legs[r.index].order.client_order_id)
                except BrokerTimeout:
                    break
                if fresh is not None and (_LEG_STATUS[fresh.status], fresh.filled_quantity) != (r.status, r.filled_quantity):
                    legs[r.index] = self._result_from_order(plan.legs[r.index], r.label, r.requested_quantity, fresh)
                    changed = True
        if changed:
            report = self._store.put_report(
                PlanReport(plan_id=plan_id, state=report.state, legs=legs, summary=render_report(plan, report.state, legs))
            )
            self._hub.publish(PlanReportUpdateEvent, report=report)
        return report

    def latest_plan(self) -> Plan | None:
        return self._store.latest()

    # ------------------------------------------------------------------ #

    def _set_state(self, plan: Plan, state: PlanState) -> Plan:
        updated = self._store.put(self._store.get(plan.id).model_copy(update={"state": state}))
        self._hub.publish(PlanUpdatedEvent, plan=updated)
        report = self._store.report(plan.id)
        if report is not None and state in _NOT_RUN:  # keep the report's wording true to the plan's state
            fresh = self._store.put_report(
                PlanReport(plan_id=plan.id, state=state, legs=report.legs, summary=render_report(updated, state, report.legs))
            )
            self._hub.publish(PlanReportUpdateEvent, report=fresh)
        return updated

    def _refuse(self, plan: Plan, code: str, message: str, new_state: PlanState | None = None) -> NoReturn:
        if new_state is not None:
            self._set_state(plan, new_state)
        self._audit.record(
            AuditKind.APPROVAL_REFUSED, "system", f"{code}: {message}", subject_id=plan.id, data={"code": code}
        )
        raise PlanApprovalError(code, message)

    def _void(self, plan: Plan, message: str, code: str = "BLOCKED") -> NoReturn:
        self._set_state(plan, PlanState.VOID)
        self._audit.record(
            AuditKind.APPROVAL_REFUSED, "system", f"{code}: {message}", subject_id=plan.id, data={"code": code}
        )
        raise PlanApprovalError(code, message)


class PlanAssistant:
    """The only part of the plan machinery the assistant (LLM) can reach: draft a plan card and read
    reports. It has no way to approve or run anything."""

    def __init__(self, service: PlanService):
        self._service = service

    async def propose(self, req: ProposePlanRequest) -> PlanProposal:
        return await self._service.propose(req)

    async def report(self, plan_id: str | None = None) -> PlanReport | None:
        plan = self._service._store.get(plan_id) if plan_id else self._service.latest_plan()
        return await self._service.report(plan.id) if plan else None
