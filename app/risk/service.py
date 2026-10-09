"""Read-only report orchestration. GET refreshes do not publish (avoids refetch loops)."""

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timedelta

from app.api_models import DisciplineSummary, DisciplineUpdateEvent
from app.broker.base import BrokerTimeout, ReadOnlyBroker
from app.events import EventHub
from app.history.store import ActivityStore, DaySummary, trading_day
from app.risk.models import ChargesMeter, DisciplineDay, DisciplineReport
from app.risk.presets import preset
from app.risk.report import compare_history, demo_days, goal_progress, rounded, score_today
from app.risk.report_store import ReportStore
from app.risk.store import ProfileStore
from app.risk.today import compute_today, percentage

log = logging.getLogger("tradedesk.risk")


class DisciplineService:
    def __init__(self, broker: ReadOnlyBroker, profiles: ProfileStore, reports: ReportStore,
                 history: Callable[[], ActivityStore], hub: EventHub, clock: Callable[[], datetime], demo_mode: bool):
        self.broker, self.profiles, self.reports = broker, profiles, reports
        self.history, self.hub, self.clock, self.demo_mode = history, hub, clock, demo_mode
        self._lock = asyncio.Lock()

    def _days(self) -> list[DisciplineDay]:
        owned = self.reports.days()
        merged = {d.day: d for d in owned if d.demo and self.demo_mode}
        merged.update({d.day: d for d in owned if not d.demo})
        for d in self.history().days(limit=3650):
            if d.demo:  # Synthetic data is owned here, so DELETE is reliable.
                continue
            existing = merged.get(d.day)
            components = existing.components if existing and not existing.demo and existing.risk_score == d.risk_score else []
            merged[d.day] = DisciplineDay(**d.model_dump(), pnl_after_charges=d.pnl-d.charges, components=components)
        return sorted(merged.values(), key=lambda d: d.day, reverse=True)

    def _seed(self, force: bool = False) -> int:
        if not self.demo_mode:
            return 0
        if not force and self.reports.seed_attempted():
            return 0
        existing = {d.day for d in self._days()}
        self.reports.mark_seed_attempted()
        if not force and existing:
            return 0
        added = 0
        for day in demo_days(trading_day(self.clock())):
            if day.day not in existing:
                self.reports.save_day(day)
                added += 1
        return added

    async def seed(self) -> int:
        async with self._lock:
            return self._seed(force=True)

    async def clear_demo(self) -> None:
        async with self._lock:
            self.reports.clear_demo()

    async def refresh(self, *, publish: bool = False) -> DisciplineReport:
        async with self._lock:
            now = self.clock()
            profile = self.profiles.get_profile()
            facts = await compute_today(self.broker, profile or preset("balanced"), now)
            score = score_today(facts, profile) if profile else None
            self._seed()
            summary = DaySummary(day=facts.day, orders=facts.orders_today, turnover=facts.turnover,
                                 pnl=facts.pnl_estimate, charges=facts.charges,
                                 risk_score=score.total if score else None)
            self.history().save_day(summary)
            self.reports.save_day(DisciplineDay(**summary.model_dump(), pnl_after_charges=facts.pnl_after_charges,
                                               components=score.components if score else []))
            days = self._days()
            comparison = compare_history(days, facts.day)
            goal = self.profiles.get_goal()
            recent = [d for d in days if facts.day - timedelta(days=29) <= d.day <= facts.day]
            warnings = list(facts.notes)
            if profile is None:
                warnings.append("Set your own profile to see a risk score. Re-entry counts currently use the balanced starting window.")
            elif facts.orders_today > profile.max_orders_per_day:
                warnings.append(f"You set {profile.max_orders_per_day} orders a day; you have {facts.orders_today} today.")
            if comparison.source == "demo":
                warnings.append("DEMO DATA: the usual-risk comparison uses synthetic days, not your trading history.")
            report = DisciplineReport(profile=profile, today=facts, score=score, history=comparison,
                                      goal=goal_progress(goal, facts.portfolio_value, facts.day) if goal else None,
                                      charges=ChargesMeter(today_paise=facts.charges,
                                          turnover_pct=percentage(facts.charges, facts.turnover),
                                          last_30_days_paise=sum(d.charges for d in recent if not d.demo),
                                          last_30_days_demo_paise=sum(d.charges for d in recent if d.demo)),
                                      warnings=warnings)
            if publish:
                self.hub.publish(DisciplineUpdateEvent, summary=DisciplineSummary(
                    orders_today=facts.orders_today, order_limit=profile.max_orders_per_day if profile else None,
                    risk_score=score.total if score else None,
                    average_score=rounded(comparison.average_score) if comparison.average_score is not None else None,
                    warnings=warnings))
            return report

    async def refresh_safely(self) -> None:
        try:
            await self.refresh(publish=True)
        except BrokerTimeout:
            log.warning("discipline refresh skipped: broker unreachable")
        except Exception:
            # Never log broker exception text, which can contain credentials.
            log.warning("discipline refresh failed; retrying on the next interval")

    async def run(self, interval: float = 20) -> None:
        while True:
            await asyncio.sleep(interval)
            await self.refresh_safely()
