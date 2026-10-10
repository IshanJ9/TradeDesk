"""Request/response and WebSocket message types for the REST + WebSocket contract.

These sit on top of app/schemas.py. FastAPI exports them to OpenAPI and
`scripts/gen_types.py` turns that into frontend/src/lib/types.gen.ts, so the frontend
never hand-writes a type.
"""

from typing import Annotated, Literal, Union

from pydantic import Field, model_validator

from app.schemas import (
    AccountLocks,
    AuditEvent,
    Funds,
    Holding,
    Instrument,
    LegFailurePolicy,
    Model,
    Comparator,
    Order,
    OrderType,
    PendingOrder,
    Plan,
    PlanReport,
    Position,
    Product,
    Rule,
    RuleBasis,
    RuleKind,
    Side,
    Tick,
    Validity,
)

# --------------------------------------------------------------------------- #
# REST
# --------------------------------------------------------------------------- #


class AccountSnapshot(Model):
    funds: Funds
    holdings: list[Holding]
    positions: list[Position]
    locks: AccountLocks


class PendingList(Model):
    orders: list[PendingOrder]
    plans: list[Plan]


class ApproveRequest(Model):
    """The client must echo the hash of the exact card it is approving."""

    order_hash: str = Field(min_length=64, max_length=64)
    # Typed by the trader on cards whose risk_ack_required is set; the server checks it, not just the screen.
    acknowledgment: str | None = Field(default=None, max_length=40)


class ApprovalConflict(Model):
    """Body of a 409 from the approve route. Nothing was sent."""

    code: Literal["HASH_MISMATCH", "EXPIRED", "NOT_PENDING", "REQUOTE_REQUIRED", "BLOCKED", "ACK_REQUIRED"]
    message: str
    # Set for REQUOTE_REQUIRED: the fresh card the trader must look at again.
    pending: PendingOrder | None = None
    # Same, when the thing being approved was a plan.
    plan: Plan | None = None


class PlanApproveRequest(Model):
    """The client must echo the hash of the exact plan it is approving (every step, cap and policy)."""

    plan_hash: str = Field(min_length=64, max_length=64)


class PlanLegRequest(Model):
    """One step of a plan, in the units a trader uses. Give exactly ONE way to size it."""

    instrument: str = Field(min_length=1, max_length=60, description="The stock, as the trader said it")
    side: Side
    quantity: int | None = Field(default=None, gt=0, description="A number of shares")
    amount_rupees: float | None = Field(default=None, gt=0, description="A rupee amount to trade")
    fraction_of_holding: float | None = Field(
        default=None, gt=0, le=1, description="SELL only: a fraction of the shares held. 'half my Infosys' -> 0.5, 'all' -> 1"
    )
    proceeds_of_leg: int | None = Field(
        default=None, ge=0, description="BUY only: spend the money raised by this earlier SELL step (0 = the first step)"
    )
    order_type: OrderType | None = Field(default=None, description="LIMIT if the trader gave a price, otherwise MARKET")
    limit_price_rupees: float | None = Field(default=None, gt=0)
    product: Product = Product.CNC
    validity: Validity = Validity.DAY

    @model_validator(mode="after")
    def _one_size(self) -> "PlanLegRequest":
        sizes = [self.quantity, self.amount_rupees, self.fraction_of_holding, self.proceeds_of_leg]
        if sum(s is not None for s in sizes) != 1:
            raise ValueError("size each step with exactly one of quantity, amount_rupees, fraction_of_holding, proceeds_of_leg")
        if self.fraction_of_holding is not None and self.side is not Side.SELL:
            raise ValueError("fraction_of_holding only applies to a SELL step")
        if self.proceeds_of_leg is not None and self.side is not Side.BUY:
            raise ValueError("proceeds_of_leg only applies to a BUY step")
        return self


class ProposePlanRequest(Model):
    """Several orders approved together as one plan, e.g. 'sell half my Infosys and buy ITC with the money'.

    Steps run in order. Nothing is sent until the trader approves the whole plan, and by default
    a step that does not complete stops the rest.
    """

    title: str | None = Field(default=None, max_length=120)
    legs: list[PlanLegRequest] = Field(min_length=2, max_length=6)
    on_leg_failure: LegFailurePolicy = LegFailurePolicy.HALT

    @model_validator(mode="after")
    def _references(self) -> "ProposePlanRequest":
        for i, leg in enumerate(self.legs):
            j = leg.proceeds_of_leg
            if j is not None:
                if j >= i:
                    raise ValueError(f"step {i + 1} can only use the proceeds of an earlier step")
                if self.legs[j].side is not Side.SELL:
                    raise ValueError(f"step {i + 1} uses the proceeds of step {j + 1}, which is not a sale")
        return self


class ExecutionResult(Model):
    """What happened after an approved card was sent.

    SENT      the broker accepted it (see `order` for its live status)
    REJECTED  the broker refused it (`order.rejection_reason` says why)
    UNKNOWN   the request timed out and we could not confirm either way. The order is
              never re-sent; it is reconciled against the broker's order book.
    """

    pending: PendingOrder
    outcome: Literal["SENT", "REJECTED", "UNKNOWN"]
    order: Order | None = None
    message: str = ""


class ReconcileResult(Model):
    resolved: int  # executions whose outcome became known on this pass
    unresolved: int  # executions still waiting (timed-out and not yet found at the broker)


class CreateRuleRequest(Model):
    """A standing instruction, in the units a trader uses (rupees, percent).

    Give the trigger as EITHER an absolute `price_rupees` OR a `percent` move measured from
    `basis`. A TRIGGER_ORDER rule never sends anything when it fires: it prepares a fresh order
    card for the trader to approve. An ALERT only tells the trader.
    """

    kind: RuleKind
    instrument: str = Field(min_length=1, max_length=60, description="The stock, as the trader said it")
    comparator: Comparator = Field(description="BELOW: fires when the price falls under the trigger. ABOVE: rises over it")
    price_rupees: float | None = Field(default=None, gt=0, description="Absolute trigger price")
    percent: float | None = Field(default=None, gt=0, le=100, description="Size of the move as a positive number, e.g. 3 for '3%'")
    basis: RuleBasis | None = Field(default=None, description="With percent: AVG_BUY (their buy price), PREV_CLOSE, or AT_CREATION (default)")
    # the order to prepare when a TRIGGER_ORDER rule fires
    side: Side | None = None
    quantity: int | None = Field(default=None, gt=0)
    amount_rupees: float | None = Field(default=None, gt=0)
    order_type: OrderType | None = None
    limit_price_rupees: float | None = Field(default=None, gt=0)
    product: Product = Product.CNC
    validity: Validity = Validity.DAY

    @model_validator(mode="after")
    def _check(self) -> "CreateRuleRequest":
        if (self.price_rupees is None) == (self.percent is None):
            raise ValueError("give exactly one of price_rupees or percent")
        if self.basis is not None and self.percent is None:
            raise ValueError("basis only applies with percent")
        if self.basis is RuleBasis.ABSOLUTE:
            raise ValueError("use price_rupees for an absolute trigger")
        order_fields = (self.side, self.quantity, self.amount_rupees, self.order_type, self.limit_price_rupees)
        if self.kind is RuleKind.ALERT:
            if any(f is not None for f in order_fields):
                raise ValueError("an ALERT does not take order fields")
        else:
            if self.side is None:
                raise ValueError("TRIGGER_ORDER needs side")
            if (self.quantity is None) == (self.amount_rupees is None):
                raise ValueError("TRIGGER_ORDER needs exactly one of quantity or amount_rupees")
        return self


class ChatRequest(Model):
    message: str = Field(min_length=1, max_length=2000)
    # True when the text came from the microphone. Futures and written options are not started that way.
    via_voice: bool = False


class PendingOrderCard(Model):
    type: Literal["pending_order"] = "pending_order"
    pending: PendingOrder


class PlanCard(Model):
    type: Literal["plan"] = "plan"
    plan: Plan


class RuleCard(Model):
    type: Literal["rule"] = "rule"
    rule: Rule


class AmbiguityCard(Model):
    """'Which Tata?': the trader must pick; the copilot never guesses."""

    type: Literal["ambiguity"] = "ambiguity"
    query: str
    candidates: list[Instrument]


class NoticeCard(Model):
    """Something the trader should know: a block, a lock, or flagged content."""

    type: Literal["notice"] = "notice"
    level: Literal["info", "warning", "blocked"]
    message: str


Card = Annotated[
    Union[PendingOrderCard, PlanCard, RuleCard, AmbiguityCard, NoticeCard],
    Field(discriminator="type"),
]


class ChatReply(Model):
    text: str
    cards: list[Card] = Field(default_factory=list)


class ChaosStatus(Model):
    network_down: bool = False
    market_open: bool = True


# --------------------------------------------------------------------------- #
# WebSocket (server -> client only; every money action goes through REST)
# --------------------------------------------------------------------------- #


class _Event(Model):
    seq: int  # increasing per user; a gap means the client should refetch a snapshot
    # Who the event is for. Set by the hub, used for delivery, never sent to the client.
    user_id: str | None = Field(default=None, exclude=True)


class SnapshotEvent(_Event):
    type: Literal["snapshot"] = "snapshot"
    account: AccountSnapshot
    pending: PendingList
    orders: list[Order]
    rules: list[Rule]


class TickEvent(_Event):
    type: Literal["tick"] = "tick"
    tick: Tick


class AccountUpdateEvent(_Event):
    type: Literal["account_update"] = "account_update"
    account: AccountSnapshot


class OrderUpdateEvent(_Event):
    type: Literal["order_update"] = "order_update"
    order: Order


class PendingCreatedEvent(_Event):
    type: Literal["pending_created"] = "pending_created"
    pending: PendingOrder


class PendingUpdatedEvent(_Event):
    type: Literal["pending_updated"] = "pending_updated"
    pending: PendingOrder


class PlanCreatedEvent(_Event):
    type: Literal["plan_created"] = "plan_created"
    plan: Plan


class PlanUpdatedEvent(_Event):
    """A plan changed state (approved, running, completed, halted, expired, voided, re-quoted)."""

    type: Literal["plan_updated"] = "plan_updated"
    plan: Plan


class PlanReportUpdateEvent(_Event):
    """Progress of an approved plan: sent after every step finishes."""

    type: Literal["plan_report_update"] = "plan_report_update"
    report: PlanReport


class RuleUpdateEvent(_Event):
    type: Literal["rule_update"] = "rule_update"
    rule: Rule


class RuleFiredEvent(_Event):
    """A standing instruction just triggered. Show it prominently (toast + the card, if any)."""

    type: Literal["rule_fired"] = "rule_fired"
    rule: Rule
    message: str
    ltp: int  # the price that triggered it, in paise
    pending: PendingOrder | None = None  # the approval card, for a TRIGGER_ORDER rule that could be prepared


class LockUpdateEvent(_Event):
    type: Literal["lock_update"] = "lock_update"
    locks: AccountLocks


class AuditEventMessage(_Event):
    type: Literal["audit_event"] = "audit_event"
    event: AuditEvent


class ChaosStatusEvent(_Event):
    type: Literal["chaos_status"] = "chaos_status"
    status: ChaosStatus


class TraceEvent(_Event):
    """One step of the assistant's work, for the live activity panel (see app/trace.py).

    `detail` is a short sentence for people. It never holds keys, passwords or raw broker replies."""

    type: Literal["trace"] = "trace"
    run_id: str  # one chat message = one run
    node: str  # "input_guard", "router", "tool:propose_order", "output_guard", ...
    kind: Literal["node", "tool", "guard"]
    status: Literal["start", "end", "blocked", "error"]
    detail: str = ""
    ms: int | None = None  # how long the step took; set on end, blocked and error


class ExternalOrderEvent(_Event):
    """An order in the broker's book that this app did not send (placed in 021's own app, for example)."""

    type: Literal["external_order"] = "external_order"
    order: Order


class DisciplineSummary(Model):
    """Today's trading against the trader's OWN limits. Facts only: no advice, no predictions."""

    orders_today: int = 0
    order_limit: int | None = None  # None = the trader set no limit
    risk_score: int | None = None  # 0-100 for today; None until the trader has a profile
    average_score: int | None = None  # the trader's own recent average, for comparison
    warnings: list[str] = Field(default_factory=list)


class DisciplineUpdateEvent(_Event):
    type: Literal["discipline_update"] = "discipline_update"
    summary: DisciplineSummary


WsEvent = Annotated[
    Union[
        SnapshotEvent,
        TickEvent,
        AccountUpdateEvent,
        OrderUpdateEvent,
        PendingCreatedEvent,
        PendingUpdatedEvent,
        PlanCreatedEvent,
        PlanUpdatedEvent,
        PlanReportUpdateEvent,
        RuleUpdateEvent,
        RuleFiredEvent,
        LockUpdateEvent,
        AuditEventMessage,
        ChaosStatusEvent,
        TraceEvent,
        ExternalOrderEvent,
        DisciplineUpdateEvent,
    ],
    Field(discriminator="type"),
]
