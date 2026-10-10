"""Read-only daily arithmetic. All money is integer paise.

The order book has cumulative average fills, not individual executions. We process
each order once at updated_at (ties by order id), so FIFO and closing-trade streaks
are estimates. A closing trade is the matched portion of one closing order; its
win/loss is before charges. Delivery and intraday inventories never cross-match.
"""

import asyncio
from collections import defaultdict, deque
from datetime import datetime, timedelta

from app.broker.base import ReadOnlyBroker
from app.history.store import trading_day
from app.orders.charges import compute_charges
from app.risk.models import ClosedTrade, RiskProfile, TodayFacts
from app.schemas import Funds, Holding, Order, OrderStatus, Position, Product, Side


def percentage(value: int, total: int) -> float:
    return 100 * value / total if total > 0 else 0.0


def calculate_today(*, orders: list[Order], holdings: list[Holding],
                    positions: list[Position], funds: Funds,
                    profile: RiskProfile, now: datetime) -> TodayFacts:
    """Pure snapshot calculation; unknown prices are excluded and disclosed.

    An unmatched CNC sell uses a known holding's average cost. Without that basis
    it is skipped, not treated as a new short. MIS supports both long and short
    round trips. Holdings contribute day P&L, positions contribute open P&L.
    Order size uses requested quantity and limit price (average fill as fallback).
    Stock exposure is gross current value, including short positions. Re-entries
    and cooling breaches count submitted orders, even unfilled/cancelled ones;
    both windows exclude their ending instant.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    day = trading_day(now)
    latest: dict[str, Order] = {}
    for order in orders:
        if order.updated_at <= now and (order.order_id not in latest or
                order.updated_at > latest[order.order_id].updated_at):
            latest[order.order_id] = order
    today = sorted((o for o in latest.values() if trading_day(o.created_at) == day
                    and o.status != OrderStatus.REJECTED),
                   key=lambda o: (o.updated_at, o.order_id))
    notes = ["P&L is an estimate from cumulative order fills and broker snapshots; "
             "fill sequencing is approximated by order update time. "
             "Closed-trade wins/losses are before charges."]
    basis = {h.instrument.key: h.avg_price for h in holdings}
    # Each deque contains mutable [signed quantity, cost price] lots.
    lots = defaultdict(deque)
    closed: list[ClosedTrade] = []
    losses: dict[str, datetime] = {}
    streak = reentries = cooling = turnover = charges = intraday = largest = 0
    last_loss = None
    for order in today:
        key = order.instrument.key
        if (order.side == Side.BUY and key in losses and
                timedelta(0) <= order.created_at - losses[key] < timedelta(minutes=profile.reentry_minutes)):
            reentries += 1
        if (streak >= profile.cooling_off_after_losses and last_loss is not None and
                timedelta(0) <= order.created_at - last_loss < timedelta(minutes=profile.cooling_off_minutes)):
            cooling += 1
        reference = order.limit_price or order.avg_fill_price
        if reference is not None:
            largest = max(largest, order.quantity * reference)
        else:
            notes.append(f"Order {order.order_id}: no price available for order-size estimate.")
        qty, price = order.filled_quantity, order.avg_fill_price
        if not qty:
            continue
        if price is None:
            notes.append(f"Order {order.order_id}: missing average fill price; turnover, charges and FIFO excluded.")
            streak = 0  # An unknown fill could have ended the previous loss streak.
            continue
        value = qty * price
        turnover += value
        intraday += value if order.product == Product.MIS else 0
        charges += compute_charges(exchange=order.instrument.exchange, side=order.side,
                                   product=order.product, quantity=qty, price=price,
                                   option=order.instrument.is_option).total
        inventory = lots[(key, order.product)]
        sign = 1 if order.side == Side.BUY else -1
        remaining, matched, realised = qty, 0, 0
        while remaining and inventory and inventory[0][0] * sign < 0:
            lot = inventory[0]
            amount = min(remaining, abs(lot[0]))
            realised += amount * (price - lot[1]) * (1 if lot[0] > 0 else -1)
            matched += amount
            remaining -= amount
            lot[0] += amount * sign
            if not lot[0]:
                inventory.popleft()
        incomplete = False
        if remaining and order.product == Product.CNC and sign < 0:
            if key in basis:
                realised += remaining * (price - basis[key])
                matched += remaining
            else:
                incomplete = True
                notes.append(f"Order {order.order_id}: missing older-holding cost basis for {remaining} shares; realised P&L excluded for those shares.")
            remaining = 0
        if remaining:
            inventory.append([remaining * sign, price])
        if matched:
            closed.append(ClosedTrade(order_id=order.order_id, instrument_key=key,
                                      product=order.product, quantity=matched, pnl=realised,
                                      closed_at=order.updated_at))
        if incomplete:
            # An unknown result must not manufacture a consecutive-loss streak.
            streak = 0
        elif matched:
            streak = streak + 1 if realised < 0 else 0
            if realised < 0:
                last_loss = order.updated_at
                losses[key] = last_loss
    stock_values: dict[str, int] = {}
    for item in [*holdings, *positions]:
        stock_values[item.instrument.key] = stock_values.get(item.instrument.key, 0) + item.current_value
    portfolio = funds.total + sum(stock_values.values())
    if portfolio <= 0:
        notes.append("Portfolio value is not positive; portfolio percentages are unavailable (shown as zero).")
    realised = sum(t.pnl for t in closed)
    unrealised = sum(p.pnl for p in positions) + sum(h.day_pnl for h in holdings)
    pnl = realised + unrealised
    return TodayFacts(day=day, orders_today=len(today), turnover=turnover, charges=charges,
                      recent_orders=sum(now-timedelta(minutes=20) < o.created_at <= now for o in today),
                      realised_pnl=realised, unrealised_pnl=unrealised, pnl_estimate=pnl,
                      pnl_after_charges=pnl - charges, portfolio_value=portfolio,
                      largest_order_pct=percentage(largest, portfolio),
                      largest_stock_pct=percentage(max(stock_values.values(), default=0), portfolio),
                      stock_values=stock_values, intraday_share_pct=percentage(intraday, turnover),
                      consecutive_losses=streak, last_loss_at=last_loss, last_loss_by_stock=losses,
                      reentries=reentries, cooling_off_breaches=cooling, closed_trades=closed, notes=notes)


async def compute_today(broker: ReadOnlyBroker, profile: RiskProfile, now: datetime) -> TodayFacts:
    orders, holdings, positions, funds = await asyncio.gather(
        broker.get_orders(), broker.get_holdings(), broker.get_positions(), broker.get_funds())
    return calculate_today(orders=orders, holdings=holdings, positions=positions,
                           funds=funds, profile=profile, now=now)
