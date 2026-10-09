"""The tools the model can call.

Read tools use a `ReadOnlyBroker` and return numbers already computed and formatted by code
(rupee strings, percentages), so the model copies figures and never calculates them.

`propose_order` only creates a card for the trader to approve. There is deliberately no tool
that approves, sends, or executes anything; `ToolContext` holds no reference to the executor or
the approval service, so a model that is fooled by hostile text still cannot place an order.
"""

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from pydantic import Field, ValidationError

from app.api_models import Card, CreateRuleRequest, NoticeCard, ProposePlanRequest
from app.broker.base import ReadOnlyBroker
from app.llm.grounding import numbers_not_typed
from app.llm.injection import Finding, scan
from app.llm.types import ToolSpec
from app.orders.cards import CardService
from app.plans.readback import leg_label
from app.plans.service import PlanAssistant
from app.rules.service import RuleNotActive, RuleNotFound, RuleService
from app.schemas import (
    Model,
    OrderAction,
    OrderIntent,
    OrderStatus,
    OrderType,
    Product,
    RuleStatus,
    Side,
    Validity,
    fmt_rupees as rs,
    paise,
)

WITHHELD = "[withheld: text flagged as instructions, not data]"


@dataclass
class ToolContext:
    broker: ReadOnlyBroker
    cards: CardService
    rules: RuleService
    plans: PlanAssistant  # can draft a plan card and read reports; it cannot approve or run anything
    clock: Callable[[], datetime]
    user_texts: list[str] = field(default_factory=list)  # what the trader actually wrote (this turn and recent ones)
    findings: list[Finding] = field(default_factory=list)
    reply_cards: list[Card] = field(default_factory=list)
    proposal_texts: list[str] = field(default_factory=list)
    read_results: list[tuple[str, dict]] = field(default_factory=list)  # for the fallback answer

    def untrusted(self, text: str | None, source: str) -> dict:
        """Wrap text that came from outside the app (names, broker messages) before the model sees it.

        Text that reads like an instruction is withheld from the model and recorded as a finding.
        """
        text = text or ""
        hits = scan(text)
        if hits:
            if not any(f.source == source and f.text == text for f in self.findings):
                self.findings.append(Finding(source, text, hits))
            return {"untrusted_text": WITHHELD, "flagged": True}
        return {"untrusted_text": text}


@dataclass(frozen=True)
class Tool:
    spec: ToolSpec
    run: Callable[[ToolContext, dict], Awaitable[dict]]
    render: Callable[[dict], str]
    read_only: bool = True


_RUPEE_WORDS = re.compile(r"₹|\brs\.?(?!\w)|\brupees?\b|\brupay\w*|\binr\b|\bworth\b|\bamount\b|\bk\b|\blakh|\blac\b|\bcrore|\bcr\b|\bka\b|\d\s?k\b", re.IGNORECASE)


def _amount_without_rupees(ctx: ToolContext, amount_rupees: float | int | None) -> dict | None:
    """A bare number before a stock name ("sell 100000 infosys") means SHARES. A model that reads it as rupees
    must be stopped unless the trader said something about money; the trader is asked, in code-written words."""
    if amount_rupees is None or any(_RUPEE_WORDS.search(t) for t in ctx.user_texts):
        return None
    message = (
        f"I read {amount_rupees:g} as an amount in rupees, but you didn't mention rupees. "
        "Nothing was prepared. Did you mean that many shares? Please tell me again, for example "
        "\"sell 10 shares\" or \"buy ₹10,000 worth\"."
    )
    ctx.reply_cards.append(NoticeCard(level="warning", message=message))
    ctx.proposal_texts.append(message)
    return {"status": "blocked", "message": message}


def _misread(ctx: ToolContext, **named: float | int | None) -> dict | None:
    """A model can corrupt a number ("sell 100000" became "69"). Every figure it puts into an order, rule or plan
    must be one the trader wrote. If not, nothing is prepared, and the trader is asked again in code-written words."""
    bad = numbers_not_typed(named, ctx.user_texts)
    if not bad:
        return None
    label = {"quantity": "the quantity", "amount_rupees": "the amount", "limit_price_rupees": "the price",
             "trigger_price_rupees": "the trigger price", "price_rupees": "the price", "percent": "the percentage"}
    parts = ", ".join(f"{label.get(k, k)} as {v.normalize():f}" for k, v in bad)
    message = f"I read {parts}, but that number isn't in what you wrote. Nothing was prepared. Please tell me again."
    ctx.reply_cards.append(NoticeCard(level="warning", message=message))
    ctx.proposal_texts.append(message)
    return {"status": "blocked", "message": message}


def _error(message: str) -> dict:
    return {"status": "error", "message": message}


def _schema(**props: dict) -> dict:
    required = [k for k, v in props.items() if v.pop("_required", False)]
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


# --------------------------------------------------------------------------- #
# account reads
# --------------------------------------------------------------------------- #


async def _funds(ctx: ToolContext, args: dict) -> dict:
    f = await ctx.broker.get_funds()
    return {"available_cash": rs(f.available_cash), "used_margin": rs(f.used_margin), "total": rs(f.total)}


def _render_funds(o: dict) -> str:
    return f"Available cash: {o['available_cash']}. Used margin: {o['used_margin']}."


async def _buffett(ctx: ToolContext) -> bool:
    return (await ctx.broker.get_account_locks()).buffett_mode


def _row(ctx: ToolContext, v, buffett: bool) -> dict:
    inst = v.instrument
    row = {
        "symbol": inst.symbol,
        "name": ctx.untrusted(inst.name, f"instrument {inst.symbol}"),
        "quantity": v.quantity,
        "avg_buy_price": rs(v.avg_price),
        "ltp": rs(v.ltp),
        "invested": rs(v.invested),
        "current_value": rs(v.current_value),
        "pnl": rs(v.pnl),
        "pnl_pct": v.pnl_pct,
    }
    if not buffett:  # Buffett Mode hides day-to-day movement
        row["day_pnl"] = rs(v.day_pnl)
    return row


def _select(items: list, down_more_than_pct: float | None, symbol: str | None) -> list:
    out = items
    if symbol:
        q = symbol.strip().lower()
        out = [v for v in out if q in v.instrument.symbol.lower() or q in v.instrument.name.lower()]
    if down_more_than_pct is not None:
        out = [v for v in out if v.pnl_pct < -abs(down_more_than_pct)]
    return out


def _valued_tool(
    kind: str,
    fetch: Callable[[ReadOnlyBroker], Awaitable[list]],
    other_kind: str,
    other_fetch: Callable[[ReadOnlyBroker], Awaitable[list]],
) -> Tool:
    async def run(ctx: ToolContext, args: dict) -> dict:
        threshold, symbol = args.get("down_more_than_pct"), args.get("symbol")
        buffett = await _buffett(ctx)
        items = await fetch(ctx.broker)
        chosen = _select(items, threshold, symbol)
        out = {
            "count": len(chosen),
            "total_held": len(items),
            "filter": {"down_more_than_pct": threshold, "symbol": symbol},
            kind: [_row(ctx, v, buffett) for v in chosen],
            "buffett_mode": buffett,
        }
        if threshold is not None or symbol is not None:
            # "positions" in everyday speech means everything the trader is invested in, and a model may ask only
            # one of the two lists. A share bought today sits under positions until the next day, so a "down more
            # than X%" or a single-stock question always answers for BOTH, in code.
            others = _select(await other_fetch(ctx.broker), threshold, symbol)
            out[f"also_down_in_{other_kind}"] = [_row(ctx, v, buffett) for v in others]
            out["note"] = (
                "Holdings are shares the trader owns; positions are what they traded today. "
                "Both lists are included here, so mention both."
            )
        return out

    def render(o: dict) -> str:
        rows, f = o[kind], o["filter"]
        label = kind
        if f["down_more_than_pct"] is not None:
            head = f"{o['count']} of your {label} {'is' if o['count'] == 1 else 'are'} down more than {f['down_more_than_pct']:g}%"
        elif f["symbol"]:
            head = f"Matching {label} for “{f['symbol']}”: {o['count']}"
        else:
            head = f"You have {o['count']} {label}"
        def describe(r: dict) -> str:
            return f"{r['symbol']}: {r['quantity']} @ avg {r['avg_buy_price']}, now {r['ltp']}, {r['pnl_pct']:+.2f}% ({r['pnl']})"

        text = head + (":\n" + "\n".join(describe(r) for r in rows) if rows else ".")
        extra = o.get(f"also_down_in_{other_kind}") or []
        if extra:
            pct = f["down_more_than_pct"]
            heading = f"Also down more than {pct:g}% in your {other_kind}" if pct is not None else f"Also in your {other_kind}"
            text += f"\n{heading}:\n" + "\n".join(describe(r) for r in extra)
        return text

    verb = "positions" if kind == "positions" else "holdings"
    return Tool(
        ToolSpec(
            f"get_{verb}",
            f"List the trader's {verb} with quantity, average buy price, current price, P&L and P&L % "
            "(measured against the average buy price). Use down_more_than_pct for 'positions down more than "
            "5%' and symbol to look up one stock (e.g. its average buy price). Never calculate these yourself.",
            _schema(
                down_more_than_pct={"type": "number", "minimum": 0, "description": "Only rows down by more than this percent"},
                symbol={"type": "string", "description": "Filter by symbol or company name"},
            ),
        ),
        run,
        render,
    )


async def _pnl_summary(ctx: ToolContext, args: dict) -> dict:
    holdings, positions = await ctx.broker.get_holdings(), await ctx.broker.get_positions()
    items = [*holdings, *positions]
    buffett = await _buffett(ctx)
    invested = sum(v.invested for v in items)
    current = sum(v.current_value for v in items)
    pnl = sum(v.pnl for v in items)
    out: dict[str, Any] = {
        "buffett_mode": buffett,
        "invested": rs(invested),
        "current_value": rs(current),
        "overall_pnl": rs(pnl),
        "overall_return_pct": round(pnl * 100 / invested, 2) if invested else 0.0,
        "holdings": len(holdings),
        "positions": len(positions),
    }
    if buffett:
        out["note"] = "Buffett Mode is on: daily P&L is hidden."
    else:
        out["day_pnl"] = rs(sum(v.day_pnl for v in items))
    return out


def _render_pnl(o: dict) -> str:
    if o["buffett_mode"]:
        return (
            f"Buffett Mode is on, so daily P&L is hidden. You've invested {o['invested']}, now worth "
            f"{o['current_value']}: an overall return of {o['overall_return_pct']:+.2f}%."
        )
    return (
        f"Today's P&L: {o['day_pnl']}. Overall: {o['overall_pnl']} ({o['overall_return_pct']:+.2f}%) on "
        f"{o['invested']} invested, now worth {o['current_value']}."
    )


async def _orders(ctx: ToolContext, args: dict) -> dict:
    status = args.get("status")
    orders = await ctx.broker.get_orders()
    if status:
        wanted = {status.upper()}
        if wanted & {"OPEN", "PENDING", "PARTIAL"}:
            # "open orders" in everyday speech means every order still live, including a part-filled one
            wanted = {"OPEN", "PENDING", "PARTIAL"}
        orders = [o for o in orders if o.status.value in wanted]
    rows = [
        {
            "order_id": o.order_id,
            "symbol": o.instrument.symbol,
            "side": o.side.value,
            "quantity": o.quantity,
            "filled_quantity": o.filled_quantity,
            "status": o.status.value,
            "limit_price": rs(o.limit_price) if o.limit_price else None,
            "avg_fill_price": rs(o.avg_fill_price) if o.avg_fill_price else None,
            "rejection_reason": o.rejection_reason.value if o.rejection_reason else None,
            "rejection_message": ctx.untrusted(o.rejection_message, f"order {o.order_id}") if o.rejection_message else None,
        }
        for o in orders[:20]
    ]
    return {"count": len(rows), "orders": rows}


def _render_orders(o: dict) -> str:
    if not o["orders"]:
        return "You have no orders today."
    lines = []
    for r in o["orders"]:
        price = f" @ {r['limit_price']}" if r["limit_price"] else ""
        extra = f" ({r['rejection_reason']})" if r["rejection_reason"] else ""
        lines.append(
            f"{r['order_id']}: {r['side']} {r['quantity']} {r['symbol']}{price} - {r['status']}"
            f", filled {r['filled_quantity']}{extra}"
        )
    return f"{o['count']} order(s):\n" + "\n".join(lines)


# --------------------------------------------------------------------------- #
# market reads
# --------------------------------------------------------------------------- #


def _candidates(ctx: ToolContext, candidates: list) -> list[dict]:
    return [{"symbol": c.symbol, "name": ctx.untrusted(c.name, f"instrument {c.symbol}")} for c in candidates]


async def _quote(ctx: ToolContext, args: dict) -> dict:
    res = await ctx.cards.resolve(args.get("symbol", ""))
    if res.status == "ambiguous":
        return {"status": "ambiguous", "query": res.query, "candidates": _candidates(ctx, res.candidates)}
    if res.status == "not_found":
        return {"status": "not_found", "query": res.query}
    q = await ctx.broker.get_quote(res.instrument.key)
    return {
        "status": "ok",
        "symbol": res.instrument.symbol,
        "name": ctx.untrusted(res.instrument.name, f"instrument {res.instrument.symbol}"),
        "ltp": rs(q.ltp),
        "prev_close": rs(q.prev_close),
        "change_pct": round((q.ltp - q.prev_close) * 100 / q.prev_close, 2),
        "day_high": rs(q.day_high) if q.day_high else None,
        "day_low": rs(q.day_low) if q.day_low else None,
    }


def _render_quote(o: dict) -> str:
    if o["status"] == "ambiguous":
        return "Which one do you mean? " + ", ".join(c["symbol"] for c in o["candidates"])
    if o["status"] == "not_found":
        return f"I couldn't find “{o['query']}”."
    return (
        f"{o['symbol']} is at {o['ltp']} ({o['change_pct']:+.2f}% vs previous close {o['prev_close']}); "
        f"today's range {o['day_low']} to {o['day_high']}."
    )


async def _find(ctx: ToolContext, args: dict) -> dict:
    res = await ctx.cards.resolve(args.get("query", ""))
    if res.status == "resolved":
        i = res.instrument
        return {"status": "resolved", "symbol": i.symbol, "exchange": i.exchange.value,
                "name": ctx.untrusted(i.name, f"instrument {i.symbol}")}
    if res.status == "ambiguous":
        return {"status": "ambiguous", "query": res.query, "candidates": _candidates(ctx, res.candidates)}
    return {"status": "not_found", "query": res.query}


def _render_find(o: dict) -> str:
    if o["status"] == "resolved":
        return f"That is {o['symbol']} on {o['exchange']}."
    return _render_quote(o)


async def _expiries(ctx: ToolContext, args: dict) -> dict:
    underlying = (args.get("underlying") or "NIFTY").upper()
    dates = await ctx.broker.get_option_expiries(underlying)
    today = ctx.clock().date()
    return {"underlying": underlying, "today": today.isoformat(), "expiries": [d.isoformat() for d in dates if d >= today]}


def _render_expiries(o: dict) -> str:
    if not o["expiries"]:
        return f"No upcoming expiries found for {o['underlying']}."
    return f"Upcoming {o['underlying']} expiries: " + ", ".join(o["expiries"]) + "."


async def _chain(ctx: ToolContext, args: dict) -> dict:
    underlying = (args.get("underlying") or "NIFTY").upper()
    today = ctx.clock().date()
    valid = [d for d in await ctx.broker.get_option_expiries(underlying) if d >= today]
    if not valid:
        return _error(f"No upcoming expiries for {underlying}.")
    if args.get("expiry"):
        try:
            expiry = date.fromisoformat(str(args["expiry"]))
        except ValueError:
            return _error("expiry must be a date like 2026-10-13. " f"Valid expiries: {[d.isoformat() for d in valid]}")
        if expiry not in valid:
            return _error(f"{expiry.isoformat()} is not an expiry. Valid expiries: {[d.isoformat() for d in valid]}")
    else:
        expiry = valid[0]  # nearest upcoming
    window = int(args.get("strikes_around") or 3)
    chain = await ctx.broker.get_option_chain(underlying, expiry, window=max(1, min(window, 10)))
    atm = min(chain.rows, key=lambda r: abs(r.strike - chain.spot)).strike
    return {
        "status": "ok",
        "underlying": underlying,
        "expiry": expiry.isoformat(),
        "spot": rs(chain.spot),
        "at_the_money_strike": rs(atm),
        "rows": [
            {
                "strike": rs(r.strike),
                "call_ltp": rs(r.call.ltp) if r.call else None,
                "call_oi": r.call.oi if r.call else None,
                "put_ltp": rs(r.put.ltp) if r.put else None,
                "put_oi": r.put.oi if r.put else None,
            }
            for r in chain.rows
        ],
    }


def _render_chain(o: dict) -> str:
    if o.get("status") == "error":
        return o["message"]
    def leg(kind: str, r: dict) -> str:
        oi = r[f"{kind}_oi"]
        return f"{r[f'{kind}_ltp']}" + (f" (OI {oi:,})" if oi is not None else "")  # OI only when the broker gave one

    lines = [f"{r['strike']}: call {leg('call', r)} | put {leg('put', r)}" for r in o["rows"]]
    return (
        f"{o['underlying']} is at {o['spot']}. Strikes around the money for the {o['expiry']} expiry "
        f"(at-the-money {o['at_the_money_strike']}):\n" + "\n".join(lines)
    )


# --------------------------------------------------------------------------- #
# propose_order: creates a card, nothing more
# --------------------------------------------------------------------------- #


class ProposeOrderInput(Model):
    action: OrderAction = Field(description="PLACE a new order, MODIFY an open one, or CANCEL an open one")
    instrument: str | None = Field(default=None, max_length=60, description="ONLY the stock's name, as the trader said it, e.g. 'ITC' or 'HDFC Bank'. Never put the price, the quantity or words like 'at market' in here; those have their own fields")
    side: Side | None = None
    quantity: int | None = Field(default=None, gt=0, description="Number of shares. Use this OR amount_rupees OR fraction_of_holding")
    amount_rupees: float | None = Field(default=None, gt=0, description="Rupee amount to spend, e.g. 'buy Infosys worth 10k' -> 10000")
    fraction_of_holding: float | None = Field(
        default=None, gt=0, le=1,
        description="SELL only, instead of quantity: a fraction of the shares held. 'half my TCS' -> 0.5, 'a third' -> 0.333, "
        "'30%' -> 0.3, 'all of it' -> 1. Do NOT work out the share count yourself; the code does it.",
    )
    order_type: OrderType | None = Field(
        default=None,
        description="LIMIT if the trader gave a price, STOP_LIMIT for a stop-loss (give trigger_price_rupees), otherwise MARKET",
    )
    limit_price_rupees: float | None = Field(default=None, gt=0)
    trigger_price_rupees: float | None = Field(
        default=None, gt=0, description="Stop-loss trigger. To move an existing stop-loss, use action MODIFY with the new trigger"
    )
    product: Product = Product.CNC
    validity: Validity = Validity.DAY
    target_order_id: str | None = Field(
        default=None,
        description="order_id from get_orders, for MODIFY or CANCEL. For 'move my stop-loss on X' you may give the "
        "instrument and trigger_price_rupees instead; the open stop-loss on X is found for you",
    )

    def to_intent(self) -> OrderIntent:
        return OrderIntent(
            action=self.action,
            instrument_ref=self.instrument,
            side=self.side,
            quantity=self.quantity,
            amount_paise=paise(self.amount_rupees) if self.amount_rupees is not None else None,
            fraction_of_holding=self.fraction_of_holding,
            order_type=self.order_type,
            limit_price=paise(self.limit_price_rupees) if self.limit_price_rupees is not None else None,
            trigger_price=paise(self.trigger_price_rupees) if self.trigger_price_rupees is not None else None,
            product=self.product,
            validity=self.validity,
            target_order_id=self.target_order_id,
        )


async def _open_stop_for(ctx: ToolContext, name: str) -> tuple[str | None, dict | None]:
    """The id of the one open stop-loss on `name`, or (None, a tool result explaining why not)."""
    keys = {i.key for i in await ctx.broker.search_instruments(name, limit=10)}
    stops = [
        o
        for o in await ctx.broker.get_orders()
        if o.order_type is OrderType.STOP_LIMIT and o.status in (OrderStatus.OPEN, OrderStatus.PARTIAL) and o.instrument.key in keys
    ]
    if len(stops) == 1:
        return stops[0].order_id, None
    if not stops:
        return None, {"status": "blocked", "message": f"I can't find an open stop-loss order on {name} in your order book."}
    ids = ", ".join(o.order_id for o in stops)
    return None, {"status": "blocked", "message": f"You have several open stop-loss orders on {name} ({ids}). Tell me which one."}


async def _propose(ctx: ToolContext, args: dict) -> dict:
    try:
        parsed = ProposeOrderInput.model_validate(args)
        if (
            parsed.action is OrderAction.MODIFY
            and not parsed.target_order_id
            and parsed.instrument
            and parsed.trigger_price_rupees is not None
        ):
            found, problem = await _open_stop_for(ctx, parsed.instrument)
            if problem is not None:
                return problem
            parsed = parsed.model_copy(update={"target_order_id": found})
        intent = parsed.to_intent()
    except ValidationError as exc:
        errors = [f"{'.'.join(map(str, e['loc'])) or 'input'}: {e['msg']}" for e in exc.errors()]
        return {"status": "invalid", "errors": errors, "note": "Fix the arguments, or ask the trader for what is missing."}
    except ValueError as exc:  # OrderIntent's own checks
        return {"status": "invalid", "errors": [str(exc)], "note": "Fix the arguments, or ask the trader for what is missing."}

    if intent.action in (OrderAction.PLACE, OrderAction.MODIFY):
        wrong = _amount_without_rupees(ctx, parsed.amount_rupees) or _misread(
            ctx, quantity=parsed.quantity, amount_rupees=parsed.amount_rupees,
            limit_price_rupees=parsed.limit_price_rupees, trigger_price_rupees=parsed.trigger_price_rupees,
        )
        if wrong:
            return wrong

    proposal = await ctx.cards.propose(intent)
    ctx.reply_cards.extend(proposal.reply.cards)
    ctx.proposal_texts.append(proposal.reply.text)  # the reply shown to the trader is written by code
    if proposal.status == "card_created":
        return {
            "status": "card_created",
            "summary": ctx.untrusted(proposal.message, f"order card {proposal.pending.id}"),
            "warnings": [ctx.untrusted(w, "order warning") for w in proposal.pending.warnings],
            "note": "A card was shown to the trader. NOTHING has been sent; it only goes out if the trader approves the card.",
        }
    if proposal.status == "needs_clarification":
        return {"status": "needs_clarification", "candidates": _candidates(ctx, proposal.candidates)}
    return {"status": proposal.status, "message": proposal.message}


def _render_propose(o: dict) -> str:
    return o.get("message") or o.get("note", "")



# --------------------------------------------------------------------------- #
# standing rules: a rule can only ever prepare an alert or an approval card
# --------------------------------------------------------------------------- #


async def _create_rule(ctx: ToolContext, args: dict) -> dict:
    try:
        req = CreateRuleRequest.model_validate(args)
    except ValidationError as exc:
        errors = [f"{'.'.join(map(str, e['loc'])) or 'input'}: {e['msg']}" for e in exc.errors()]
        return {"status": "invalid", "errors": errors, "note": "Fix the arguments, or ask the trader for what is missing."}

    wrong = _amount_without_rupees(ctx, req.amount_rupees) or _misread(
        ctx, quantity=req.quantity, amount_rupees=req.amount_rupees, price_rupees=req.price_rupees,
        limit_price_rupees=req.limit_price_rupees, percent=req.percent,
    )
    if wrong:
        return wrong

    outcome = await ctx.rules.create(req)
    ctx.reply_cards.extend(outcome.reply.cards)
    ctx.proposal_texts.append(outcome.reply.text)  # the confirmation the trader reads is written by code
    if outcome.status == "rule_created":
        return {
            "status": "rule_created",
            "rule_id": outcome.rule.id,
            "summary": outcome.message,
            "note": "The rule is saved. When it fires it only prepares an alert or an approval card; nothing is sent without the trader's approval.",
        }
    if outcome.status == "needs_clarification":
        return {"status": "needs_clarification", "candidates": _candidates(ctx, outcome.candidates)}
    return {"status": outcome.status, "message": outcome.message}


async def _list_rules(ctx: ToolContext, args: dict) -> dict:
    status = args.get("status")
    rules = ctx.rules.list(RuleStatus(status.upper()) if status else None)[:20]
    return {
        "count": len(rules),
        "rules": [
            {
                "rule_id": r.id,
                "status": r.status.value,
                "kind": r.kind.value,
                "description": r.description,
                "trigger_price": rs(r.condition.trigger_price),
                "fired_at": r.fired_at.isoformat() if r.fired_at else None,
            }
            for r in rules
        ],
    }


def _render_rules(o: dict) -> str:
    if not o["rules"]:
        return "You have no standing rules."
    lines = [f"{r['rule_id']} [{r['status']}]: {r['description']}" for r in o["rules"]]
    return f"{o['count']} rule(s):\n" + "\n".join(lines)


async def _cancel_rule(ctx: ToolContext, args: dict) -> dict:
    rule_id = str(args.get("rule_id", ""))
    try:
        rule = await ctx.rules.cancel(rule_id)
    except RuleNotFound:
        return {"status": "not_found", "message": "I can't find a rule with that id. Use list_rules to see them."}
    except RuleNotActive:
        return {"status": "not_active", "message": "That rule has already fired or been cancelled."}
    ctx.proposal_texts.append(f"Cancelled: {rule.description}")
    return {"status": "cancelled", "rule_id": rule.id}

# --------------------------------------------------------------------------- #
# plans: several orders approved together
# --------------------------------------------------------------------------- #


async def _propose_plan(ctx: ToolContext, args: dict) -> dict:
    try:
        req = ProposePlanRequest.model_validate(args)
    except ValidationError as exc:
        errors = [f"{'.'.join(map(str, e['loc'])) or 'input'}: {e['msg']}" for e in exc.errors()]
        return {"status": "invalid", "errors": errors, "note": "Fix the arguments, or ask the trader for what is missing."}

    for leg in req.legs:
        wrong = _amount_without_rupees(ctx, leg.amount_rupees) or _misread(
            ctx, quantity=leg.quantity, amount_rupees=leg.amount_rupees, limit_price_rupees=leg.limit_price_rupees
        )
        if wrong:
            return wrong

    proposal = await ctx.plans.propose(req)
    ctx.reply_cards.extend(proposal.reply.cards)
    ctx.proposal_texts.append(proposal.reply.text)  # the plan description is written by code
    if proposal.status == "plan_created":
        return {
            "status": "plan_created",
            "plan_id": proposal.plan.id,
            "steps": [leg_label(leg) for leg in proposal.plan.legs],
            "summary": ctx.untrusted(proposal.message, f"plan {proposal.plan.id}"),
            "note": "A plan card was shown to the trader. NOTHING has been sent; the steps only run if the trader approves the whole plan.",
        }
    if proposal.status == "needs_clarification":
        return {"status": "needs_clarification", "candidates": _candidates(ctx, proposal.candidates)}
    return {"status": proposal.status, "message": proposal.message}


def _safe_report_summary(ctx: ToolContext, report) -> str:
    """The summary is written by code, but a step's message can carry the broker's own words, so only
    those messages are scanned; if one reads like instructions it is replaced, not the whole report."""
    summary = report.summary
    for r in report.legs:
        if r.message and ctx.untrusted(r.message, f"plan {report.plan_id} step {r.index + 1}").get("flagged"):
            summary = summary.replace(r.message, WITHHELD)
    return summary


async def _plan_report(ctx: ToolContext, args: dict) -> dict:
    report = await ctx.plans.report(args.get("plan_id"))
    if report is None:
        return {"status": "not_found", "message": "There is no plan yet."}
    return {
        "status": "ok",
        "plan_id": report.plan_id,
        "state": report.state.value,
        "all_filled": report.all_filled,
        "summary": _safe_report_summary(ctx, report),
        "steps": [
            {
                "step": r.index + 1,
                "label": r.label,
                "status": r.status.value,
                "requested_quantity": r.requested_quantity,
                "filled_quantity": r.filled_quantity,
                "pending_quantity": r.pending_quantity,
                "avg_fill_price": rs(r.avg_fill_price) if r.avg_fill_price else None,
            }
            for r in report.legs
        ],
    }


# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #


def build_tools() -> dict[str, Tool]:
    from app.llm.portfolio_tools import alert_on_holdings, exit_losing_positions, trim_to_max_weight  # they build on this module

    tools = [
        Tool(ToolSpec("get_funds", "Available cash and used margin.", _schema()), _funds, _render_funds),
        _valued_tool("holdings", lambda b: b.get_holdings(), "positions", lambda b: b.get_positions()),
        _valued_tool("positions", lambda b: b.get_positions(), "holdings", lambda b: b.get_holdings()),
        Tool(
            ToolSpec(
                "get_pnl_summary",
                "Today's P&L and overall P&L across holdings and positions. In Buffett Mode it returns the "
                "invested amount and long-term return only.",
                _schema(),
            ),
            _pnl_summary,
            _render_pnl,
        ),
        Tool(
            ToolSpec(
                "get_orders",
                "Today's orders with their status. Use this to find an order_id before modifying or cancelling. "
                "Asking for OPEN (or PENDING or PARTIAL) returns every order still live, including part-filled ones.",
                _schema(status={"type": "string", "enum": [s.value for s in OrderStatus], "description": "Only orders in this status"}),
            ),
            _orders,
            _render_orders,
        ),
        Tool(
            ToolSpec(
                "get_quote",
                "Current price, previous close, day change and day range of one stock.",
                _schema(symbol={"type": "string", "description": "Stock name or symbol", "_required": True}),
            ),
            _quote,
            _render_quote,
        ),
        Tool(
            ToolSpec(
                "find_instrument",
                "Match a stock name to exactly one instrument, or list the candidates when it is ambiguous.",
                _schema(query={"type": "string", "_required": True}),
            ),
            _find,
            _render_find,
        ),
        Tool(
            ToolSpec(
                "get_option_expiries",
                "Upcoming option expiry dates for an index. Never assume which weekday an expiry falls on.",
                _schema(underlying={"type": "string", "description": "Defaults to NIFTY"}),
            ),
            _expiries,
            _render_expiries,
        ),
        Tool(
            ToolSpec(
                "get_option_chain",
                "Option chain strikes around the current index level ('near the money'). Omit expiry for the "
                "nearest one; otherwise pass a date from get_option_expiries as YYYY-MM-DD.",
                _schema(
                    underlying={"type": "string", "description": "Defaults to NIFTY"},
                    expiry={"type": "string", "description": "YYYY-MM-DD"},
                    strikes_around={"type": "integer", "minimum": 1, "maximum": 10, "description": "Strikes either side of the money; default 3"},
                ),
            ),
            _chain,
            _render_chain,
        ),
        Tool(
            ToolSpec(
                "propose_order",
                "Prepare an order card for the trader to approve. This does NOT place anything: the trader must "
                "click Approve on the card. Use it for every buy, sell, modify or cancel request. If the stock "
                "name is ambiguous it returns candidates: ask the trader which one; never guess.",
                ProposeOrderInput.model_json_schema(),
            ),
            _propose,
            _render_propose,
            read_only=False,
        ),
        Tool(
            ToolSpec(
                "create_rule",
                "Save a standing instruction: an ALERT ('alert me if HDFC Bank drops 3% from my buy price') or a "
                "TRIGGER_ORDER ('buy 5 TCS if it falls below 3800'). Give the trigger as price_rupees OR percent "
                "(+ basis AVG_BUY for 'from my buy price', PREV_CLOSE, or AT_CREATION). A rule never sends an "
                "order: when it fires it prepares an approval card for the trader. Rules that are already true "
                "right now are refused. If the stock is ambiguous it returns candidates: ask, never guess.",
                CreateRuleRequest.model_json_schema(),
            ),
            _create_rule,
            lambda o: o.get("message") or o.get("note", ""),
            read_only=False,
        ),
        Tool(
            ToolSpec(
                "list_rules",
                "The trader's standing instructions (alerts and conditional orders) with their status.",
                _schema(status={"type": "string", "enum": [s.value for s in RuleStatus]}),
            ),
            _list_rules,
            _render_rules,
        ),
        Tool(
            ToolSpec(
                "cancel_rule",
                "Cancel an active standing instruction by its rule_id (from list_rules).",
                _schema(rule_id={"type": "string", "_required": True}),
            ),
            _cancel_rule,
            lambda o: o.get("message", ""),
            read_only=False,
        ),
        Tool(
            ToolSpec(
                "propose_plan",
                "Prepare a PLAN of several orders for the trader to approve together, e.g. 'sell half my Infosys and "
                "buy ITC with the money'. Each step is sized with exactly one of quantity, amount_rupees, "
                "fraction_of_holding (SELL: 'half' = 0.5) or proceeds_of_leg (BUY: spend the money from an earlier "
                "sale; the first step is 0). Never calculate share counts yourself. Nothing is sent until the trader "
                "approves the whole plan. Use propose_order for a single order.",
                ProposePlanRequest.model_json_schema(),
            ),
            _propose_plan,
            lambda o: o.get("message") or o.get("note", ""),
            read_only=False,
        ),
        Tool(
            ToolSpec(
                "get_plan_report",
                "How an approved plan went, step by step: filled, partly filled, rejected, or not sent. Defaults "
                "to the most recent plan.",
                _schema(plan_id={"type": "string"}),
            ),
            _plan_report,
            lambda o: o.get("summary") or o.get("message", ""),
        ),
        Tool(
            ToolSpec(
                "exit_losing_positions",
                "Prepare ONE approval card that exits every position currently in a loss, e.g. 'exit all my losing "
                "intraday positions' or 'square off my losers'. The code finds the positions and every quantity; pass "
                "no stock names or numbers. product MIS = intraday (the default), CNC = today's delivery positions. "
                "Nothing is sent until the trader approves.",
                _schema(product={"type": "string", "enum": ["MIS", "CNC"], "description": "MIS = intraday (default)"}),
            ),
            exit_losing_positions,
            lambda o: o.get("message") or o.get("note", ""),
            read_only=False,
        ),
        Tool(
            ToolSpec(
                "trim_to_max_weight",
                "Prepare ONE approval card that sells just enough of each holding so that no stock is above a "
                "percentage of the portfolio (shares + cash), e.g. 'rebalance so no stock exceeds 20%'. Pass only the "
                "percentage the trader said. The code works out every quantity. It only sells; it never picks "
                "anything to buy. Nothing is sent until the trader approves.",
                _schema(max_percent={"type": "number", "minimum": 1, "maximum": 100, "_required": True}),
            ),
            trim_to_max_weight,
            lambda o: o.get("message") or o.get("note", ""),
            read_only=False,
        ),
        Tool(
            ToolSpec(
                "alert_on_holdings",
                "Set an ALERT on EVERY stock the trader holds, e.g. 'tell me when any of my holdings falls 3% in a day'. "
                "Measured from yesterday's close. Pass only the percentage the trader said and the direction. Alerts "
                "only notify; they never prepare or send orders. For one named stock use create_rule instead.",
                _schema(
                    percent={"type": "number", "exclusiveMinimum": 0, "maximum": 100, "_required": True},
                    direction={"type": "string", "enum": ["DOWN", "UP"], "description": "DOWN = falls (default), UP = rises"},
                ),
            ),
            alert_on_holdings,
            lambda o: o.get("message", ""),
            read_only=False,
        ),
    ]
    return {t.spec.name: t for t in tools}
