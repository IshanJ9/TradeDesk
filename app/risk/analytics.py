"""Descriptive analytics from recorded observations only. No predictions or generated history."""

from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

from app.history.store import ActivityStore, IST, trading_day
from app.risk.models import AnalyticsGroup, AnalyticsReport, DisciplineDay, OrderPattern, PaceReport
from app.risk.report_store import ReportStore
from app.schemas import OrderStatus


def average_money(values: list[int]) -> int | None:
    return int((Decimal(sum(values))/len(values)).quantize(Decimal('1'), rounding=ROUND_HALF_UP)) if values else None


def group(label: str, days: list[DisciplineDay]) -> AnalyticsGroup:
    risks = [d.average_risk_score for d in days if d.average_risk_score is not None]
    returns = [d.observed_return_pct for d in days if d.observed_return_pct is not None]
    wins = sum(d.pnl_after_charges > 0 for d in days)
    return AnalyticsGroup(label=label, days=len(days), risk_days=len(risks),
        average_risk=sum(risks)/len(risks) if risks else None,
        average_net_pnl_paise=average_money([d.pnl_after_charges for d in days]),
        total_net_pnl_paise=sum(d.pnl_after_charges for d in days), profitable_days=wins,
        profitable_day_pct=100*wins/len(days) if days else None, return_days=len(returns),
        average_observed_return_pct=sum(returns)/len(returns) if returns else None)


def pace(history: ActivityStore, reports: ReportStore, now: datetime) -> PaceReport:
    local = now.astimezone(IST)
    start = now-timedelta(minutes=20)
    def count(day, begin, end):
        return sum(r.recording_source == 'zerotwoone' and r.order.status != OrderStatus.REJECTED and begin < r.order.created_at <= end
                   for r in history.orders_on(day))
    current = count(trading_day(now), start, now)
    if trading_day(start) != trading_day(now):
        current += count(trading_day(start), start, now)
    prior = []
    for d in history.days(limit=90):
        if d.demo or d.day >= local.date():
            continue
        end = datetime.combine(d.day, local.timetz())
        begin = end-timedelta(minutes=20)
        if begin.date() == end.date() and reports.covered(begin,end):
            prior.append(count(d.day,begin,end))
        if len(prior)>=20:
            break
    return PaceReport(orders_now=current, usual_orders=sum(prior)/len(prior) if prior else None,
        baseline_days=len(prior), note="Last 20 minutes versus the same IST clock window on up to 20 recorded past days. "
        "Only windows with five-minute observation coverage count. Rejected orders are excluded; external orders count.")


def build_analytics(days: list[DisciplineDay], history: ActivityStore, reports: ReportStore, now: datetime,
                    *, recording_source: str = 'unknown') -> AnalyticsReport:
    # Demo snapshots must never become evidence about the trader, even when no real history exists.
    selected = sorted((d for d in days if not d.demo and d.recording_source == 'zerotwoone'
                       and d.day < trading_day(now)), key=lambda d:d.day)[-30:]
    weekdays = [group(name,[d for d in selected if d.day.weekday()==i])
                for i,name in enumerate(['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'])]
    bands = [group(label,[d for d in selected if d.average_risk_score is not None and lo<=d.average_risk_score<hi])
             for label,lo,hi in [('0–<25',0,25),('25–<50',25,50),('50–<75',50,75),('75–100',75,101)]]
    symbols, hours = defaultdict(list), defaultdict(list)
    pattern_days = 0
    for d in selected:
        records = [r for r in history.orders_on(d.day) if r.recording_source == 'zerotwoone'
                   and r.order.status != OrderStatus.REJECTED]
        if records:
            pattern_days += 1
        for record in records:
            symbols[record.order.instrument.key].append(record)
            hours[f'{record.order.created_at.astimezone(IST).hour:02d}:00 IST'].append(record)
    def patterns(buckets):
        return [OrderPattern(label=label,orders=len(rows),external_orders=sum(r.source=='external' for r in rows),
                 filled_turnover_paise=sum(r.order.filled_quantity*(r.order.avg_fill_price or 0) for r in rows))
                for label,rows in sorted(buckets.items(),key=lambda item:(-len(item[1]),item[0]))]
    behavior = [d for d in selected if d.reentries is not None and d.cooling_off_breaches is not None]
    return AnalyticsReport(source='real' if selected else 'none', summary=group('Recorded past days',selected),
        today=next((d for d in days if not d.demo and d.recording_source=='zerotwoone'
                    and d.day==trading_day(now)),None),
        weekdays=weekdays,risk_bands=bands,days=selected,symbols=patterns(symbols)[:10],
        hours=sorted(patterns(hours),key=lambda row:row.label),pattern_days=pattern_days,
        reentries=sum(d.reentries for d in behavior),cooling_off_breaches=sum(d.cooling_off_breaches for d in behavior),
        behavior_days=len(behavior),pace=pace(history,reports,now) if recording_source=='zerotwoone' else
        PaceReport(orders_now=None, usual_orders=None, baseline_days=0,
                   note='Live pace unavailable: connect the 021 broker. Mock activity is excluded.'),notes=[
            'Only snapshots and order records identified as 021 observations are included. Legacy records without known provenance and all mock/demo activity are excluded.',
            'Actual recorded history only. Today is excluded from historical averages; up to 30 recorded past days are shown.',
            'Average risk uses one observation per five-minute bucket. Gaps are not filled. It is an observed-sample average, not a full-session time-weighted score.',
            'Net P&L and charges are estimates. Observed return = change in estimated day net P&L since the first snapshot / that snapshot portfolio value. It needs at least five minutes of observations.',
            'Observed returns can cover partial days. The average is arithmetic over eligible days, not compounded, annualized or cash-flow adjusted. No baseline means no percentage.',
            'Risk settings may change between days. Risk bands are descriptive ranges, not recommendations or evidence that risk caused returns.',
            'Order-pattern counts use persisted non-rejected broker orders. Filled turnover excludes unknown fill prices. No order records means unavailable coverage, not proof of no trading.',
            'Re-entry and cooling-off counts describe observed behavior, not motives. Closed-trade loss streaks use approximate FIFO before charges.'])
