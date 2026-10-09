"""Level 4: whole-portfolio requests become one approval card, sized entirely by code."""

from datetime import datetime, timezone

import pytest

from app.broker.mock import MockBroker
from app.config import Settings
from app.llm.copilot import Copilot
from app.llm.tools import build_tools
from app.llm.types import LLMTurn, ToolCall
from app.main import create_app
from app.plans.service import PlanAssistant
from app.schemas import Product, Side, paise

T0 = datetime(2026, 10, 8, 5, 0, tzinfo=timezone.utc)


class Env:
    def __init__(self):
        self.broker = MockBroker(clock=lambda: T0)
        self.app = create_app(Settings(ticker_interval=None, reconcile_interval=None), broker=self.broker, clock=lambda: T0)
        self.state = self.app.state

    def position(self, symbol, qty, avg_rupees, product=Product.MIS):
        key = f"NSE:{symbol}"
        self.broker._positions[key] = (qty, paise(avg_rupees))
        self.broker._position_product[key] = product


@pytest.fixture
def env():
    return Env()


def steps(reply):
    """(side, quantity, symbol, product) for every order the reply's card would place."""
    card = reply.cards[-1]
    if card.type == "pending_order":
        p = card.pending
        return [(p.side, p.quantity, p.instrument.symbol, p.product)]
    assert card.type == "plan"
    return [(leg.order.side, leg.order.quantity, leg.order.instrument.symbol, leg.order.product) for leg in card.plan.legs]


# ---- exit losing positions ------------------------------------------------------------------ #


async def test_exits_every_losing_intraday_position_in_one_plan(env):
    env.position("INFY", 10, "1500")  # long, ltp 1448: losing
    env.position("ZOMATO", -20, "230")  # short, ltp 236.5: losing
    env.position("TCS", 3, "3900")  # ltp 3912: winning, stays
    env.position("ITC", 4, "450", Product.CNC)  # losing but delivery: not intraday
    reply = await env.state.copilot.handle("exit all my losing intraday positions")
    assert steps(reply) == [
        (Side.SELL, 10, "INFY", Product.MIS),  # biggest loss first: 520 vs 130
        (Side.BUY, 20, "ZOMATO", Product.MIS),  # a short is closed by buying back
    ]
    assert reply.cards[-1].plan.on_leg_failure.value == "CONTINUE"
    assert "INFY" in reply.text and "ZOMATO" in reply.text and "TCS" not in reply.text
    assert env.broker._orders == {}  # a card only


async def test_one_loser_is_a_normal_order_card(env):
    env.position("INFY", 10, "1500")
    reply = await env.state.copilot.handle("square off my losing positions")
    assert steps(reply) == [(Side.SELL, 10, "INFY", Product.MIS)]


async def test_a_stop_loss_order_is_not_read_as_exit_losers(env):
    env.position("INFY", 10, "1500")
    reply = await env.state.copilot.handle("sell 5 infosys with a stop loss at 1400")
    assert reply.cards[-1].type == "pending_order" and reply.cards[-1].pending.order_type.value == "STOP_LIMIT"


async def test_no_losers_prepares_nothing(env):
    reply = await env.state.copilot.handle("exit all my losing intraday positions")  # only RELIANCE, in profit
    assert all(c.type == "notice" for c in reply.cards)
    assert "Nothing was prepared" in reply.text
    assert env.state.pending.all() == []


async def test_more_than_six_losers_takes_the_six_largest_and_says_so(env):
    for symbol, avg in [("INFY", "1460"), ("TCS", "4000"), ("HDFCBANK", "1700"), ("ITC", "420"),
                        ("TATAMOTORS", "950"), ("TATASTEEL", "170"), ("ZOMATO", "240")]:
        env.position(symbol, 10, avg)
    reply = await env.state.copilot.handle("exit all my losing intraday positions")
    names = [s[2] for s in steps(reply)]
    assert len(names) == 6 and "ZOMATO" not in names  # ZOMATO's loss (35) is the smallest
    assert "You have 7" in reply.text


# ---- trim to a maximum weight ---------------------------------------------------------------- #


async def test_trim_sells_just_enough_of_each_stock_over_the_cap(env):
    # portfolio = shares 1,35,535 + cash 2,50,000 = 3,85,535; 5% = 19,276.75
    reply = await env.state.copilot.handle("rebalance so no stock exceeds 5%")
    assert steps(reply) == [
        (Side.SELL, 54, "ITC", Product.CNC),  # 41,500 over by 22,223.25 at 415 -> 53.6 -> 54
        (Side.SELL, 7, "INFY", Product.CNC),
        (Side.SELL, 4, "HDFCBANK", Product.CNC),
        (Side.SELL, 1, "TCS", Product.CNC),
    ]
    assert "₹3,85,535" in reply.text
    assert all(side is Side.SELL for side, *_ in steps(reply))  # it never picks anything to buy


async def test_trim_with_one_stock_over_is_a_single_card(env):
    reply = await env.state.copilot.handle("trim so no stock is above 10%")
    assert steps(reply) == [(Side.SELL, 8, "ITC", Product.CNC)]


async def test_trim_when_nothing_is_over_prepares_nothing(env):
    reply = await env.state.copilot.handle("rebalance so no stock exceeds 20%")
    assert "No stock is above 20%" in reply.text and "ITC" in reply.text
    assert env.state.pending.all() == []


async def test_a_misread_percentage_is_refused(env):
    s = env.state
    llm_turns = [LLMTurn(tool_calls=[ToolCall("c1", "trim_to_max_weight", {"max_percent": 25})]), LLMTurn(text="")]

    class Scripted:
        async def complete(self, *, system, messages, tools):
            return llm_turns.pop(0)

    copilot = Copilot(Scripted(), build_tools(), env.broker.read_only(), s.cards, s.rules, PlanAssistant(s.plans), s.audit, lambda: T0)
    reply = await copilot.handle("rebalance so no stock exceeds 5%")
    assert "isn't in what you wrote" in reply.text
    assert s.pending.all() == []


# ---- alert on every holding ------------------------------------------------------------------ #


async def test_any_holding_falling_sets_one_alert_per_stock_from_yesterdays_close(env):
    reply = await env.state.copilot.handle("tell me when any of my holdings falls 3% in a day")
    rules = {r.condition.instrument_key: r for r in env.state.rules.list()}
    # ZOMATO closed at 245 and is already at 236.50, below 237.65: that alert would fire at once, so it is not set
    assert set(rules) == {"NSE:INFY", "NSE:ITC", "NSE:TCS", "NSE:HDFCBANK", "NSE:TATAMOTORS"}
    assert rules["NSE:INFY"].condition.trigger_price == paise("1396.80")  # 1440 x 0.97
    assert all(r.kind.value == "ALERT" for r in rules.values())
    assert "Set 5 alerts" in reply.text and "ZOMATO" in reply.text
    assert env.state.pending.all() == [] and env.broker._orders == {}


async def test_rising_alerts_point_up(env):
    await env.state.copilot.handle("notify me if any of my stocks rises 5%")
    assert {r.condition.comparator.value for r in env.state.rules.list()} == {"ABOVE"}
