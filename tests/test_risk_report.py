import asyncio
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.broker.base import BrokerTimeout
from app.broker.mock import MockBroker
from app.config import Settings
from app.db import Database
from app.events import EventHub
from app.history.store import DaySummary, InMemoryActivityStore
from app.main import create_app
from app.risk.models import DisciplineDay, Goal
from app.risk.presets import preset
from app.risk.report import compare_history, demo_days, goal_progress, score_today
from app.risk.report_store import ReportStore
from app.risk.service import DisciplineService
from app.risk.store import ProfileStore
from app.risk.today import calculate_today
from app.schemas import Funds

NOW = datetime(2026, 10, 9, 10, tzinfo=timezone.utc)
TODAY = NOW.date()
PROFILE = preset("balanced")
FACTS = calculate_today(orders=[], holdings=[], positions=[], funds=Funds(available_cash=1_000_000),
                        profile=PROFILE, now=NOW)


def day(offset=1, score=50, demo=False, pnl=10_000, charges=1000):
    return DisciplineDay(day=TODAY-timedelta(days=offset), orders=2, turnover=100_000,
                         pnl=pnl, charges=charges, pnl_after_charges=pnl-charges,
                         risk_score=score, demo=demo, components=[])


def goal(**updates):
    return Goal(**(dict(target_paise=100_000, start_date=TODAY-timedelta(days=7), end_date=TODAY+timedelta(days=7),
                       start_value=1_000_000, max_acceptable_loss_paise=50_000) | updates))


@pytest.mark.parametrize("updates,key,score,total", [
    ({"orders_today": 3}, "activity", 50, 15),
    ({"largest_order_pct": 10}, "size", 50, 10),
    ({"largest_stock_pct": 15}, "concentration", 50, 10),
    ({"intraday_share_pct": 100}, "intraday", 100, 15),
    ({"reentries": 1}, "loss_chasing", 50, 8),
    ({"cooling_off_breaches": 1}, "loss_chasing", 50, 8),
    ({"reentries": 1, "cooling_off_breaches": 1}, "loss_chasing", 100, 15),
])
def test_score_weights_and_half_up_rounding(updates, key, score, total):
    result = score_today(FACTS.model_copy(update=updates), PROFILE)
    assert result.total == total
    assert next(c.score for c in result.components if c.key == key) == score
    assert sum(c.weight for c in result.components) == 100


def test_score_caps_every_component_and_total():
    result = score_today(FACTS.model_copy(update={"orders_today": 100, "largest_order_pct": 999,
                         "largest_stock_pct": 999, "intraday_share_pct": 999, "reentries": 10}), PROFILE)
    assert result.total == 100 and all(c.score == 100 for c in result.components)
    assert score_today(FACTS, PROFILE).total == 0


def test_average_uses_previous_twenty_real_scored_days_only():
    days = [day(i, i) for i in range(1, 22)] + [day(0, 100), day(-1, 100), day(22, 100, demo=True)]
    result = compare_history(days, TODAY)
    assert result.average_score == 10.5 and result.baseline_days == 20
    assert result.source == "real" and len(result.days) == 21
    assert all(not d.demo and d.day < TODAY for d in result.days)


def test_history_split_equal_is_below_and_profitable_means_after_charges():
    result = compare_history([day(1, 20, pnl=500), day(2, 50, pnl=1000), day(3, 80, pnl=2000)], TODAY)
    assert result.average_score == 50
    assert result.above_usual.model_dump() == {"days": 1, "net_pnl": 1000, "profitable_days": 1}
    assert result.at_or_below_usual.model_dump() == {"days": 2, "net_pnl": -500, "profitable_days": 0}
    assert result.note == "Past days don't predict future ones."


def test_history_demo_fallback_and_unscored_real_days():
    assert compare_history([day(demo=True)], TODAY).source == "demo"
    result = compare_history([day(score=None), day(2, demo=True)], TODAY)
    assert result.source == "real" and result.average_score is None
    assert result.baseline_days == result.above_usual.days == result.at_or_below_usual.days == 0
    assert compare_history([], TODAY).source == "none"


def test_history_detail_window_is_thirty_days():
    assert len(compare_history([day(i) for i in range(1, 41)], TODAY).days) == 30


def test_goal_progress_pace_and_weekly_arithmetic():
    result = goal_progress(goal(), 1_020_000, TODAY)
    assert result.progress_paise == 20_000 and result.progress_pct == 20
    assert result.days_left == 7 and result.needed_per_week_paise == 80_000
    assert result.pace_paise == 50_000 and result.loss_headroom_paise == 50_000
    assert result.status == "active" and "straight line" in result.pace_text


def test_percentage_goal_and_negative_progress():
    result = goal_progress(goal(target_paise=None, target_pct=10), 940_000, TODAY)
    assert result.target_paise == 100_000 and result.progress_pct == -60
    assert result.loss_headroom_paise == -10_000
    assert result.needed_per_week_paise == 160_000


@pytest.mark.parametrize("offset,portfolio,status,weekly", [(7, 1_000_000, "expired", None),
                   (8, 1_000_000, "expired", None), (7, 1_100_000, "achieved", 0), (0, 1_200_000, "achieved", 0)])
def test_expired_and_achieved_goals(offset, portfolio, status, weekly):
    result = goal_progress(goal(), portfolio, TODAY+timedelta(days=offset))
    assert result.status == status and result.needed_per_week_paise == weekly
    assert result.remaining_paise >= 0


def test_goal_before_start_and_after_end_clamps_pace():
    assert goal_progress(goal(), 1_000_000, TODAY-timedelta(days=9)).pace_paise == 0
    assert goal_progress(goal(), 1_000_000, TODAY-timedelta(days=9)).status == "not_started"
    assert goal_progress(goal(), 1_000_000, TODAY+timedelta(days=9)).pace_paise == 100_000


def test_demo_seed_is_deterministic_twenty_past_weekdays_with_components():
    a, b = demo_days(TODAY), demo_days(TODAY)
    assert a == b and len(a) == 20 and len({d.day for d in a}) == 20
    assert all(d.demo and d.day < TODAY and d.day.weekday() < 5 and len(d.components) == 5 for d in a)


@pytest.fixture
def service():
    db = Database()
    profiles, reports, history, hub = ProfileStore(db), ReportStore(db), InMemoryActivityStore(), EventHub()
    profiles.save_profile(PROFILE)
    result = DisciplineService(MockBroker(clock=lambda: NOW).read_only(), profiles, reports, lambda: history,
                               hub, lambda: NOW, True)
    yield result
    db.close()


@pytest.mark.asyncio
async def test_refresh_persists_shared_summary_and_components_without_get_event(service):
    with patch("app.risk.service.compute_today", new_callable=AsyncMock, return_value=FACTS):
        first = await service.refresh()
        assert first.history.source == "demo"
        assert any("DEMO DATA" in w for w in first.warnings)
        assert service.hub.seq == 0
        assert service.history().days()[0].day == TODAY
        stored = next(d for d in service.reports.days() if d.day == TODAY)
        assert stored.components == first.score.components
        assert not stored.demo
        await service.refresh()
        assert len(service.reports.days()) == 21  # upsert, not duplicate snapshots
        assert service.hub.seq == 0


@pytest.mark.asyncio
async def test_publish_uses_shared_event_contract(service):
    queue = service.hub.subscribe()
    with patch("app.risk.service.compute_today", new_callable=AsyncMock, return_value=FACTS):
        report = await service.refresh(publish=True)
    event = queue.get_nowait()
    assert event.type == "discipline_update"
    assert event.summary.order_limit == 6
    assert event.summary.risk_score == report.score.total
    assert event.summary.average_score is not None


@pytest.mark.asyncio
async def test_clear_demo_preserves_real_and_does_not_reseed(service):
    with patch("app.risk.service.compute_today", new_callable=AsyncMock, return_value=FACTS):
        await service.refresh()
        await service.clear_demo()
        report = await service.refresh()
    assert report.history.source == "none"
    assert len(service.reports.days()) == 1 and not service.reports.days()[0].demo
    assert len(service.history().days()) == 1


@pytest.mark.asyncio
async def test_auto_seed_skips_existing_history_and_explicit_seed_never_overwrites_real(service):
    previous = DaySummary(day=TODAY-timedelta(days=1), orders=1, turnover=1, pnl=1, charges=0, risk_score=40)
    service.history().save_day(previous)
    with patch("app.risk.service.compute_today", new_callable=AsyncMock, return_value=FACTS):
        report = await service.refresh()
    assert report.history.source == "real" and not any(d.demo for d in service.reports.days())
    assert await service.seed() == 19
    assert await service.seed() == 0
    assert service.history().days()[-1] == previous


@pytest.mark.asyncio
async def test_real_and_demo_charges_are_separate_calendar_30_day_totals(service):
    service.reports.mark_seed_attempted()
    for item in [day(1, charges=100), day(29, charges=200), day(30, charges=999),
                 day(2, charges=300, demo=True), day(-1, charges=999)]:
        service.reports.save_day(item)
    with patch("app.risk.service.compute_today", new_callable=AsyncMock,
               return_value=FACTS.model_copy(update={"charges": 50, "turnover": 1000, "pnl_after_charges": -50})):
        report = await service.refresh()
    assert report.charges.last_30_days_paise == 350
    assert report.charges.last_30_days_demo_paise == 300
    assert report.charges.turnover_pct == 5


@pytest.mark.asyncio
async def test_no_profile_returns_no_score_and_saves_unscored_day(service):
    with patch.object(service.profiles, "get_profile", return_value=None):
        report = await service.refresh(publish=True)
    assert report.score is None and report.profile is None
    assert service.history().days()[0].risk_score is None


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [BrokerTimeout("offline"), RuntimeError("bad snapshot")])
async def test_background_refresh_recovers_from_errors(service, error):
    with patch.object(service, "refresh", new_callable=AsyncMock, side_effect=[error, None]) as refresh:
        await service.refresh_safely()
        await service.refresh_safely()
    assert refresh.await_count == 2


@pytest.mark.asyncio
async def test_loop_waits_twenty_seconds_and_cancels_cleanly(service):
    with patch("app.risk.service.asyncio.sleep", new_callable=AsyncMock,
               side_effect=[None, asyncio.CancelledError]) as sleep, \
         patch.object(service, "refresh_safely", new_callable=AsyncMock) as refresh:
        with pytest.raises(asyncio.CancelledError):
            await service.run()
    assert sleep.call_args_list[0].args == (20,)
    refresh.assert_awaited_once()


def test_report_storage_survives_restart_and_clearing_is_durable(tmp_path):
    url = f"sqlite:///{tmp_path / 'reports.db'}"
    db = Database(url)
    store = ReportStore(db)
    store.save_day(day())
    store.save_day(day(2, demo=True))
    store.clear_demo()
    db.close()
    db = Database(url)
    reopened = ReportStore(db)
    assert reopened.days() == [day()] and reopened.seed_attempted()
    db.close()


@pytest.mark.parametrize("demo_mode", [False, True])
def test_api_report_seed_clear_and_openapi(demo_mode):
    settings = Settings(broker="mock", llm_provider="rules", demo_mode=demo_mode, database_url="sqlite:///:memory:",
                        ticker_interval=None, reconcile_interval=None)
    with TestClient(create_app(settings, broker=MockBroker(clock=lambda: NOW), clock=lambda: NOW)) as client:
        seed = client.post("/api/discipline/demo-seed")
        assert seed.status_code == (200 if demo_mode else 404)
        if demo_mode:
            assert seed.json()["seeded_days"] == 20
        report = client.get("/api/discipline")
        assert report.status_code == 200
        assert report.json()["score"] is None
        assert report.json()["history"]["source"] == ("demo" if demo_mode else "none")
        seq = client.app.state.hub.seq
        assert client.put("/api/profile", json=PROFILE.model_dump(mode="json")).status_code == 200
        assert client.app.state.hub.seq == seq+1
        assert client.get("/api/discipline").json()["score"] is not None
        assert client.app.state.hub.seq == seq+1
        cleared = client.delete("/api/discipline/demo-seed")
        assert cleared.status_code == (204 if demo_mode else 404)
        assert client.get("/api/discipline").json()["history"]["source"] == "none"
        assert "DisciplineReport" in client.get("/openapi.json").json()["components"]["schemas"]


def test_profile_save_survives_broker_timeout_but_report_returns_503():
    broker = MockBroker(clock=lambda: NOW)
    settings = Settings(broker="mock", llm_provider="rules", ticker_interval=None, reconcile_interval=None)
    with TestClient(create_app(settings, broker=broker, clock=lambda: NOW)) as client:
        broker.get_funds = AsyncMock(side_effect=BrokerTimeout("offline"))
        assert client.put("/api/profile", json=PROFILE.model_dump(mode="json")).status_code == 200
        assert client.get("/api/profile").json()["style"] == "balanced"
        assert client.get("/api/discipline").status_code == 503
