"""The trader's chosen limits. This guard only reads snapshots and returns a verdict."""

from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal

from app.broker.base import ReadOnlyBroker
from app.risk.guard import RiskVerdict, Stage
from app.risk.models import Goal, RiskProfile
from app.risk.store import ProfileStore
from app.risk.today import TodayFacts, compute_today, percentage
from app.schemas import OrderAction, PendingOrder, Product, Side, fmt_rupees


def orders_a_day(n: int) -> str:
    return f"{n} order a day" if n == 1 else f"{n} orders a day"


def within_window(now: datetime, last: datetime | None, minutes: int) -> bool:
    return last is not None and timedelta(0) <= now - last < timedelta(minutes=minutes)


def evaluate(pending: PendingOrder, stage: Stage, profile: RiskProfile,
             facts: TodayFacts, goal: Goal | None, now: datetime, extra_orders: int = 0) -> RiskVerdict:
    if pending.action == OrderAction.CANCEL:
        return RiskVerdict()
    placing = pending.action == OrderAction.PLACE
    # extra_orders: earlier steps of the same plan, which will be placed before this one
    count = facts.orders_today + extra_orders + int(placing)
    over_orders = count > profile.max_orders_per_day
    # Decimal preserves exact percentage boundaries for integer-paise amounts.
    loss = max(0, -facts.pnl_after_charges)
    limit = Decimal(facts.portfolio_value) * Decimal(str(profile.daily_loss_limit_pct)) / 100
    reached_loss = facts.portfolio_value > 0 and loss >= limit
    block = None
    if placing and profile.hard_order_limit and over_orders:
        block = (f"You switched on a hard limit of {profile.max_orders_per_day} orders a day. "
                 "You can change it in Discipline.")
    elif placing and profile.hard_stop_on_daily_loss and reached_loss:
        block = (f"You switched on a hard daily loss limit of {profile.daily_loss_limit_pct:g}% of your portfolio. "
                 "Today's estimated loss after charges has reached it. You can change it in Discipline.")
    if stage == "approve":
        return RiskVerdict(block=block)
    warnings = []
    if over_orders:
        warnings.append(f"You set {orders_a_day(profile.max_orders_per_day)}; "
                        + (f"this would be order #{count}." if placing else f"you already have {count} today. A modification adds no order."))
    price = pending.limit_price or pending.protection_price or pending.ref_ltp
    value = (pending.quantity or 0) * price
    portfolio = facts.portfolio_value
    if portfolio > 0:
        if Decimal(value) * 100 > Decimal(portfolio) * Decimal(str(profile.max_order_pct)):
            warnings.append(f"You set a {profile.max_order_pct:g}% single-order limit; "
                            f"this order is approximately {percentage(value, portfolio):.2f}% of your portfolio.")
        stock_value = facts.stock_values.get(pending.instrument.key, 0) + value
        if (pending.side == Side.BUY and
                Decimal(stock_value) * 100 > Decimal(portfolio) * Decimal(str(profile.max_stock_pct))):
            warnings.append(f"You set a {profile.max_stock_pct:g}% stock-weight limit; "
                            f"{pending.instrument.symbol} could reach approximately {percentage(stock_value, portfolio):.2f}% "
                            "gross exposure if filled. This estimate adds the proposed buy to current exposure.")
        if reached_loss:
            warnings.append(f"You set a {profile.daily_loss_limit_pct:g}% daily loss limit; "
                            f"today's estimated loss after charges ({fmt_rupees(loss)}) has reached it.")
        elif Decimal(loss) >= limit * Decimal("0.8"):
            warnings.append(f"You set a {profile.daily_loss_limit_pct:g}% daily loss limit; "
                            f"today's estimated loss after charges ({fmt_rupees(loss)}) is close to your limit (at least 80%).")
    else:
        warnings.append("Portfolio value is not positive; percentage-based risk checks are unavailable.")
    if (facts.consecutive_losses >= profile.cooling_off_after_losses and
            within_window(now, facts.last_loss_at, profile.cooling_off_minutes)):
        warnings.append(f"You set a {profile.cooling_off_minutes}-minute cooling-off window after "
                        f"{profile.cooling_off_after_losses} consecutive losses; you are inside that window.")
    if (pending.side == Side.BUY and within_window(
            now, facts.last_loss_by_stock.get(pending.instrument.key), profile.reentry_minutes)):
        warnings.append(f"You set a {profile.reentry_minutes}-minute re-entry window; "
                        f"you closed {pending.instrument.symbol} at an estimated loss inside that window.")
    if pending.product == Product.MIS and not profile.intraday_allowed:
        warnings.append("You set intraday trading to off; this is an MIS order.")
    if goal is not None and goal.start_date <= facts.day <= goal.end_date:
        # The plan explicitly asks for today's loss PLUS loss since the goal began.
        # Disclose overlap: this is a conservative warning metric, not actual net loss.
        goal_loss = max(0, goal.start_value - portfolio)
        combined = loss + goal_loss
        if Decimal(combined) >= Decimal(goal.max_acceptable_loss_paise) * Decimal("0.8"):
            warnings.append(f"You set a goal loss allowance of {fmt_rupees(goal.max_acceptable_loss_paise)}; "
                            f"today's loss plus the decline since the goal started is {fmt_rupees(combined)} "
                            "(at least 80% of your allowance). These periods can overlap; this is not net loss.")
    return RiskVerdict(warnings=tuple(warnings), block=block)


class ProfileGuard:
    def __init__(self, broker: ReadOnlyBroker, store: ProfileStore, clock: Callable[[], datetime]):
        self._broker, self._store, self._clock = broker, store, clock

    async def check(self, pending: PendingOrder, stage: Stage, *, extra_orders: int = 0) -> RiskVerdict:
        if pending.action == OrderAction.CANCEL:
            return RiskVerdict()
        profile = self._store.get_profile()
        if profile is None:
            return RiskVerdict()
        if stage == "approve" and (pending.action != OrderAction.PLACE or
                not (profile.hard_order_limit or profile.hard_stop_on_daily_loss)):
            return RiskVerdict()
        now = self._clock()
        facts = await compute_today(self._broker, profile, now)
        return evaluate(pending, stage, profile, facts, self._store.get_goal(), now, extra_orders)
