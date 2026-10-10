from datetime import datetime, timedelta, timezone

import pytest

from app.cocaptain.zone import POLICY_VERSION, ZoneLimits, in_overtrading_zone
from app.risk.presets import preset
from app.risk.today import calculate_today
from app.schemas import Funds, Instrument, PendingOrder, Plan, PlanLeg

NOW = datetime(2026, 10, 10, 5, tzinfo=timezone.utc)


def card(index=0, **changes):
    fields = dict(id=f"p{index}", action="PLACE", instrument=Instrument(symbol="ITC", exchange="NSE"),
                  side="BUY", quantity=2, order_type="LIMIT", limit_price=10_000,
                  client_order_id=f"c{index}", ref_ltp=10_000, created_at=NOW,
                  expires_at=NOW + timedelta(minutes=1))
    return PendingOrder(**(fields | changes))


def facts(**changes):
    return calculate_today(orders=[], holdings=[], positions=[], funds=Funds(available_cash=1_000_000),
                           profile=preset("balanced"), now=NOW).model_copy(update=changes)


@pytest.mark.parametrize("existing,expected", [(3, False), (4, False), (5, True)])
@pytest.mark.parametrize("limit,field", [("max_orders_per_day", "orders_today"),
                                         ("short_window_order_limit", "recent_orders")])
def test_count_boundaries_include_proposed_order(limit, field, existing, expected):
    decision = in_overtrading_zone(ZoneLimits(**{limit: 5}), facts(**{field: existing}), card())
    assert decision.in_zone is expected
    assert decision.proposed_orders == 1
    assert decision.policy_version == POLICY_VERSION
    assert bool(decision.reasons) is expected


@pytest.mark.parametrize("filled,expected", [(79_999, False), (80_000, False), (80_001, True)])
def test_turnover_boundary_uses_filled_plus_proposed(filled, expected):
    result = in_overtrading_zone(ZoneLimits(daily_turnover_limit_paise=100_000), facts(turnover=filled), card())
    assert result.in_zone is expected
    assert result.proposed_value_paise == 20_000


@pytest.mark.parametrize("profile", [None, ZoneLimits()])
def test_no_configured_limits_never_enters_zone(profile):
    result = in_overtrading_zone(profile, facts(orders_today=100, turnover=1_000_000, recent_orders=100), card())
    assert not result.in_zone and not result.configured and result.reasons == []


def test_plan_counts_every_leg_and_gross_value_of_both_sides():
    plan = Plan(id="plan", title="Two steps", legs=[
        PlanLeg(index=0, order=card(0, side="SELL")), PlanLeg(index=1, order=card(1))],
        created_at=NOW, expires_at=NOW + timedelta(minutes=1))
    result = in_overtrading_zone(ZoneLimits(5, 100_000, 3),
                                facts(orders_today=4, turnover=60_001, recent_orders=2), plan)
    assert result.proposed_orders == 2 and result.proposed_value_paise == 40_000
    assert result.in_zone and len(result.reasons) == 3
    assert "would be 6" in result.reasons[0]


@pytest.mark.parametrize("action", ["CANCEL", "MODIFY"])
def test_existing_order_changes_do_not_require_coapproval(action):
    result = in_overtrading_zone(ZoneLimits(1, 1, 1), facts(orders_today=100, turnover=100, recent_orders=100),
                                card(action=action, target_order_id="existing"))
    assert not result.in_zone and result.proposed_orders == 0


def test_actual_profile_matches_warning_daily_boundary():
    profile = preset("balanced")
    assert not in_overtrading_zone(profile, facts(orders_today=profile.max_orders_per_day-1), card()).in_zone
    assert in_overtrading_zone(profile, facts(orders_today=profile.max_orders_per_day), card()).in_zone
