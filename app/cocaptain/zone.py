"""The "overtrading zone": when an order needs a second person (the trader's Co-Captain) as well as the trader.

Pure arithmetic over limits the trader set themselves (app/risk/models.py RiskProfile) and today's activity facts.
No model, no network. The comparisons are the SAME ones app/risk/engine.py uses for its "You set ..." warnings
(strictly greater than the limit, the proposed order counted); tests/test_cocaptain_zone.py checks the two agree.

A limit that is not configured is ignored. With no profile at all the zone is never entered.
"""

from dataclasses import dataclass

from app.risk.models import RiskProfile, TodayFacts
from app.schemas import fmt_rupees

POLICY_VERSION = "zone-v1"  # stored with each approval: an approval is bound to the rule it was given under


@dataclass(frozen=True)
class ZoneDecision:
    in_zone: bool
    reasons: tuple[str, ...] = ()  # the trader's own numbers, in plain words; never advice
    configured: bool = False  # False: no profile, so no limit can be crossed
    policy_version: str = POLICY_VERSION


def in_overtrading_zone(profile: RiskProfile | None, facts: TodayFacts, *, orders: int, value: int) -> ZoneDecision:
    """`orders` and `value` describe what is being approved: 1 order and its value, or a plan's step count and its
    total value. They are counted on top of today's activity."""
    if profile is None:
        return ZoneDecision(in_zone=False)
    reasons: list[str] = []
    count = facts.orders_today + orders
    if count > profile.max_orders_per_day:
        per_day = f"{profile.max_orders_per_day} order a day" if profile.max_orders_per_day == 1 else f"{profile.max_orders_per_day} orders a day"
        reasons.append(f"You set {per_day}; this would take you to {count}.")
    if profile.short_window_order_limit is not None:
        recent = facts.recent_orders + orders
        if recent > profile.short_window_order_limit:
            reasons.append(f"You set a 20-minute allowance of {profile.short_window_order_limit} orders; this would make {recent}.")
    if profile.daily_turnover_limit_paise is not None and facts.turnover + value > profile.daily_turnover_limit_paise:
        reasons.append(f"You set a daily turnover allowance of {fmt_rupees(profile.daily_turnover_limit_paise)}; "
                       f"filled turnover is {fmt_rupees(facts.turnover)} and this adds {fmt_rupees(value)}.")
    return ZoneDecision(in_zone=bool(reasons), reasons=tuple(reasons), configured=True)
