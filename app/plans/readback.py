"""Plain-English wording for a plan and for what happened to it. Written by code, never the model."""

from app.orders.readback import readback
from app.schemas import (
    LegFailurePolicy,
    LegStatus,
    Plan,
    PlanLeg,
    PlanLegResult,
    PlanReport,
    PlanState,
    QuantityBasis,
    Side,
    fmt_rupees,
)


def leg_label(leg: PlanLeg) -> str:
    """Short name for a step, using symbols only: 'Sell 10 INFY', 'Buy about 95 ITC'."""
    o = leg.order
    about = "about " if leg.quantity_basis is QuantityBasis.FROM_PROCEEDS else ""
    return f"{o.side.value.capitalize()} {about}{o.quantity} {o.instrument.symbol}"


def leg_text(leg: PlanLeg) -> str:
    o = leg.order
    if leg.quantity_basis is QuantityBasis.FIXED:
        return readback(o)
    name = f"{o.instrument.name or o.instrument.symbol} ({o.instrument.exchange.value})"
    if o.limit_price is not None:
        price = f"at up to {fmt_rupees(o.limit_price)}"
    else:
        price = f"at the market price, protected so it never fills above {fmt_rupees(o.protection_price)}"
    return (
        f"You are buying about {o.quantity} shares of {name} {price}, paid for with the money from step "
        f"{leg.proceeds_from_leg + 1} (never more than {leg.max_quantity} shares or {fmt_rupees(leg.max_spend)})."
    )


def plan_readback(plan: Plan) -> str:
    steps = "\n".join(f"{leg.index + 1}. {leg_text(leg)}" for leg in plan.legs)
    after = (
        "If a step is rejected or doesn't complete, the later steps are not sent."
        if plan.on_leg_failure is LegFailurePolicy.HALT
        else "If a step fails, the others are still attempted, except any that need its money."
    )
    return (
        f"{plan.title}: {len(plan.legs)} steps, approved together and run in order.\n{steps}\n"
        f"Nothing is sent until you approve the whole plan. {after}"
    )


def leg_status_text(r: PlanLegResult) -> str:
    avg = fmt_rupees(r.avg_fill_price) if r.avg_fill_price else "-"
    match r.status:
        case LegStatus.FILLED:
            return f"{r.label} - filled {r.filled_quantity}/{r.requested_quantity} at {avg}"
        case LegStatus.PARTIAL:
            return (
                f"{r.label} - partly filled {r.filled_quantity}/{r.requested_quantity} at an average of {avg}; "
                f"{r.pending_quantity} still pending"
            )
        case LegStatus.OPEN:
            return f"{r.label} - sent but not filled yet ({r.pending_quantity} pending)"
        case LegStatus.REJECTED:
            return f"{r.label} - rejected: {r.message or (r.rejection_reason.value if r.rejection_reason else 'no reason given')}"
        case LegStatus.CANCELLED:
            return f"{r.label} - cancelled ({r.filled_quantity}/{r.requested_quantity} had filled)"
        case LegStatus.UNKNOWN:
            return f"{r.label} - outcome unknown. It has NOT been re-sent; check your order book."
        case _:  # SKIPPED / NOT_SENT
            return f"{r.label} - not sent{': ' + r.message if r.message else ''}"


def render_report(plan: Plan, state: PlanState, legs: list[PlanLegResult]) -> str:
    lines = [f"Step {r.index + 1}: {leg_status_text(r)}" for r in legs]
    if state in (PlanState.APPROVED, PlanState.RUNNING):
        head = f"{plan.title}: in progress."
    elif state is PlanState.PENDING:
        head = f"{plan.title}: waiting for your approval. Nothing has been sent."
    elif state in (PlanState.REQUOTE_REQUIRED, PlanState.EXPIRED, PlanState.VOID, PlanState.REJECTED):
        head = f"{plan.title}: not run. Nothing was sent."
    elif all(r.status is LegStatus.FILLED for r in legs):
        head = f"{plan.title}: all {len(legs)} steps filled."
    else:
        head = f"{plan.title}: finished, but not every step filled."
    return head + "\n" + "\n".join(lines)
