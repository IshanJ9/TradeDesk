"""The trader's own allowances. Strict > boundaries match risk/engine.py."""

from dataclasses import dataclass

from app.risk.models import RiskProfile, TodayFacts
from app.schemas import Model, OrderAction, PendingOrder, Plan, fmt_rupees

POLICY_VERSION = "zone-v1"


@dataclass(frozen=True)
class ZoneLimits:
    max_orders_per_day: int | None = None
    daily_turnover_limit_paise: int | None = None
    short_window_order_limit: int | None = None


class ZoneDecision(Model):
    in_zone: bool
    configured: bool
    reasons: list[str]
    proposed_orders: int
    proposed_value_paise: int
    policy_version: str = POLICY_VERSION


def proposal_totals(proposed: PendingOrder | Plan) -> tuple[int, int]:
    orders = [leg.order for leg in proposed.legs] if isinstance(proposed, Plan) else [proposed]
    new_orders = [order for order in orders if order.action == OrderAction.PLACE]
    return len(new_orders), sum(
        (order.quantity or 0) * (order.limit_price or order.protection_price or order.ref_ltp)
        for order in new_orders
    )


def in_overtrading_zone(
    profile: RiskProfile | ZoneLimits | None, facts: TodayFacts, proposed: PendingOrder | Plan,
) -> ZoneDecision:
    limits = profile or ZoneLimits()
    count, value = proposal_totals(proposed)
    configured = any(limit is not None for limit in (
        limits.max_orders_per_day, limits.daily_turnover_limit_paise, limits.short_window_order_limit,
    ))
    reasons = []
    if count:  # modifying/cancelling an existing order never requires another person's approval
        daily = facts.orders_today + count
        if limits.max_orders_per_day is not None and daily > limits.max_orders_per_day:
            reasons.append(f"You set {limits.max_orders_per_day} orders a day; including this proposal, the count would be {daily}.")
        turnover = facts.turnover + value
        if limits.daily_turnover_limit_paise is not None and turnover > limits.daily_turnover_limit_paise:
            reasons.append(
                f"You set a daily turnover allowance of {fmt_rupees(limits.daily_turnover_limit_paise)}; "
                f"filled turnover plus this proposal is {fmt_rupees(turnover)}. Other unfilled orders are excluded."
            )
        recent = facts.recent_orders + count
        if limits.short_window_order_limit is not None and recent > limits.short_window_order_limit:
            reasons.append(f"You set {limits.short_window_order_limit} orders in 20 minutes; including this proposal, the count would be {recent}.")
    return ZoneDecision(in_zone=bool(reasons), configured=configured, reasons=reasons,
                        proposed_orders=count, proposed_value_paise=value)
