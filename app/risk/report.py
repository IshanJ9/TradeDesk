"""Pure discipline score, comparisons, goal arithmetic, and deterministic DEMO history."""

from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from random import Random

from app.risk.models import (ComparisonGroup, DisciplineDay, Goal, GoalProgress, HistoryComparison,
                             RiskProfile, RiskScore, ScoreComponent, TodayFacts, TrendPoint)
from app.schemas import fmt_rupees


def rounded(value: float | Decimal) -> int:
    return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def score_today(facts: TodayFacts, profile: RiskProfile) -> RiskScore:
    values = [
        ("activity", "Order activity", facts.orders_today / profile.max_orders_per_day * 100, 30),
        ("size", "Largest order", facts.largest_order_pct / profile.max_order_pct * 100, 20),
        ("concentration", "Stock concentration", facts.largest_stock_pct / profile.max_stock_pct * 100, 20),
        ("intraday", "Intraday turnover", facts.intraday_share_pct, 15),
        ("loss_chasing", "Re-entry and cooling-off breaches", (facts.reentries + facts.cooling_off_breaches) * 50, 15),
    ]
    components = [ScoreComponent(key=k, label=label, score=min(100, max(0, value)), weight=weight)
                  for k, label, value, weight in values]
    return RiskScore(total=rounded(sum(c.score * c.weight / 100 for c in components)), components=components)


def compare_history(days: list[DisciplineDay], today: date) -> HistoryComparison:
    past = sorted((d for d in days if d.day < today), key=lambda d: d.day, reverse=True)
    real = [d for d in past if not d.demo]
    # Once real history exists, never use synthetic days to describe this trader.
    chosen = real if real else [d for d in past if d.demo]
    scored = [d for d in chosen if d.risk_score is not None]
    baseline = scored[:20]
    average = sum(d.risk_score for d in baseline) / len(baseline) if baseline else None

    def group(items):
        return ComparisonGroup(days=len(items), net_pnl=sum(d.pnl_after_charges for d in items),
                               profitable_days=sum(d.pnl_after_charges > 0 for d in items))

    window = chosen[:30]
    cumulative = 0
    timeline = []
    for day in reversed(window):
        cumulative += day.pnl_after_charges
        timeline.append(TrendPoint(day=day.day, cumulative_pnl_paise=cumulative,
                                   risk_score=day.risk_score, demo=day.demo))
    above = [d for d in window if d.risk_score is not None and average is not None and d.risk_score > average]
    below = [d for d in window if d.risk_score is not None and average is not None and d.risk_score <= average]
    return HistoryComparison(average_score=average, baseline_days=len(baseline),
                             source="real" if real else "demo" if chosen else "none",
                             above_usual=group(above), at_or_below_usual=group(below), days=window,
                             timeline=timeline)


def goal_progress(goal: Goal, portfolio: int, today: date) -> GoalProgress:
    target = goal.target_paise if goal.target_paise is not None else max(
        1, rounded(Decimal(goal.start_value) * Decimal(str(goal.target_pct)) / 100))
    progress = portfolio - goal.start_value
    remaining = max(0, target - progress)
    duration = (goal.end_date - goal.start_date).days
    elapsed = min(duration, max(0, (today - goal.start_date).days))
    left = max(0, (goal.end_date - today).days)
    pace = rounded(Decimal(target) * elapsed / duration)
    weekly = rounded(Decimal(remaining) * 7 / left) if left else (0 if not remaining else None)
    status = "not_started" if today < goal.start_date else "achieved" if progress >= target else "expired" if today >= goal.end_date else "active"
    return GoalProgress(goal=goal, progress_paise=progress, target_paise=target,
                        progress_pct=100 * progress / target, days_left=left, remaining_paise=remaining,
                        needed_per_week_paise=weekly, pace_paise=pace,
                        pace_text=f"A straight line to your goal would be {fmt_rupees(pace)} by today; you're at {fmt_rupees(progress)}.",
                        loss_headroom_paise=goal.max_acceptable_loss_paise - max(0, -progress), status=status)


def demo_days(today: date) -> list[DisciplineDay]:
    rng = Random(721021)
    days = []
    cursor = today
    for _ in range(20):
        cursor -= timedelta(days=1)
        while cursor.weekday() >= 5:
            cursor -= timedelta(days=1)
        components = [ScoreComponent(key=key, label=label, score=rng.randint(0, 100), weight=weight)
                      for key, label, weight in [("activity", "Order activity", 30), ("size", "Largest order", 20),
                          ("concentration", "Stock concentration", 20), ("intraday", "Intraday turnover", 15),
                          ("loss_chasing", "Re-entry and cooling-off breaches", 15)]]
        pnl, charges = rng.randint(-80_000, 100_000), rng.randint(500, 6_000)
        days.append(DisciplineDay(day=cursor, orders=rng.randint(1, 12), turnover=rng.randint(100_000, 5_000_000),
                                   pnl=pnl, charges=charges, pnl_after_charges=pnl-charges,
                                   risk_score=rounded(sum(c.score*c.weight/100 for c in components)),
                                   components=components, demo=True))
    return days
