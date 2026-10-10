"""Options, end to end on the mock broker: buy calls/puts in whole lots, sell only what is held (never write an option),
021's Options charges, and the factual risk notice on every buy card."""

import functools
from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.broker.mock import MockBroker
from app.config import Settings
from app.llm.types import LLMTurn, ToolCall
from app.main import create_app
from app.orders.charges import compute_charges
from app.schemas import Exchange, Product, Side, paise

T0 = datetime(2026, 10, 10, 5, 0, tzinfo=timezone.utc)  # NIFTY 24500; mock expiries 13, 20, 27 Oct 2026
NIFTY_CALL = {"underlying": "NIFTY", "strike": paise(24500), "option_type": "CE"}


@pytest.fixture
def broker():
    return MockBroker(clock=lambda: T0)


@pytest.fixture
def client(broker):
    settings = Settings(ticker_interval=None, reconcile_interval=None, timeout_reconcile_delay=0, account_push_interval=3600)
    with TestClient(create_app(settings, broker=broker, clock=lambda: T0)) as c:
        yield c


def option(side="BUY", **over):
    body = dict(action="PLACE", option=dict(NIFTY_CALL), side=side, lots=1, order_type="MARKET")
    body.update(over)
    return body


def preview(client, body):
    r = client.post("/api/orders/preview", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def card(client, body):
    reply = preview(client, body)
    assert reply["cards"][0]["type"] == "pending_order", reply
    return reply["cards"][0]["pending"], reply["text"]


def approve(client, p):
    return client.post(f"/api/approvals/{p['id']}/approve", json={"order_hash": p["order_hash"]})


# ---- buying ---------------------------------------------------------------------------------------- #


def test_one_lot_of_a_call_is_75_units_carried_overnight_with_a_factual_notice(client):
    p, text = card(client, option())
    assert p["instrument"]["symbol"] == "NIFTY26101324500CE"
    assert p["instrument"]["expiry"] == "2026-10-13"  # nearest expiry, chosen by code and stated on the card
    assert (p["quantity"], p["product"], p["side"]) == (75, "NRML", "BUY")
    assert text.startswith("You are buying 1 lot (75 units) of NIFTY 24,500 CE (13 Oct 2026) (NSE)")
    notice = " ".join(p["warnings"])
    assert "1 lot of 75 = 75 units" in notice
    assert "unless NIFTY is above ₹24,500.00 at expiry on 13 Oct 2026" in notice and "is lost" in notice
    assert "SEBI's study of FY22 to FY24 found that 93% of individual traders in equity F&O made a loss." in notice


def test_a_put_says_below_and_a_named_expiry_is_used(client):
    p, _ = card(client, option(option={**NIFTY_CALL, "option_type": "PE", "expiry": "2026-10-20"}))
    assert p["instrument"]["symbol"] == "NIFTY26102024500PE"
    assert "unless NIFTY is below ₹24,500.00 at expiry on 20 Oct 2026, this put is worth nothing" in " ".join(p["warnings"])


def test_intraday_stays_intraday(client):
    p, _ = card(client, option(product="MIS"))
    assert p["product"] == "MIS"


@pytest.mark.parametrize("over, words", [
    (dict(lots=None, quantity=50), "lots of 75; 50 is not a whole number of lots"),
    (dict(option={**NIFTY_CALL, "strike": paise(24510)}), "I can't find a NIFTY ₹24,510.00 CE option"),
    (dict(option={**NIFTY_CALL, "expiry": "2026-10-06"}), "expiring 06 Oct 2026"),  # already expired
    (dict(option={**NIFTY_CALL, "underlying": "BANKNIFTY"}), "I can't find a BANKNIFTY"),
])
def test_a_wrong_size_or_contract_is_refused_with_the_reason(client, over, words):
    reply = preview(client, option(**over))
    assert reply["cards"][0]["level"] == "blocked" and words in reply["text"], reply["text"]


def test_option_charges_follow_021s_options_column():
    # 1 lot at a ₹121.20 premium: value ₹9,090.00
    buy = compute_charges(exchange=Exchange.NSE, side=Side.BUY, product=Product.NRML, quantity=75, price=12120, option=True)
    assert (buy.brokerage, buy.stt, buy.exchange_txn, buy.stamp_duty, buy.ipft, buy.sebi_fee, buy.gst) == (2000, 0, 318, 27, 5, 1, 423)
    sell = compute_charges(exchange=Exchange.NSE, side=Side.SELL, product=Product.NRML, quantity=75, price=12120, option=True)
    assert sell.stt == 1364 and sell.stamp_duty == 0 and sell.dp_charge == 0  # STT 0.15% of ₹9,090 on the sell side only
    equity = compute_charges(exchange=Exchange.NSE, side=Side.BUY, product=Product.CNC, quantity=75, price=12120)
    assert equity.brokerage != buy.brokerage  # equity rates unchanged and separate


# ---- selling: only what is held ---------------------------------------------------------------------- #


def test_selling_an_option_not_held_is_refused_as_writing(client, broker):
    reply = preview(client, option(side="SELL"))
    assert reply["cards"][0]["level"] == "blocked"
    assert "Selling options you don't hold (writing) isn't supported" in reply["text"]
    assert broker._orders == {}


def test_buy_approve_then_sell_what_is_held_but_never_more(client, broker):
    p, _ = card(client, option(lots=2))
    assert approve(client, p).status_code == 200
    [position] = [x for x in client.portal.call(broker.get_positions) if x.instrument.is_option]
    assert (position.instrument.symbol, position.quantity, position.product) == ("NIFTY26101324500CE", 150, Product.NRML)

    too_many = preview(client, option(side="SELL", lots=3))
    assert too_many["cards"][0]["level"] == "blocked" and "You hold 150 of NIFTY 24,500 CE (13 Oct 2026) as NRML" in too_many["text"]
    p_sell, text = card(client, option(side="SELL", lots=1))
    assert text.startswith("You are selling 1 lot (75 units)") and "after charges" in text
    assert not any("expire worthless" in w for w in p_sell["warnings"])  # the buy-side notice is for buying
    half, _ = card(client, option(side="SELL", lots=None, fraction_of_holding=0.5))
    assert half["quantity"] == 75  # half of 150, in whole lots


def test_an_intraday_position_cant_be_sold_as_carry(client, broker):
    p, _ = card(client, option(product="MIS"))
    approve(client, p)
    wrong_product = preview(client, option(side="SELL"))  # NRML: nothing held as NRML
    assert wrong_product["cards"][0]["level"] == "blocked"


# ---- through the assistant's tool ------------------------------------------------------------------- #


class ScriptedLLM:
    def __init__(self, *turns):
        self.turns = list(turns)

    async def complete(self, *, system, messages, tools):
        return self.turns.pop(0) if self.turns else LLMTurn(text="")


def _ask(client, llm, message):
    copilot = client.app.state.copilot
    copilot._llm = llm
    return client.portal.call(functools.partial(copilot.handle, message))


def _propose(**args):
    return ToolCall(id="c1", name="propose_order", input={"action": "PLACE", "side": "BUY", "order_type": "MARKET", **args})


def test_the_tool_drafts_an_option_card_from_the_traders_words(client):
    call = _propose(option_underlying="NIFTY", strike_rupees=24500, option_type="CE", lots=1)
    reply = _ask(client, ScriptedLLM(LLMTurn(tool_calls=[call]), LLMTurn(text="")), "buy 1 lot nifty 24500 ce")
    [c] = [c for c in reply.cards if c.type == "pending_order"]
    assert c.pending.quantity == 75 and c.pending.instrument.is_option


@pytest.mark.parametrize("args, message, words", [
    (dict(strike_rupees=24000, lots=1), "buy 1 lot nifty 24500 ce", "the strike as 24000"),  # a corrupted strike
    (dict(strike_rupees=24500, lots=3), "buy 1 lot nifty 24500 ce", "the number of lots as 3"),
])
def test_a_number_the_trader_didnt_type_stops_the_card(client, args, message, words):
    call = _propose(option_underlying="NIFTY", option_type="CE", **args)
    reply = _ask(client, ScriptedLLM(LLMTurn(tool_calls=[call]), LLMTurn(text="")), message)
    assert not any(c.type == "pending_order" for c in reply.cards)
    assert words in reply.text


def test_the_built_in_reader_understands_option_wording(client):
    reply = client.post("/api/chat", json={"message": "buy 2 lots nifty 24500 pe 2026-10-20"}).json()
    [c] = [c for c in reply["cards"] if c["type"] == "pending_order"]
    assert c["pending"]["instrument"]["symbol"] == "NIFTY26102024500PE" and c["pending"]["quantity"] == 150
    assert not [c for c in client.post("/api/chat", json={"message": "buy nifty 24500 ce"}).json()["cards"]
                if c["type"] == "pending_order"]  # no lots given: nothing is guessed


def test_an_option_sized_in_rupees_is_invalid(client):
    call = _propose(option_underlying="NIFTY", strike_rupees=24500, option_type="CE", amount_rupees=10000)
    reply = _ask(client, ScriptedLLM(LLMTurn(tool_calls=[call]), LLMTurn(text="Please say how many lots.")),
                 "buy nifty 24500 ce worth 10000 rupees")
    assert not any(c.type == "pending_order" for c in reply.cards)
