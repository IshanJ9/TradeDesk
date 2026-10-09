"""Level 4: requests about the WHOLE portfolio, turned into one plan the trader approves once.

    "exit all my losing intraday positions"
    "rebalance so no stock exceeds 20%"
    "tell me when any of my holdings falls 3% in a day"   (alerts only: nothing to approve)

The model only picks the tool (and, for rebalancing, passes the percentage the trader typed). Code finds the
positions, works out every quantity from live account data, and drafts the steps. One step becomes a normal
order card; several become a plan card. Nothing is sent until the trader approves, and every step goes
through the same checks as a single order.

These tools never choose what to BUY: they only exit or trim what the trader already has. That keeps them
on the side of carrying out the trader's instruction, not advising.
"""

from dataclasses import dataclass

from pydantic import ValidationError

from app.api_models import CreateRuleRequest, NoticeCard, PlanLegRequest, ProposePlanRequest
from app.llm.tools import ToolContext, _misread
from app.orders.owned import delivery_owned
from app.schemas import (
    Comparator,
    Instrument,
    LegFailurePolicy,
    OrderAction,
    OrderIntent,
    OrderType,
    Product,
    RuleBasis,
    RuleKind,
    Side,
    fmt_rupees as rs,
)

MAX_STEPS = 6  # a plan holds at most 6 steps


@dataclass(frozen=True)
class Exit:
    instrument: Instrument
    side: Side
    quantity: int
    product: Product


def _say(ctx: ToolContext, message: str, level: str = "info") -> dict:
    ctx.reply_cards.append(NoticeCard(level=level, message=message))
    ctx.proposal_texts.append(message)
    return {"status": "nothing_to_do", "message": message}


async def _draft(ctx: ToolContext, exits: list[Exit], title: str, preface: str) -> dict:
    """One exit -> an order card; several -> a plan card where each step stands on its own."""
    if len(exits) == 1:
        e = exits[0]
        proposal = await ctx.cards.propose(
            OrderIntent(
                action=OrderAction.PLACE, instrument_ref=e.instrument.symbol, side=e.side, quantity=e.quantity,
                order_type=OrderType.MARKET, product=e.product,
            )
        )
        status, text, cards = proposal.status, proposal.reply.text, proposal.reply.cards
    else:
        try:
            req = ProposePlanRequest(
                title=title,
                legs=[
                    PlanLegRequest(instrument=e.instrument.symbol, side=e.side, quantity=e.quantity, order_type=OrderType.MARKET, product=e.product)
                    for e in exits
                ],
                on_leg_failure=LegFailurePolicy.CONTINUE,  # each exit is independent: one refusal shouldn't stop the others
            )
        except ValidationError as exc:
            return {"status": "error", "message": exc.errors()[0]["msg"]}
        proposal = await ctx.plans.propose(req)
        status, text, cards = proposal.status, proposal.reply.text, proposal.reply.cards
    ctx.reply_cards.extend(cards)
    ctx.proposal_texts.append(f"{preface}\n{text}" if status in ("card_created", "plan_created") else text)
    if status in ("card_created", "plan_created"):
        return {
            "status": status,
            "steps": len(exits),
            "note": "A card was shown to the trader. NOTHING has been sent; it only goes out if the trader approves it.",
        }
    return {"status": status, "message": ctx.untrusted(proposal.message, "card")["untrusted_text"]}


# --------------------------------------------------------------------------- #
# "exit all my losing intraday positions"
# --------------------------------------------------------------------------- #


async def exit_losing_positions(ctx: ToolContext, args: dict) -> dict:
    product = Product(str(args.get("product") or "MIS").upper())
    word = "intraday" if product is Product.MIS else "delivery"
    losers = sorted(
        (p for p in await ctx.broker.get_positions() if p.product is product and p.quantity != 0 and p.pnl < 0),
        key=lambda p: p.pnl,  # biggest loss first
    )
    if not losers:
        return _say(ctx, f"None of your {word} positions is in a loss right now, so there is nothing to exit. Nothing was prepared.")

    chosen, rest = losers[:MAX_STEPS], losers[MAX_STEPS:]
    lines = [f"{p.instrument.symbol} {'+' if p.quantity > 0 else ''}{p.quantity} at a loss of {rs(-p.pnl)} ({p.pnl_pct:+.2f}%)" for p in chosen]
    preface = f"Your {word} positions in a loss: " + "; ".join(lines) + "."
    if rest:
        preface += f" You have {len(losers)}; this covers the {MAX_STEPS} largest losses. Ask again for the rest afterwards."
    exits = [
        Exit(p.instrument, Side.SELL if p.quantity > 0 else Side.BUY, abs(p.quantity), product)  # a short is closed by buying back
        for p in chosen
    ]
    return await _draft(ctx, exits, f"Exit {len(exits)} losing {word} position{'s' if len(exits) > 1 else ''}", preface)


# --------------------------------------------------------------------------- #
# "rebalance so no stock exceeds 20%"
# --------------------------------------------------------------------------- #


async def trim_to_max_weight(ctx: ToolContext, args: dict) -> dict:
    try:
        cap = float(args["max_percent"])
    except (KeyError, TypeError, ValueError):
        return {"status": "invalid", "errors": ["max_percent is required, e.g. 20 for 20%"]}
    if not 1 <= cap <= 100:
        return {"status": "invalid", "errors": ["max_percent must be between 1 and 100"]}
    wrong = _misread(ctx, percent=cap)
    if wrong:
        return wrong

    by_stock: dict[str, tuple[Instrument, int, int]] = {}  # key -> (instrument, shares, price)
    for row in await delivery_owned(ctx.broker):  # holdings + today's delivery buys
        inst, qty, price = by_stock.get(row.instrument.key, (row.instrument, 0, row.ltp))
        by_stock[row.instrument.key] = (inst, qty + row.quantity, row.ltp)
    cash = (await ctx.broker.get_funds()).available_cash
    stocks_value = sum(q * p for _, q, p in by_stock.values())
    total = stocks_value + cash
    if not by_stock or total <= 0:
        return _say(ctx, "You don't hold any shares, so there is nothing to rebalance. Nothing was prepared.")

    # Selling turns shares into cash, so the total (shares + cash) stays the same and each sale is exact.
    limit = total * cap / 100
    over = []
    for inst, qty, price in by_stock.values():
        value = qty * price
        if value > limit and price > 0:
            sell = min(qty, -int(-(value - limit) // price))  # whole shares, rounded up so the stock ends at or below the cap
            over.append((value - limit, inst, qty, price, sell))
    basis = f"Your portfolio is {rs(total)} (shares {rs(stocks_value)} + cash {rs(cash)}), at current prices."
    if not over:
        biggest = max(by_stock.values(), key=lambda t: t[1] * t[2])
        share = biggest[1] * biggest[2] * 100 / total
        return _say(ctx, f"{basis} No stock is above {cap:g}% (the largest is {biggest[0].symbol} at {share:.1f}%). Nothing was prepared.")

    over.sort(key=lambda t: t[0], reverse=True)  # the furthest over the cap first
    chosen = over[:MAX_STEPS]
    lines = [f"{inst.symbol} is {qty * price * 100 / total:.1f}%, sell {sell} of {qty}" for _, inst, qty, price, sell in chosen]
    preface = f"{basis} Above {cap:g}%: " + "; ".join(lines) + f". After these sales each would be at or below {cap:g}% at today's prices."
    if len(over) > MAX_STEPS:
        preface += f" {len(over) - MAX_STEPS} more stock(s) are over the limit; ask again afterwards."
    exits = [Exit(inst, Side.SELL, sell, Product.CNC) for _, inst, _, _, sell in chosen]
    return await _draft(ctx, exits, f"Trim to at most {cap:g}% per stock", preface)


# --------------------------------------------------------------------------- #
# "tell me when any of my holdings falls 3% in a day"
# --------------------------------------------------------------------------- #


async def alert_on_holdings(ctx: ToolContext, args: dict) -> dict:
    """One ALERT per stock held, measured from yesterday's close. Alerts only tell the trader; nothing is sent."""
    try:
        pct = float(args["percent"])
    except (KeyError, TypeError, ValueError):
        return {"status": "invalid", "errors": ["percent is required, e.g. 3 for 3%"]}
    if not 0 < pct <= 100:
        return {"status": "invalid", "errors": ["percent must be between 0 and 100"]}
    wrong = _misread(ctx, percent=pct)
    if wrong:
        return wrong
    falls = str(args.get("direction") or "DOWN").upper() != "UP"

    symbols = sorted({row.instrument.symbol for row in await delivery_owned(ctx.broker)})
    if not symbols:
        return _say(ctx, "You don't hold any shares, so there is nothing to watch. No alert was set.")
    set_, skipped = [], []
    for symbol in symbols:
        outcome = await ctx.rules.create(
            CreateRuleRequest(
                kind=RuleKind.ALERT, instrument=symbol, comparator=Comparator.BELOW if falls else Comparator.ABOVE,
                percent=pct, basis=RuleBasis.PREV_CLOSE,
            )
        )
        if outcome.status == "rule_created":
            set_.append(f"{symbol} at {rs(outcome.rule.condition.trigger_price)}")
        else:
            skipped.append(f"{symbol} ({outcome.message})")
    move = "falls" if falls else "rises"
    parts = []
    if set_:
        parts.append(
            f"Set {len(set_)} alert{'s' if len(set_) > 1 else ''}, one per stock you hold, for when it {move} {pct:g}% from "
            f"yesterday's close: " + "; ".join(set_) + ". They only notify you; nothing is ever sent. They are measured "
            "from yesterday's close, so they cover today's session: ask again tomorrow to set fresh ones."
        )
    if skipped:
        parts.append("Not set: " + "; ".join(skipped))
    message = " ".join(parts)
    ctx.reply_cards.append(NoticeCard(level="info" if set_ else "warning", message=message))
    ctx.proposal_texts.append(message)
    return {"status": "alerts_set" if set_ else "nothing_set", "count": len(set_), "message": message}
