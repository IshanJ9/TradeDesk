"""Person C, Step 1. Every broker here is a mock; no external services are contacted."""

from datetime import date, datetime, timezone
from itertools import product
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.broker.base import BrokerTimeout
from app.broker.mock import MockBroker
from app.config import Settings
from app.db import Database
from app.main import create_app
from app.risk.models import Goal, GoalRequest, OnboardingAnswers, RiskProfile
from app.risk.presets import all_presets, preset, suggest_profile
from app.risk.store import ProfileStore
from app.schemas import Exchange, Funds, Holding, Instrument, Position, Product

NOW = datetime(2026, 10, 9, 20, 0, tzinfo=timezone.utc)  # October 10 in India


@pytest.fixture
def client():
    settings = Settings(broker="mock", llm_provider="rules", ticker_interval=None, reconcile_interval=None)
    broker = MockBroker(clock=lambda: NOW)
    broker.place_order = AsyncMock(side_effect=AssertionError("risk settings must never send orders"))
    broker.modify_order = AsyncMock(side_effect=AssertionError("risk settings must never modify orders"))
    broker.cancel_order = AsyncMock(side_effect=AssertionError("risk settings must never cancel orders"))
    with TestClient(create_app(settings, broker=broker, clock=lambda: NOW)) as c:
        yield c
    broker.place_order.assert_not_awaited()
    broker.modify_order.assert_not_awaited()
    broker.cancel_order.assert_not_awaited()


def profile_payload(**updates):
    return {**preset("balanced").model_dump(mode="json"), **updates}


def goal_payload(**updates):
    return {"target_paise": 100_000, "end_date": "2026-12-01", "max_acceptable_loss_paise": 50_000, **updates}


@pytest.mark.parametrize("name,expected", [
    ("conservative", (3, 10, 20, 1, 2, 30, 60, False)),
    ("balanced", (6, 20, 30, 2, 3, 20, 30, True)),
    ("aggressive", (12, 35, 45, 4, 4, 10, 15, True)),
])
def test_exact_starting_values_and_hard_limits_off(name, expected):
    p = preset(name)
    assert (p.max_orders_per_day, p.max_order_pct, p.max_stock_pct, p.daily_loss_limit_pct,
            p.cooling_off_after_losses, p.cooling_off_minutes, p.reentry_minutes, p.intraday_allowed) == expected
    assert p.style == name
    assert not p.hard_order_limit and not p.hard_stop_on_daily_loss and not p.hide_day_pnl
    assert all("not recommendations" in option.note for option in all_presets())


def test_presets_are_immutable_independent_values():
    p = preset("balanced")
    with pytest.raises(ValidationError):
        p.max_orders_per_day = 90
    changed = p.model_copy(update={"max_orders_per_day": 90})
    assert changed.max_orders_per_day == 90
    assert preset("balanced").max_orders_per_day == 6


@pytest.mark.parametrize("field", ["max_order_pct", "max_stock_pct", "daily_loss_limit_pct"])
@pytest.mark.parametrize("value", [0, 0.99, 100.01, float("nan"), float("inf"), True, "10"])
def test_profile_rejects_invalid_percentages(field, value):
    with pytest.raises(ValidationError):
        RiskProfile(**profile_payload(**{field: value}))


@pytest.mark.parametrize("field", ["max_orders_per_day", "cooling_off_after_losses", "cooling_off_minutes", "reentry_minutes"])
@pytest.mark.parametrize("value", [0, -1, 1.5, True, "3"])
def test_profile_requires_positive_integer_counts(field, value):
    with pytest.raises(ValidationError):
        RiskProfile(**profile_payload(**{field: value}))


@pytest.mark.parametrize("field,maximum", [
    ("max_orders_per_day", 100_000), ("cooling_off_after_losses", 100_000),
    ("cooling_off_minutes", 1440), ("reentry_minutes", 1440),
    ("max_order_pct", 100), ("max_stock_pct", 100), ("daily_loss_limit_pct", 100),
])
def test_profile_boundaries(field, maximum):
    assert getattr(RiskProfile(**profile_payload(**{field: 1})), field) == 1
    assert getattr(RiskProfile(**profile_payload(**{field: maximum})), field) == maximum
    with pytest.raises(ValidationError):
        RiskProfile(**profile_payload(**{field: maximum + 1}))


@pytest.mark.parametrize("field", ["hard_order_limit", "hard_stop_on_daily_loss", "intraday_allowed", "hide_day_pnl"])
@pytest.mark.parametrize("value", [1, "true", None])
def test_switches_require_explicit_booleans(field, value):
    with pytest.raises(ValidationError):
        RiskProfile(**profile_payload(**{field: value}))


def test_profile_api_empty_save_edit_and_reject_invalid(client):
    assert client.get("/api/profile").json() is None
    p = profile_payload(style="custom", max_orders_per_day=9, hard_order_limit=True)
    response = client.put("/api/profile", json=p)
    assert response.status_code == 200
    assert response.json() == p
    assert client.get("/api/profile").json() == p
    invalid = client.put("/api/profile", json={**p, "max_orders_per_day": 0})
    assert invalid.status_code == 422
    assert client.put("/api/profile", json={**p, "approved": True}).status_code == 422
    assert client.put("/api/profile", json={**p, "style": "invented"}).status_code == 422
    assert client.get("/api/profile").json() == p
    p.update(hard_order_limit=False, daily_loss_limit_pct=3)
    assert client.put("/api/profile", json=p).json() == p
    assert client.get("/api/profile").json() == p


def test_all_questionnaire_combinations_and_boundaries():
    loss = ["small", "moderate", "larger"]
    period = ["weeks_or_more", "days", "same_day"]
    orders = ["up_to_three", "four_to_six", "seven_or_more"]
    aim = ["preserve_capital", "steady_progress", "active_trading"]
    seen_scores = set()
    for a, b, c, intraday, d in product(range(3), range(3), range(3), (False, True), range(3)):
        answers = OnboardingAnswers(daily_loss_comfort=loss[a], holding_period=period[b],
                                    usual_orders=orders[c], intraday_allowed=intraday, aim=aim[d])
        result = suggest_profile(answers)
        score = a + b + c + 2 * int(intraday) + d
        seen_scores.add(score)
        expected = "conservative" if score < 4 else "balanced" if score < 7 else "aggressive"
        assert result.preset == expected
        assert result.profile.intraday_allowed is intraday
        assert not result.profile.hard_order_limit and not result.profile.hard_stop_on_daily_loss
        assert result.requires_review is True
    assert seen_scores == set(range(11))


def test_onboarding_and_presets_do_not_save_until_explicit_put(client):
    options = client.get("/api/profile/presets")
    assert options.status_code == 200 and len(options.json()) == 3
    answers = dict(daily_loss_comfort="small", holding_period="weeks_or_more", usual_orders="up_to_three",
                   intraday_allowed=False, aim="preserve_capital")
    r = client.post("/api/profile/onboarding", json=answers)
    assert r.status_code == 200 and r.json()["preset"] == "conservative"
    assert client.get("/api/profile").json() is None
    assert client.post("/api/profile/onboarding", json={**answers, "aim": "guaranteed_returns"}).status_code == 422
    assert client.put("/api/profile", json=r.json()["profile"]).status_code == 200
    assert client.get("/api/profile").json()["style"] == "conservative"


@pytest.mark.parametrize("updates", [
    {"target_paise": None}, {"target_pct": 10}, {"target_paise": 0}, {"target_paise": 1.1},
    {"target_paise": True}, {"target_paise": "100"}, {"target_paise": None, "target_pct": 0},
    {"target_paise": None, "target_pct": 101}, {"max_acceptable_loss_paise": 0},
    {"max_acceptable_loss_paise": 1.5}, {"start_value": 1},
    {"start_date": "2026-10-11", "end_date": "2026-10-10"},
])
def test_invalid_goal_input_is_rejected(client, updates):
    assert client.put("/api/goal", json=goal_payload(**updates)).status_code == 422
    assert client.get("/api/goal").json() is None


def test_goal_uses_indian_date_and_server_portfolio_value(client):
    assert client.get("/api/goal").json() is None
    broker = client.app.state.broker
    inst = Instrument(symbol="DEMO", exchange=Exchange.NSE)
    broker.get_funds = AsyncMock(return_value=Funds(available_cash=100_000, used_margin=20_000))
    broker.get_holdings = AsyncMock(return_value=[
        Holding(instrument=inst, quantity=2, avg_price=1000, ltp=1100, prev_close=1050)])
    broker.get_positions = AsyncMock(return_value=[
        Position(instrument=inst, quantity=3, avg_price=1000, ltp=1100, prev_close=1050, product=Product.CNC)])
    r = client.put("/api/goal", json=goal_payload())
    assert r.status_code == 200
    assert r.json()["start_date"] == "2026-10-10"
    assert r.json()["start_value"] == 125_500
    assert r.json()["target_paise"] == 100_000
    assert client.get("/api/goal").json() == r.json()
    updated = client.put("/api/goal", json=goal_payload(target_paise=None, target_pct=10))
    assert updated.status_code == 200
    assert updated.json()["target_paise"] is None and updated.json()["target_pct"] == 10
    assert client.delete("/api/goal").status_code == 204
    assert client.get("/api/goal").json() is None
    assert client.delete("/api/goal").status_code == 204


@pytest.mark.parametrize("updates", [
    {"end_date": "2026-10-10"}, {"end_date": "2026-10-09"},
    {"start_date": "2026-10-09"}, {"start_date": "2026-10-11"},
])
def test_goal_refuses_expired_or_unverifiable_start_dates(client, updates):
    assert client.put("/api/goal", json=goal_payload(**updates)).status_code == 422


def test_goal_positive_money_and_percentage_boundaries():
    for pct in (1, 100):
        assert GoalRequest(**goal_payload(target_paise=None, target_pct=pct)).target_pct == pct
    assert GoalRequest(**goal_payload(target_paise=1)).target_paise == 1
    for field in ("target_paise", "max_acceptable_loss_paise"):
        with pytest.raises(ValidationError):
            GoalRequest(**goal_payload(**{field: 9_007_199_254_740_992}))


def test_failed_broker_read_preserves_existing_goal(client):
    before = client.put("/api/goal", json=goal_payload()).json()
    client.app.state.broker.get_funds = AsyncMock(side_effect=BrokerTimeout())
    assert client.put("/api/goal", json=goal_payload(target_paise=200_000)).status_code == 503
    assert client.get("/api/goal").json() == before


def test_zero_portfolio_and_excess_loss_budget_rejected(client):
    broker = client.app.state.broker
    broker.get_holdings = AsyncMock(return_value=[])
    broker.get_positions = AsyncMock(return_value=[])
    broker.get_funds = AsyncMock(return_value=Funds(available_cash=0))
    assert client.put("/api/goal", json=goal_payload()).status_code == 409
    broker.get_funds = AsyncMock(return_value=Funds(available_cash=100))
    assert client.put("/api/goal", json=goal_payload()).status_code == 422
    assert client.get("/api/goal").json() is None
    assert client.put("/api/goal", json=goal_payload(max_acceptable_loss_paise=100)).status_code == 200


def test_settings_persist_across_database_reopen(tmp_path):
    url = f"sqlite:///{(tmp_path / 'risk.db').as_posix()}"
    db = Database(url)
    store = ProfileStore(db)
    p = preset("aggressive")
    g = Goal(target_pct=10, start_date=date(2026, 10, 10), end_date=date(2026, 12, 1),
             start_value=1_000_000, max_acceptable_loss_paise=100_000)
    store.save_profile(p)
    store.save_goal(g)
    # Updating one record doesn't alter the other or multiply the singleton rows.
    store.save_profile(preset("conservative"))
    store.save_profile(p)
    assert len(db.query("SELECT id FROM risk_profile")) == 1
    db.close()
    reopened = Database(url)
    restored = ProfileStore(reopened)
    assert restored.get_profile() == p
    assert restored.get_goal() == g
    restored.delete_goal()
    assert restored.get_profile() == p
    reopened.close()


def test_openapi_exposes_owned_models(client):
    spec = client.get("/openapi.json").json()
    assert "/api/profile" in spec["paths"] and "/api/goal" in spec["paths"]
    schemas = spec["components"]["schemas"]
    assert {"Goal", "GoalRequest", "OnboardingAnswers"} <= set(schemas)
    request_schema = spec["paths"]["/api/profile"]["put"]["requestBody"]["content"]["application/json"]["schema"]
    profile = schemas[request_schema["$ref"].rsplit("/", 1)[-1]]
    assert {"max_orders_per_day", "hard_order_limit", "hard_stop_on_daily_loss"} <= set(profile["properties"])
