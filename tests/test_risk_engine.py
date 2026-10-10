from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.broker.base import BrokerTimeout
from app.broker.mock import MockBroker
from app.config import Settings
from app.db import Database
from app.main import create_app
from app.risk.engine import ProfileGuard, evaluate
from app.risk.guard import RiskVerdict
from app.risk.models import Goal
from app.risk.presets import preset
from app.risk.store import ProfileStore
from app.risk.today import calculate_today
from app.schemas import Exchange, Funds, Instrument, PendingOrder

NOW = datetime(2026, 10, 9, 10, tzinfo=timezone.utc)
STOCK = Instrument(symbol="INFY", exchange=Exchange.NSE)
PROFILE = preset("balanced")
FACTS = calculate_today(orders=[], holdings=[], positions=[], funds=Funds(available_cash=1_000_000),
                        profile=PROFILE, now=NOW)


def pending(**updates):
    fields = dict(id="p1", action="PLACE", instrument=STOCK, side="BUY", quantity=10,
                  order_type="LIMIT", limit_price=10_000, client_order_id="c1", ref_ltp=10_000,
                  created_at=NOW, expires_at=NOW + timedelta(seconds=60))
    fields.update(updates)
    return PendingOrder(**fields)


def verdict(*, p=None, profile=None, stage="preview", goal=None, **facts):
    return evaluate(p or pending(), stage, profile or PROFILE, FACTS.model_copy(update=facts), goal, NOW)


def has(result, text):
    return any(text in w for w in result.warnings)


@pytest.mark.parametrize("count,warn", [(4, False), (5, False), (6, True)])
def test_order_count_boundary(count, warn):
    result = verdict(orders_today=count)
    assert has(result, "orders a day") == warn
    assert result.block is None


@pytest.mark.parametrize("quantity,warn", [(19, False), (20, False), (21, True)])
def test_single_order_boundary(quantity, warn):
    assert has(verdict(p=pending(quantity=quantity)), "single-order limit") == warn


@pytest.mark.parametrize("held,warn", [(199_999, False), (200_000, False), (200_001, True)])
def test_stock_weight_boundary(held, warn):
    assert has(verdict(stock_values={STOCK.key: held}), "stock-weight limit") == warn


def test_sell_does_not_warn_about_increasing_stock_weight():
    assert not has(verdict(p=pending(side="SELL"), stock_values={STOCK.key: 900_000}), "stock-weight limit")


@pytest.mark.parametrize("loss,close,reached", [(0, False, False), (15_999, False, False),
                         (16_000, True, False), (19_999, True, False), (20_000, False, True), (20_001, False, True)])
def test_daily_loss_boundaries(loss, close, reached):
    result = verdict(pnl_after_charges=-loss)
    assert has(result, "close to your limit") == close
    assert has(result, "has reached it") == reached
    assert result.block is None


@pytest.mark.parametrize("streak,seconds,warn", [(2, 0, False), (3, 0, True), (3, 1199, True),
                                                               (3, 1200, False), (3, -1, False)])
def test_cooling_boundary(streak, seconds, warn):
    assert has(verdict(consecutive_losses=streak, last_loss_at=NOW - timedelta(seconds=seconds)),
               "cooling-off") == warn


@pytest.mark.parametrize("seconds,warn", [(0, True), (1799, True), (1800, False), (-1, False)])
def test_reentry_boundary(seconds, warn):
    assert has(verdict(last_loss_by_stock={STOCK.key: NOW - timedelta(seconds=seconds)}), "re-entry") == warn


def test_reentry_only_same_stock_buy_and_known_loss():
    assert not has(verdict(last_loss_by_stock={"NSE:OTHER": NOW}), "re-entry")
    assert not has(verdict(p=pending(side="SELL"), last_loss_by_stock={STOCK.key: NOW}), "re-entry")
    assert not has(verdict(consecutive_losses=4), "cooling-off")


@pytest.mark.parametrize("product,allowed,warn", [("CNC", False, False), ("MIS", False, True), ("MIS", True, False)])
def test_intraday_setting(product, allowed, warn):
    result = verdict(p=pending(product=product), profile=PROFILE.model_copy(update={"intraday_allowed": allowed}))
    assert has(result, "intraday trading") == warn


def goal(**updates):
    return Goal(**(dict(target_paise=100_000, start_date=date(2026, 10, 8), end_date=date(2026, 11, 8),
                       start_value=1_000_000, max_acceptable_loss_paise=100_000) | updates))


@pytest.mark.parametrize("combined,warn", [(79_999, False), (80_000, True), (100_000, True)])
def test_goal_loss_boundary(combined, warn):
    result = verdict(goal=goal(), portfolio_value=960_000, pnl_after_charges=-(combined - 40_000))
    assert has(result, "goal loss allowance") == warn
    if warn:
        assert has(result, "periods can overlap")


def test_goal_gains_do_not_offset_losses_and_inactive_goals_do_not_warn():
    assert has(verdict(goal=goal(), portfolio_value=1_100_000, pnl_after_charges=-80_000), "goal loss allowance")
    assert not has(verdict(goal=goal(end_date=date(2026, 10, 8), start_date=date(2026, 10, 7)),
                           pnl_after_charges=-100_000), "goal loss allowance")
    assert not has(verdict(goal=goal(start_date=date(2026, 10, 10)), pnl_after_charges=-100_000), "goal loss allowance")


@pytest.mark.parametrize("stage", ["preview", "approve"])
@pytest.mark.parametrize("enabled,count,blocked", [(False, 7, False), (True, 5, False), (True, 6, True)])
def test_hard_order_limit(stage, enabled, count, blocked):
    result = verdict(stage=stage, profile=PROFILE.model_copy(update={"hard_order_limit": enabled}), orders_today=count)
    assert bool(result.block) == blocked
    if blocked:
        assert "change it in Discipline" in result.block


@pytest.mark.parametrize("stage", ["preview", "approve"])
@pytest.mark.parametrize("enabled,loss,blocked", [(False, 20_000, False), (True, 19_999, False), (True, 20_000, True)])
def test_hard_daily_loss(stage, enabled, loss, blocked):
    result = verdict(stage=stage, profile=PROFILE.model_copy(update={"hard_stop_on_daily_loss": enabled}), pnl_after_charges=-loss)
    assert bool(result.block) == blocked
    if blocked:
        assert "change it in Discipline" in result.block


@pytest.mark.parametrize("action", ["CANCEL", "MODIFY"])
@pytest.mark.parametrize("stage", ["preview", "approve"])
def test_cancel_modify_never_block(action, stage):
    result = verdict(p=pending(action=action, target_order_id="old"), stage=stage,
                     profile=PROFILE.model_copy(update={"hard_order_limit": True, "hard_stop_on_daily_loss": True}),
                     orders_today=99, pnl_after_charges=-100_000)
    assert result.block is None
    assert bool(result.warnings) == (action == "MODIFY" and stage == "preview")


def test_modify_does_not_add_to_order_count():
    result = verdict(p=pending(action="MODIFY", target_order_id="old"), orders_today=6)
    assert not has(result, "orders a day")


def test_approve_has_no_soft_warnings():
    assert verdict(stage="approve", orders_today=99, pnl_after_charges=-100_000,
                   consecutive_losses=9, last_loss_at=NOW, last_loss_by_stock={STOCK.key: NOW}) == RiskVerdict()


def test_nonpositive_portfolio_discloses_unavailable_checks_without_false_loss_block():
    result = verdict(profile=PROFILE.model_copy(update={"hard_stop_on_daily_loss": True}),
                     portfolio_value=0, pnl_after_charges=-100_000)
    assert result.block is None and has(result, "checks are unavailable")
    assert not has(result, "single-order limit")


def test_market_uses_protection_price_and_warning_does_not_change_order_hash():
    p = pending(order_type="MARKET", limit_price=None, protection_price=21_000)
    result = verdict(p=p)
    assert has(result, "single-order limit")
    assert p.model_copy(update={"warnings": list(result.warnings)}).order_hash == p.order_hash


@pytest.fixture
def store():
    db = Database()
    yield ProfileStore(db)
    db.close()


@pytest.mark.asyncio
async def test_no_profile_cancel_and_soft_approve_do_not_read_broker(store):
    broker = AsyncMock()
    guard = ProfileGuard(broker, store, lambda: NOW)
    assert await guard.check(pending(), "preview") == RiskVerdict()
    store.save_profile(PROFILE)
    assert await guard.check(pending(), "approve") == RiskVerdict()
    store.save_profile(PROFILE.model_copy(update={"hard_order_limit": True}))
    assert await guard.check(pending(action="CANCEL", target_order_id="old"), "preview") == RiskVerdict()
    assert await guard.check(pending(action="MODIFY", target_order_id="old"), "approve") == RiskVerdict()
    assert not broker.mock_calls


@pytest.mark.asyncio
async def test_approval_reloads_profile_and_facts(store):
    store.save_profile(PROFILE.model_copy(update={"hard_order_limit": True}))
    guard = ProfileGuard(AsyncMock(), store, lambda: NOW)
    with patch("app.risk.engine.compute_today", new_callable=AsyncMock) as compute:
        compute.return_value = FACTS.model_copy(update={"orders_today": 5})
        assert (await guard.check(pending(), "preview")).block is None
        compute.return_value = FACTS.model_copy(update={"orders_today": 6})
        assert (await guard.check(pending(), "approve")).block
        assert compute.await_count == 2
        store.save_profile(PROFILE)
        assert (await guard.check(pending(), "approve")).block is None
        compute.assert_awaited_with(guard._broker, PROFILE.model_copy(update={"hard_order_limit": True}), NOW)


@pytest.mark.asyncio
async def test_timeout_propagates_to_existing_nothing_sent_handler(store):
    store.save_profile(PROFILE.model_copy(update={"hard_order_limit": True}))
    guard = ProfileGuard(AsyncMock(), store, lambda: NOW)
    with patch("app.risk.engine.compute_today", new_callable=AsyncMock, side_effect=BrokerTimeout("offline")):
        with pytest.raises(BrokerTimeout):
            await guard.check(pending(), "approve")


def test_main_wires_same_guard_and_store_into_services():
    settings = Settings(broker="mock", llm_provider="rules", database_url="sqlite:///:memory:",
                        ticker_interval=None, reconcile_interval=None)
    with TestClient(create_app(settings, broker=MockBroker(clock=lambda: NOW), clock=lambda: NOW)) as client:
        state = client.app.state
        assert isinstance(state.risk, ProfileGuard)
        assert state.risk._store is state.profile_store
        assert state.cards._risk is state.risk and state.approvals._risk is state.risk


@pytest.mark.parametrize("failure", ["order_limit", "loss_limit", "cooldown", "goal_loss", "timeout"])
def test_real_approval_route_returns_blocked_and_never_sends(failure):
    broker = MockBroker(clock=lambda: NOW)
    broker.place_order = AsyncMock(side_effect=AssertionError("Must not send"))
    settings = Settings(broker="mock", llm_provider="rules", database_url="sqlite:///:memory:",
                        ticker_interval=None, reconcile_interval=None)
    with TestClient(create_app(settings, broker=broker, clock=lambda: NOW)) as client:
        state = client.app.state
        state.profile_store.save_profile(PROFILE.model_copy(update={
            "hard_order_limit": True, "hard_stop_on_daily_loss": True,
            "hard_cooling_off": True, "hard_stop_on_goal_loss": True}))
        state.profile_store.save_goal(goal())
        p = pending()
        state.pending.put(p)
        with patch("app.risk.engine.compute_today", new_callable=AsyncMock) as compute:
            if failure == "timeout":
                compute.side_effect = BrokerTimeout("offline")
            else:
                compute.return_value = FACTS.model_copy(update={
                    "orders_today": 6 if failure == "order_limit" else 0,
                    "pnl_after_charges": -20_000 if failure == "loss_limit" else 0,
                    "consecutive_losses": 3 if failure == "cooldown" else 0,
                    "last_loss_at": NOW,
                    "portfolio_value": 900_000 if failure == "goal_loss" else 1_000_000})
            response = client.post(f"/api/approvals/{p.id}/approve", json={"order_hash": p.order_hash})
        assert response.status_code == 409
        assert response.json()["code"] == "BLOCKED"
        assert state.pending.get(p.id).state.value == "VOID"
        broker.place_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_preview_service_attaches_warning_or_blocks_without_card():
    from app.schemas import OrderIntent

    settings = Settings(broker="mock", llm_provider="rules", database_url="sqlite:///:memory:",
                        ticker_interval=None, reconcile_interval=None)
    broker = MockBroker(clock=lambda: NOW)
    app = create_app(settings, broker=broker, clock=lambda: NOW)
    app.state.profile_store.save_profile(PROFILE)
    intent = OrderIntent(action="PLACE", instrument_ref="INFY", side="BUY", quantity=1,
                         order_type="LIMIT", limit_price=145_000)
    with patch("app.risk.engine.compute_today", new_callable=AsyncMock, return_value=FACTS.model_copy(update={"orders_today": 6})):
        soft = await app.state.cards.propose(intent)
        assert soft.status == "card_created"
        assert any("orders a day" in w for w in soft.pending.warnings)
        app.state.profile_store.save_profile(PROFILE.model_copy(update={"hard_order_limit": True}))
        hard = await app.state.cards.propose(intent)
        assert hard.status == "blocked" and hard.pending is None


@pytest.mark.parametrize("stage", ["preview", "approve"])
@pytest.mark.parametrize("streak,seconds,blocked", [(2, 0, False), (3, 0, True),
    (3, 1199, True), (3, 1200, False), (3, -1, False)])
def test_opt_in_cooldown_boundaries(stage, streak, seconds, blocked):
    facts = dict(consecutive_losses=streak, last_loss_at=NOW-timedelta(seconds=seconds))
    assert bool(verdict(stage=stage, profile=PROFILE.model_copy(update={"hard_cooling_off": True}),
                        **facts).block) is blocked
    assert verdict(stage=stage, **facts).block is None


@pytest.mark.parametrize("stage", ["preview", "approve"])
@pytest.mark.parametrize("portfolio,blocked", [(900_001, False), (900_000, True), (899_999, True)])
def test_opt_in_goal_stop_uses_baseline_not_overlapping_daily_loss(stage, portfolio, blocked):
    profile = PROFILE.model_copy(update={"hard_stop_on_goal_loss": True})
    result = verdict(stage=stage, profile=profile, goal=goal(), portfolio_value=portfolio,
                     pnl_after_charges=-100_000)
    assert bool(result.block) is blocked
    assert verdict(stage=stage, goal=goal(), portfolio_value=portfolio).block is None


@pytest.mark.parametrize("g", [None, goal(start_date=NOW.date()+timedelta(days=1)),
                               goal(start_date=NOW.date()-timedelta(days=3), end_date=NOW.date()-timedelta(days=1))])
def test_goal_stop_requires_goal_in_its_date_window(g):
    assert verdict(profile=PROFILE.model_copy(update={"hard_stop_on_goal_loss": True}),
                   goal=g, portfolio_value=800_000).block is None


@pytest.mark.parametrize("action", ["CANCEL", "MODIFY"])
@pytest.mark.parametrize("stage", ["preview", "approve"])
def test_new_stops_never_block_cancellations_or_modifications(action, stage):
    assert verdict(p=pending(action=action, target_order_id="old"), stage=stage,
                   profile=PROFILE.model_copy(update={"hard_cooling_off": True, "hard_stop_on_goal_loss": True}),
                   goal=goal(), portfolio_value=800_000, consecutive_losses=3, last_loss_at=NOW).block is None


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", ["hard_cooling_off", "hard_stop_on_goal_loss"])
async def test_new_stops_refresh_facts_at_approval_and_can_be_switched_off(store, flag):
    store.save_profile(PROFILE.model_copy(update={flag: True}))
    store.save_goal(goal())
    guard = ProfileGuard(AsyncMock(), store, lambda: NOW)
    with patch("app.risk.engine.compute_today", new_callable=AsyncMock) as compute:
        compute.return_value = FACTS
        assert (await guard.check(pending(), "preview")).block is None
        compute.return_value = FACTS.model_copy(update={"portfolio_value": 900_000,
            "consecutive_losses": 3, "last_loss_at": NOW})
        assert (await guard.check(pending(), "approve")).block
        assert compute.await_count == 2
        store.save_profile(PROFILE)
        assert (await guard.check(pending(), "approve")).block is None


def test_legacy_profiles_default_new_stops_off():
    from app.risk.models import RiskProfile
    raw = PROFILE.model_dump()
    raw.pop("hard_cooling_off")
    raw.pop("hard_stop_on_goal_loss")
    loaded = RiskProfile.model_validate(raw)
    assert loaded.hard_cooling_off is False
    assert loaded.hard_stop_on_goal_loss is False



def test_cooldown_survives_restart_day_rollover_and_streak_reset(tmp_path):
    from app.history.store import trading_day
    url = "sqlite:///" + str(tmp_path / "cooldown.db")
    db = Database(url)
    first = ProfileStore(db)
    profile = PROFILE.model_copy(update={"hard_cooling_off": True, "cooling_off_minutes": 60})
    first.save_profile(profile)
    late = NOW.replace(hour=18, minute=15)  # 23:45 IST
    facts = FACTS.model_copy(update={"day": trading_day(late), "consecutive_losses": 3, "last_loss_at": late})
    assert first.cooldown_until(profile, facts, late) == late+timedelta(hours=1)
    db.close()
    db = Database(url)
    try:
        store = ProfileStore(db)
        later = late+timedelta(minutes=30)
        reset = FACTS.model_copy(update={"day": trading_day(later), "consecutive_losses": 0, "last_loss_at": None})
        until = store.cooldown_until(store.get_profile(), reset, later)
        assert until == late+timedelta(hours=1)
        assert evaluate(pending(), "approve", profile, reset, None, later, cooldown_until=until).block
        assert store.cooldown_until(profile, reset, late+timedelta(hours=1)) is None
    finally:
        db.close()


def test_switching_cooldown_off_clears_latched_pause(store):
    profile = PROFILE.model_copy(update={"hard_cooling_off": True})
    store.save_profile(profile)
    facts = FACTS.model_copy(update={"consecutive_losses": 3, "last_loss_at": NOW})
    assert store.cooldown_until(profile, facts, NOW)
    store.save_profile(PROFILE)
    store.save_profile(profile)
    assert store.cooldown_until(profile, FACTS, NOW) is None


@pytest.mark.parametrize("turnover,quantity,warns", [(0,10,False),(1,10,True),(100000,1,True)])
def test_optional_turnover_allowance(turnover,quantity,warns):
    result=verdict(p=pending(quantity=quantity),profile=PROFILE.model_copy(update={"daily_turnover_limit_paise":100000}),turnover=turnover)
    assert has(result,"daily turnover allowance") is warns
    assert result.block is None


@pytest.mark.parametrize("turnover,charges,warns", [(0,100,False),(100000,999,False),(100000,1000,True)])
def test_optional_charge_ratio(turnover,charges,warns):
    result=verdict(profile=PROFILE.model_copy(update={"charges_turnover_limit_pct":1.0}),turnover=turnover,charges=charges)
    assert has(result,"charges-to-turnover") is warns


def test_pace_warning_includes_proposed_plan_steps_but_is_not_hard_stop():
    p=PROFILE.model_copy(update={"short_window_order_limit":3})
    result=evaluate(pending(),"preview",p,FACTS.model_copy(update={"recent_orders":2}),None,NOW,extra_orders=1)
    assert has(result,"20-minute order allowance")
    assert result.block is None
