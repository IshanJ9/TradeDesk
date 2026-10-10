"""The overtrading zone: pure arithmetic over the trader's own limits, and it agrees with the warnings the risk engine
already shows ("You set ..."), including the exact boundary (strictly over the limit, the proposed order counted)."""

import pytest

from app.cocaptain.zone import POLICY_VERSION, in_overtrading_zone
from app.risk.engine import evaluate
from test_risk_engine import FACTS, NOW, PROFILE, pending

LIMITED = PROFILE.model_copy(update={"max_orders_per_day": 5, "short_window_order_limit": 3,
                                     "daily_turnover_limit_paise": 1_000_000})  # Rs 10,000


def facts(**kw):
    return FACTS.model_copy(update=kw)


def zone(profile=LIMITED, orders=1, value=0, **kw):
    return in_overtrading_zone(profile, facts(**kw), orders=orders, value=value)


# ---- the boundaries: the order being approved is counted, and only strictly over the limit is "past" ------- #


def test_orders_per_day_boundary():
    assert not zone(orders_today=4).in_zone  # this would be order 5 of 5
    over = zone(orders_today=5)  # this would be order 6 of 5
    assert over.in_zone and "You set 5 orders a day; this would take you to 6." in over.reasons


def test_the_twenty_minute_allowance_boundary():
    assert not zone(recent_orders=2).in_zone  # this would be the 3rd of 3
    over = zone(recent_orders=3)
    assert over.in_zone and any("20-minute allowance of 3 orders; this would make 4" in r for r in over.reasons)


def test_turnover_boundary_counts_the_value_of_this_order():
    assert not zone(turnover=900_000, value=100_000).in_zone  # exactly at the allowance: not past it
    over = zone(turnover=900_000, value=100_001)
    assert over.in_zone and any("daily turnover allowance of ₹10,000.00" in r for r in over.reasons)


def test_a_plan_counts_all_its_steps_and_its_total_value():
    assert not zone(orders_today=2, orders=3).in_zone  # 2 + 3 = 5 orders
    assert zone(orders_today=3, orders=3).in_zone  # 3 + 3 = 6
    assert zone(turnover=0, orders=2, value=1_000_001).in_zone


def test_a_limit_that_is_not_set_is_ignored_and_no_profile_means_never_in_the_zone():
    loose = PROFILE.model_copy(update={"max_orders_per_day": 100, "short_window_order_limit": None,
                                       "daily_turnover_limit_paise": None})
    assert not zone(profile=loose, orders_today=50, recent_orders=40, turnover=10**12, value=10**12).in_zone
    none = in_overtrading_zone(None, facts(), orders=1, value=10**12)
    assert not none.in_zone and not none.configured


def test_the_decision_carries_the_policy_version_and_every_reason():
    d = zone(orders_today=9, recent_orders=9, turnover=2_000_000, value=1)
    assert d.policy_version == POLICY_VERSION == "zone-v1" and d.configured
    assert len(d.reasons) == 3


# ---- it agrees with the warnings the risk engine already shows ---------------------------------------------- #


CASES = [dict(), dict(orders_today=4), dict(orders_today=5), dict(recent_orders=2), dict(recent_orders=3),
         dict(turnover=900_000), dict(turnover=1_000_000), dict(orders_today=9, recent_orders=9, turnover=5_000_000)]


@pytest.mark.parametrize("kw", CASES)
def test_the_zone_and_the_engines_own_warnings_agree(kw):
    p = pending(quantity=10, limit_price=10_000)  # a Rs 1,00,000 order: its value is counted for turnover
    value = 10 * 10_000
    result = evaluate(p, "preview", LIMITED, facts(**kw), None, NOW)
    engine_says = {
        "orders": any("order a day" in w or "orders a day" in w for w in result.warnings),
        "window": any("20-minute order allowance" in w for w in result.warnings),
        "turnover": any("daily turnover allowance" in w for w in result.warnings),
    }
    mine = zone(orders=1, value=value, **kw)
    assert mine.in_zone == any(engine_says.values())
    assert any("a day" in r for r in mine.reasons) == engine_says["orders"]
    assert any("20-minute" in r for r in mine.reasons) == engine_says["window"]
    assert any("turnover" in r for r in mine.reasons) == engine_says["turnover"]
