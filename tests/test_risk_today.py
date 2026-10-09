"""Daily facts use only fake snapshots, never a live broker."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from app.broker.base import BrokerTimeout
from app.orders.charges import compute_charges
from app.risk.presets import preset
from app.risk.today import calculate_today, compute_today
from app.schemas import Exchange, Funds, Holding, Instrument, Order, OrderStatus, Position, Product, Side

NOW = datetime(2026, 10, 9, 10, tzinfo=timezone.utc)
STOCK = Instrument(symbol="TEST", exchange=Exchange.NSE)
PROFILE = preset("balanced").model_copy(update={"cooling_off_after_losses": 2})


def order(n, side="BUY", quantity=10, price=10_000, minute=0, **updates):
    timestamp = NOW - timedelta(hours=2) + timedelta(minutes=minute)
    return Order(order_id=str(n), instrument=STOCK, side=side, quantity=quantity,
                 filled_quantity=updates.pop("filled_quantity", quantity),
                 avg_fill_price=updates.pop("avg_fill_price", price),
                 order_type="LIMIT", limit_price=price, product=updates.pop("product", "MIS"),
                 status=updates.pop("status", "FILLED"),
                 created_at=updates.pop("created_at", timestamp),
                 updated_at=updates.pop("updated_at", timestamp), **updates)


def facts(orders=(), holdings=(), positions=(), **kw):
    return calculate_today(orders=list(orders), holdings=list(holdings), positions=list(positions),
                           funds=kw.pop("funds", Funds(available_cash=1_000_000)),
                           profile=kw.pop("profile", PROFILE), now=kw.pop("now", NOW), **kw)


def holding(**kw):
    return Holding(instrument=STOCK, quantity=kw.pop("quantity", 10), avg_price=10_000,
                   ltp=12_000, prev_close=11_000, **kw)


def test_empty_day_and_no_positive_portfolio():
    result = facts(funds=Funds(available_cash=0))
    assert result.orders_today == result.turnover == result.charges == result.pnl_estimate == 0
    assert result.largest_order_pct == result.largest_stock_pct == result.intraday_share_pct == 0
    assert result.last_loss_at is None and result.closed_trades == []
    assert any("not positive" in n for n in result.notes)


@pytest.mark.parametrize("status", ["PARTIAL", "CANCELLED", "FILLED"])
def test_partial_quantity_is_used_for_turnover_and_charges(status):
    o = order(1, quantity=100, filled_quantity=3, status=status)
    result = facts([o])
    expected = compute_charges(exchange=Exchange.NSE, side=Side.BUY,
                               product=Product.MIS, quantity=3, price=10_000).total
    assert result.turnover == 30_000 and result.charges == expected
    assert result.largest_order_pct == 100  # full intended size, not filled size
    assert result.orders_today == 1 and result.intraday_share_pct == 100


def test_multiple_fifo_lots_and_partial_closures():
    result = facts([order(1, quantity=10), order(2, quantity=5, price=12_000, minute=1),
                    order(3, "SELL", 12, 11_000, minute=2),
                    order(4, "SELL", 3, 13_000, minute=3)])
    assert [t.pnl for t in result.closed_trades] == [8_000, 3_000]
    assert [t.quantity for t in result.closed_trades] == [12, 3]
    assert result.realised_pnl == 11_000
    assert result.pnl_after_charges == 11_000 - result.charges


def test_short_sell_then_buy_and_flip_to_long():
    result = facts([order(1, "SELL", 10, 12_000), order(2, "BUY", 15, 10_000, minute=1),
                    order(3, "SELL", 5, 11_000, minute=2)])
    assert [t.pnl for t in result.closed_trades] == [20_000, 5_000]


def test_delivery_and_intraday_never_cross_match():
    result = facts([order(1, product="CNC"), order(2, "SELL", price=9_000, minute=1),
                    order(3, price=8_000, minute=2)])
    assert result.realised_pnl == 10_000  # short MIS, not the CNC buy
    assert result.intraday_share_pct == pytest.approx(100 * 170_000 / 270_000)


def test_older_delivery_holding_basis_and_day_pnl():
    result = facts([order(1, "SELL", 5, 12_000, product="CNC")], [holding()])
    assert result.realised_pnl == 10_000
    assert result.unrealised_pnl == 10_000  # remaining holding's day change, not lifetime gain
    assert result.pnl_estimate == 20_000


def test_unknown_older_basis_is_disclosed_without_inventing_short():
    result = facts([order(1, "SELL", product="CNC"), order(2, price=9_000, product="CNC", minute=1)])
    assert result.realised_pnl == 0 and result.closed_trades == []
    assert result.turnover == 190_000 and result.charges > 0
    assert any("missing older-holding cost basis" in n for n in result.notes)


def test_partial_known_fifo_then_missing_old_basis():
    result = facts([order(1, quantity=3, product="CNC"),
                    order(2, "SELL", 5, 9_000, product="CNC", minute=1)])
    assert result.realised_pnl == -3_000 and result.consecutive_losses == 0
    assert any("2 shares" in n for n in result.notes)


def test_sell_matches_today_lots_then_older_holding_for_remainder():
    result = facts([order(1, quantity=3, price=11_000, product="CNC"),
                    order(2, "SELL", 5, 12_000, product="CNC", minute=1)], [holding()])
    assert result.realised_pnl == 7_000  # today's 3 * 1000 plus older 2 * 2000
    assert result.closed_trades[0].quantity == 5


def test_positions_holdings_and_margin_are_counted_once():
    p = Position(instrument=STOCK, quantity=2, avg_price=11_000, ltp=12_000,
                 prev_close=9_000, product=Product.CNC)
    result = facts(holdings=[holding()], positions=[p],
                   funds=Funds(available_cash=100_000, used_margin=10_000))
    assert result.portfolio_value == 254_000
    assert result.stock_values == {STOCK.key: 144_000}
    assert result.largest_stock_pct == pytest.approx(100 * 144_000 / 254_000)
    assert result.unrealised_pnl == 12_000


def test_short_position_open_pnl():
    p = Position(instrument=STOCK, quantity=-2, avg_price=11_000, ltp=12_000, prev_close=9_000)
    assert facts(positions=[p]).unrealised_pnl == -2_000


def loss_orders():
    return [order(1), order(2, "SELL", price=9_000, minute=1),
            order(3, minute=2), order(4, "SELL", price=9_000, minute=3)]


def test_loss_streak_reentry_and_cooling():
    result = facts(loss_orders() + [order(5, minute=4, filled_quantity=0, status="OPEN")])
    assert result.consecutive_losses == 2
    assert result.last_loss_at == NOW - timedelta(hours=2) + timedelta(minutes=3)
    assert result.last_loss_by_stock == {STOCK.key: result.last_loss_at}
    assert result.reentries == 2 and result.cooling_off_breaches == 1


@pytest.mark.parametrize("price", [10_000, 11_000])
def test_flat_or_profitable_close_resets_streak(price):
    result = facts(loss_orders() + [order(5, minute=4), order(6, "SELL", price=price, minute=5)])
    assert result.consecutive_losses == 0
    assert result.last_loss_at == loss_orders()[-1].updated_at


@pytest.mark.parametrize("minutes,expected", [(29, 1), (30, 0), (31, 0)])
def test_reentry_window_boundary(minutes, expected):
    result = facts(loss_orders()[:2] + [order(3, minute=1 + minutes, filled_quantity=0)])
    assert result.reentries == expected


@pytest.mark.parametrize("minutes,expected", [(19, 1), (20, 0), (21, 0)])
def test_cooling_window_boundary(minutes, expected):
    result = facts(loss_orders() + [order(5, minute=3 + minutes, filled_quantity=0)])
    assert result.cooling_off_breaches == expected


def test_other_stock_is_not_reentry():
    other = order(3, minute=2).model_copy(update={"instrument": Instrument(symbol="OTHER", exchange=Exchange.NSE)})
    assert facts(loss_orders()[:2] + [other]).reentries == 0


def test_order_created_before_loss_but_updated_after_is_not_reentry():
    pending = order(3, minute=0, updated_at=loss_orders()[1].updated_at + timedelta(minutes=1))
    assert facts(loss_orders()[:2] + [pending]).reentries == 0


def test_missing_average_fill_does_not_use_limit_price_as_execution():
    result = facts([order(1, avg_fill_price=None)])
    assert result.turnover == result.charges == result.realised_pnl == 0
    assert result.orders_today == 1 and result.largest_order_pct == 10
    assert any("missing average fill price" in n for n in result.notes)


def test_unknown_fill_interrupts_known_loss_streak():
    result = facts(loss_orders() + [order(5, minute=4, avg_fill_price=None)])
    assert result.consecutive_losses == 0


def test_fifo_uses_timestamps_even_when_broker_returns_reverse_order():
    orders = loss_orders()
    assert facts(orders) == facts(list(reversed(orders)))


def test_same_symbol_on_different_exchanges_is_not_same_inventory():
    sell = order(2, "SELL", price=9_000, minute=1).model_copy(
        update={"instrument": Instrument(symbol="TEST", exchange=Exchange.BSE)})
    result = facts([order(1), sell])
    assert result.realised_pnl == 0 and not result.closed_trades


def test_unpriced_market_order_not_fabricated():
    o = order(1, filled_quantity=0, avg_fill_price=None).model_copy(update={"limit_price": None, "order_type": "MARKET"})
    result = facts([o])
    assert result.largest_order_pct == 0
    assert any("no price available" in n for n in result.notes)


def test_rejected_yesterday_future_ignored_and_latest_snapshot_deduplicated():
    rejected = order(1).model_copy(update={"status": OrderStatus.REJECTED})
    yesterday = order(2, created_at=NOW - timedelta(days=1), updated_at=NOW - timedelta(days=1))
    future = order(3, created_at=NOW + timedelta(minutes=1), updated_at=NOW + timedelta(minutes=1))
    old = order(4, filled_quantity=2)
    newer = old.model_copy(update={"filled_quantity": 5, "updated_at": NOW})
    result = facts([rejected, yesterday, future, newer, old])
    assert result.orders_today == 1 and result.turnover == 50_000


def test_indian_midnight_and_timezone_validation():
    now = datetime(2026, 10, 9, 19, tzinfo=timezone.utc)
    before = now.replace(hour=18, minute=29)
    after = now.replace(hour=18, minute=30)
    result = facts([order(1, created_at=before, updated_at=before),
                    order(2, created_at=after, updated_at=after)], now=now)
    assert str(result.day) == "2026-10-10" and result.orders_today == 1
    with pytest.raises(ValueError, match="timezone-aware"):
        facts(now=now.replace(tzinfo=None))


@pytest.mark.asyncio
async def test_reads_all_four_broker_sources_and_propagates_failure():
    broker = AsyncMock()
    broker.get_orders.return_value = [order(1)]
    broker.get_holdings.return_value = []
    broker.get_positions.return_value = []
    broker.get_funds.return_value = Funds(available_cash=1_000_000)
    result = await compute_today(broker, PROFILE, NOW)
    assert result.turnover == 100_000
    for method in (broker.get_orders, broker.get_holdings, broker.get_positions, broker.get_funds):
        method.assert_awaited_once()
    broker.get_funds.side_effect = BrokerTimeout("offline")
    with pytest.raises(BrokerTimeout):
        await compute_today(broker, PROFILE, NOW)
