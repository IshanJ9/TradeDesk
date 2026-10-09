import pytest

from app.config import Settings
from app.orders.charges import compute_charges, estimated_total
from app.orders.limits import HardLimits, OrderBlocked, check_instrument, check_locks, check_price, check_size
from app.schemas import (
    AccountLocks,
    Exchange,
    Instrument,
    OptionType,
    OrderAction,
    Product,
    RejectionReason,
    Side,
    paise,
)

from datetime import date

# ---- charges: 021's published rates ------------------------------------------------ #


def test_delivery_buy_matches_a_hand_calculation():
    """BUY 10 INFY @ Rs 1,450, delivery, NSE. Value Rs 14,500 = 1,450,000 paise."""
    c = compute_charges(exchange=Exchange.NSE, side=Side.BUY, product=Product.CNC, quantity=10, price=paise(1450))
    assert c.brokerage == 435  # 0.03% = Rs 4.35, under the Rs 20 cap
    assert c.stt == 1450  # 0.1%
    assert c.exchange_txn == 43  # 0.00297%
    assert c.sebi_fee == 1 and c.ipft == 1  # 0.0001% each
    assert c.stamp_duty == 218  # 0.015% on buys (217.5 rounds half up)
    assert c.gst == 125  # 18% of (brokerage + stamp + exchange + ipft) = 0.18 * 697
    assert c.dp_charge == 0 and c.clearing == 0
    assert c.total == 2273


def test_estimated_total_for_the_pitch_example_is_close_to_14520():
    c = compute_charges(exchange=Exchange.NSE, side=Side.BUY, product=Product.CNC, quantity=10, price=paise(1450))
    assert estimated_total(Side.BUY, 10, paise(1450), c) == paise(14500) + 2273


def test_brokerage_is_capped_at_rs_20():
    c = compute_charges(exchange=Exchange.NSE, side=Side.BUY, product=Product.CNC, quantity=1000, price=paise(1450))
    assert c.brokerage == 2000  # 0.03% of Rs 14.5 lakh would be Rs 435


def test_delivery_sell_has_stt_but_no_stamp_and_pays_the_dp_charge():
    c = compute_charges(exchange=Exchange.NSE, side=Side.SELL, product=Product.CNC, quantity=10, price=paise(1450))
    assert c.stamp_duty == 0 and c.stt == 1450 and c.dp_charge == 1550
    assert c.gst == round(0.18 * (435 + 0 + 43 + 1 + 1550))
    assert estimated_total(Side.SELL, 10, paise(1450), c) == paise(14500) - c.total


def test_intraday_rates_differ():
    buy = compute_charges(exchange=Exchange.NSE, side=Side.BUY, product=Product.MIS, quantity=10, price=paise(1450))
    sell = compute_charges(exchange=Exchange.NSE, side=Side.SELL, product=Product.MIS, quantity=10, price=paise(1450))
    assert buy.stt == 0 and buy.stamp_duty == 44  # 0.003% = 43.5 -> 44
    assert sell.stt == 363 and sell.stamp_duty == 0 and sell.dp_charge == 0  # 0.025% = 362.5 -> 363


def test_bse_uses_its_own_transaction_rate():
    nse = compute_charges(exchange=Exchange.NSE, side=Side.BUY, product=Product.CNC, quantity=100, price=paise(1000))
    bse = compute_charges(exchange=Exchange.BSE, side=Side.BUY, product=Product.CNC, quantity=100, price=paise(1000))
    assert bse.exchange_txn > nse.exchange_txn


def test_break_even_covers_both_legs():
    c = compute_charges(exchange=Exchange.NSE, side=Side.BUY, product=Product.CNC, quantity=10, price=paise(1450))
    sell = compute_charges(exchange=Exchange.NSE, side=Side.SELL, product=Product.CNC, quantity=10, price=paise(1450))
    round_trip = c.total + sell.total
    assert c.break_even_price > paise(1450)
    # selling at the break-even price recovers at least the round-trip charges, per share
    assert (c.break_even_price - paise(1450)) * 10 >= round_trip
    assert (c.break_even_price - paise(1450) - 1) * 10 < round_trip  # and it's the lowest whole-paisa price that does


def test_sell_break_even_is_below_the_price():
    c = compute_charges(exchange=Exchange.NSE, side=Side.SELL, product=Product.CNC, quantity=10, price=paise(1450))
    assert 0 < c.break_even_price < paise(1450)


# ---- hard limits --------------------------------------------------------------------- #

LIMITS = HardLimits()


def inst(**kw):
    base = dict(symbol="INFY", exchange=Exchange.NSE, price_band_low=paise(1152), price_band_high=paise(1728))
    base.update(kw)
    return Instrument(**base)


def blocked(fn, *args, **kw) -> OrderBlocked:
    with pytest.raises(OrderBlocked) as exc:
        fn(*args, **kw)
    return exc.value


def test_021s_published_limits_are_the_defaults():
    assert LIMITS.max_quantity == 100_000
    assert LIMITS.max_order_value == paise(10_000_000)  # Rs 1 crore


def test_limits_come_from_settings_so_demos_can_tighten_them():
    tight = HardLimits.from_settings(Settings(max_order_quantity=50, max_order_value_rupees=100_000))
    assert tight.max_quantity == 50 and tight.max_order_value == paise(100_000)


def test_quantity_limit():
    check_size(100_000, paise(1), LIMITS)
    assert blocked(check_size, 100_001, paise(1), LIMITS).reason is RejectionReason.QUANTITY_LIMIT


def test_value_limit_is_one_crore():
    check_size(1, paise(10_000_000), LIMITS)
    err = blocked(check_size, 1, paise(10_000_000) + 100, LIMITS)
    assert err.reason is RejectionReason.VALUE_LIMIT and "1,00,00,000" in err.message


def test_only_equity_may_be_traded():
    check_instrument(inst(), LIMITS)
    index = inst(symbol="NIFTY", series="INDEX")
    option = inst(
        symbol="NIFTY261013024500CE", option_type=OptionType.CE, expiry=date(2026, 10, 13), strike=paise(24500), lot_size=75
    )
    assert blocked(check_instrument, index, LIMITS).reason is RejectionReason.SEGMENT_NOT_ALLOWED
    assert blocked(check_instrument, option, LIMITS).reason is RejectionReason.SEGMENT_NOT_ALLOWED


def test_suspended_stock_is_blocked():
    assert blocked(check_instrument, inst(suspended=True), LIMITS).reason is RejectionReason.SUSPENDED


def test_limit_price_must_be_on_the_tick_grid_and_inside_the_band():
    check_price(inst(), paise(1450))
    assert blocked(check_price, inst(), paise("1450.03")).reason is RejectionReason.INVALID_PRICE
    assert blocked(check_price, inst(), paise(1000)).reason is RejectionReason.PRICE_BAND
    assert blocked(check_price, inst(), paise(2000)).reason is RejectionReason.PRICE_BAND
    check_price(inst(), paise(1152))  # the band edges themselves are allowed
    check_price(inst(), paise(1728))


# ---- locks ---------------------------------------------------------------------------- #


def test_anchor_blocks_new_orders_and_shows_the_traders_own_message():
    locks = AccountLocks(anchor_active=True, anchor_message="Sleep on it. The market will be there tomorrow.")
    for action in (OrderAction.PLACE, OrderAction.MODIFY):
        err = blocked(check_locks, locks, action)
        assert err.reason is RejectionReason.ANCHOR_ACTIVE
        assert "Sleep on it" in err.message


def test_anchor_still_lets_you_cancel():
    check_locks(AccountLocks(anchor_active=True), OrderAction.CANCEL)


def test_co_captain_lock_blocks_new_orders():
    err = blocked(check_locks, AccountLocks(co_captain_locked=True, co_captain_message="Step back"), OrderAction.PLACE)
    assert err.reason is RejectionReason.CO_APPROVAL_REQUIRED and "Step back" in err.message
    check_locks(AccountLocks(co_captain_locked=True), OrderAction.CANCEL)


def test_orders_above_the_co_approval_limit_are_blocked_until_that_flow_exists():
    locks = AccountLocks(co_approval_limit=paise(50_000))
    check_locks(locks, OrderAction.PLACE, paise(50_000))
    assert blocked(check_locks, locks, OrderAction.PLACE, paise(50_001)).reason is RejectionReason.CO_APPROVAL_REQUIRED


def test_no_locks_no_problem():
    check_locks(AccountLocks(), OrderAction.PLACE, paise(1_000_000))
