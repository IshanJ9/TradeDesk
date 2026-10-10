"""Futures, end to end on the mock broker: whole lots, buy or sell (including opening a short), 021's Futures charges,
a cap on new exposure, the factual risk notice, the typed acknowledgment enforced by the SERVER, and no futures from voice."""

import functools
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.broker.mock import MockBroker
from app.config import Settings
from app.llm.types import LLMTurn, ToolCall
from app.main import create_app
from app.orders.charges import compute_charges
from app.orders.limits import OrderBlocked
from app.schemas import Exchange, OrderIntent, Product, RejectionReason, Side, paise

T0 = datetime(2026, 10, 10, 5, 0, tzinfo=timezone.utc)  # NIFTY 24500; mock futures expire 27 Oct and 24 Nov 2026
KEY = "NSE:NIFTY261027FUT"
ACK = "I UNDERSTAND"


@pytest.fixture
def broker():
    return MockBroker(clock=lambda: T0)


@pytest.fixture
def client(broker):
    settings = Settings(ticker_interval=None, reconcile_interval=None, timeout_reconcile_delay=0, account_push_interval=3600)
    with TestClient(create_app(settings, broker=broker, clock=lambda: T0)) as c:
        yield c


def future(side="BUY", **over):
    body = dict(action="PLACE", future={"underlying": "NIFTY"}, side=side, lots=1, order_type="MARKET")
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


def approve(client, p, ack=ACK):
    body = {"order_hash": p["order_hash"]}
    if ack is not None:
        body["acknowledgment"] = ack
    return client.post(f"/api/approvals/{p['id']}/approve", json=body)


def seed(client, broker, quantity, avg=24550.0):
    client.portal.call(broker.find_future, "NIFTY")
    broker._positions[KEY] = (quantity, paise(avg))
    broker._position_product[KEY] = Product.NRML


# ---- the card ---------------------------------------------------------------------------------------- #


def test_one_lot_is_75_units_carried_overnight_with_factual_notices_and_needs_the_acknowledgment(client):
    p, text = card(client, future())
    assert p["instrument"]["symbol"] == "NIFTY261027FUT" and p["instrument"]["expiry"] == "2026-10-27"  # nearest, stated
    assert (p["quantity"], p["product"], p["side"], p["risk_ack_required"]) == (75, "NRML", "BUY", True)
    assert text.startswith("You are buying 1 lot (75 units) of NIFTY FUT (27 Oct 2026) (NSE)")
    assert "Contract value is about" in text and "total about" not in text  # not presented as the cash it costs
    notice = " ".join(p["warnings"])
    assert "1 lot of 75 = 75 units" in notice
    assert "buys 75 units on margin" in notice and "lose more than the margin" in notice
    assert "Each ₹1 move in NIFTY FUT (27 Oct 2026) changes this position's value by ₹75.00" in notice
    assert "021's API does not report margin" in notice and "expires on 27 Oct 2026" in notice
    assert "93% of individual traders" in notice


def test_a_sale_with_nothing_held_opens_a_short_and_says_the_loss_has_no_upper_limit(client):
    p, text = card(client, future(side="SELL"))
    assert p["risk_ack_required"] is True and text.startswith("You are selling 1 lot (75 units)")
    assert "sells 75 units you don't hold (a short position)" in " ".join(p["warnings"])
    assert "loss can grow without limit" in " ".join(p["warnings"])


def test_a_named_expiry_and_intraday_are_used(client):
    p, _ = card(client, future(future={"underlying": "NIFTY", "expiry": "2026-11-24"}, product="MIS"))
    assert p["instrument"]["symbol"] == "NIFTY261124FUT" and p["product"] == "MIS"


@pytest.mark.parametrize("over, words", [
    (dict(future={"underlying": "BANKNIFTY"}), "can't find a BANKNIFTY futures contract"),
    (dict(future={"underlying": "NIFTY", "expiry": "2026-12-29"}), "can't find a NIFTY futures contract expiring 29 Dec 2026"),
    (dict(lots=None, quantity=100), "trade in lots of 75"),
])
def test_a_wrong_contract_or_size_is_refused_with_the_reason(client, over, words):
    reply = preview(client, future(**over))
    assert reply["cards"][0]["level"] == "blocked" and words in reply["text"]


# ---- 021's Futures column --------------------------------------------------------------------------- #


def test_futures_charges_follow_021s_futures_column():
    price, qty = paise(24600), 75  # contract value Rs 18,45,000
    sell = compute_charges(exchange=Exchange.NSE, side=Side.SELL, product=Product.NRML, quantity=qty, price=price, future=True)
    assert sell.brokerage == 2000  # flat Rs 20
    assert sell.stt == 92_250  # 0.05% on the sell side: Rs 922.50
    assert sell.exchange_txn == 3_192  # NSE 0.00173%
    assert sell.stamp_duty == 0  # stamp duty is on the buy side only
    assert (sell.sebi_fee, sell.ipft) == (185, 185)  # 0.0001% each
    assert sell.gst == round(0.18 * (2000 + 3_192 + 185))  # 18% on brokerage, exchange charges and IPFT
    buy = compute_charges(exchange=Exchange.NSE, side=Side.BUY, product=Product.NRML, quantity=qty, price=price, future=True)
    assert buy.stt == 0 and buy.stamp_duty == 36_900  # 0.02% on the buy side, as 021's page prints it
    assert buy.gst == round(0.18 * (2000 + 36_900 + 3_192 + 185))


# ---- the cap on new exposure ------------------------------------------------------------------------- #


def test_new_exposure_is_capped_in_lots_per_order(client):
    card(client, future(lots=2))  # the limit is 2 lots
    reply = preview(client, future(lots=3))
    assert reply["cards"][0]["level"] == "blocked"
    assert "3 lots of new exposure; the limit is 2 per order" in reply["text"] and "MAX_FO_LOTS_PER_ORDER" in reply["text"]


def test_closing_what_you_hold_is_never_capped_and_needs_no_acknowledgment(client, broker):
    seed(client, broker, 225)  # 3 lots long: more than one order may open
    p, text = card(client, future(side="SELL", lots=3))
    assert p["risk_ack_required"] is False and p["quantity"] == 225
    assert "closes that part" in " ".join(p["warnings"]) and "which closes" in " ".join(p["warnings"])
    assert approve(client, p, ack=None).status_code == 200  # no acknowledgment, none needed
    assert not [x for x in client.portal.call(broker.get_positions) if x.instrument.is_future]


def test_selling_past_what_you_hold_opens_new_exposure_and_that_part_is_capped_and_acknowledged(client, broker):
    seed(client, broker, 75)  # 1 lot long
    p, _ = card(client, future(side="SELL", lots=2))  # closes 1 lot, opens a 1-lot short
    assert p["risk_ack_required"] is True
    notice = " ".join(p["warnings"])
    assert "closes that part" in notice and "sells 75 units you don't hold" in notice
    too_much = preview(client, future(side="SELL", lots=4))  # closes 1, opens 3: over the 2-lot cap
    assert too_much["cards"][0]["level"] == "blocked" and "3 lots of new exposure" in too_much["text"]


def test_half_of_a_long_position_is_sold_in_whole_lots(client, broker):
    seed(client, broker, 150)
    p, _ = card(client, future(side="SELL", lots=None, fraction_of_holding=0.5))
    assert p["quantity"] == 75


# ---- the server checks the acknowledgment ------------------------------------------------------------ #


@pytest.mark.parametrize("ack", [None, "", "i understand", "yes", "I UNDERSTAND!"])
def test_approve_without_the_exact_acknowledgment_sends_nothing_and_leaves_the_card_open(client, broker, ack):
    p, _ = card(client, future())
    r = approve(client, p, ack=ack)
    assert r.status_code == 409 and r.json()["code"] == "ACK_REQUIRED"
    assert broker._orders == {}
    assert client.app.state.pending.get(p["id"]).state.value == "PENDING"  # nothing was claimed: try again


def test_approve_with_the_acknowledgment_places_the_order_and_creates_the_position(client, broker):
    p, _ = card(client, future(lots=2))
    r = approve(client, p)
    assert r.status_code == 200, r.text
    [pos] = [x for x in client.portal.call(broker.get_positions) if x.instrument.is_future]
    assert (pos.instrument.symbol, pos.quantity, pos.product) == ("NIFTY261027FUT", 150, Product.NRML)


def test_the_acknowledgment_is_not_asked_for_ordinary_share_orders(client):
    reply = preview(client, dict(action="PLACE", instrument_ref="ITC", side="BUY", quantity=1, order_type="MARKET"))
    p = reply["cards"][0]["pending"]
    assert p["risk_ack_required"] is False
    assert approve(client, p, ack=None).status_code == 200


# ---- no futures from voice, rules or plans ------------------------------------------------------------ #


def test_a_dictated_message_cannot_start_a_future(client, broker):
    r = client.post("/api/chat", json={"message": "buy 1 lot nifty futures", "via_voice": True}).json()
    assert not any(c["type"] == "pending_order" for c in r["cards"])
    assert "can't be started from a voice message" in r["text"] and "type the order" in r["text"]
    typed = client.post("/api/chat", json={"message": "buy 1 lot nifty futures"}).json()
    assert any(c["type"] == "pending_order" for c in typed["cards"])
    shares = client.post("/api/chat", json={"message": "buy 1 itc", "via_voice": True}).json()
    assert any(c["type"] == "pending_order" for c in shares["cards"])  # ordinary orders are fine by voice


def test_a_rule_or_plan_cannot_make_a_future_card(client):
    builder = client.app.state.builder
    intent = OrderIntent(**future())
    for kw in ({"rule_id": "r1"}, {"plan_id": "p1"}):
        with pytest.raises(OrderBlocked) as err:
            client.portal.call(functools.partial(builder.build, intent, **kw))
        assert err.value.reason is RejectionReason.SEGMENT_NOT_ALLOWED and "one at a time" in err.value.message


def test_the_model_cannot_switch_the_voice_block_off(client):
    from app.llm.tools import ProposeOrderInput
    assert "via_voice" not in ProposeOrderInput.model_json_schema()["properties"]


# ---- through the assistant's tool and the built-in reader --------------------------------------------- #


class ScriptedLLM:
    def __init__(self, *turns):
        self.turns = list(turns)

    async def complete(self, *, system, messages, tools):
        return self.turns.pop(0) if self.turns else LLMTurn(text="")


def _ask(client, llm, message, via_voice=False):
    copilot = client.app.state.copilot
    copilot._llm = llm
    return client.portal.call(functools.partial(copilot.handle, message, via_voice))


def _propose(**args):
    return ToolCall(id="c1", name="propose_order", input={"action": "PLACE", "side": "BUY", "order_type": "MARKET", **args})


def test_the_tool_drafts_a_futures_card_from_the_traders_words(client):
    call = _propose(future_underlying="NIFTY", lots=1)
    reply = _ask(client, ScriptedLLM(LLMTurn(tool_calls=[call]), LLMTurn(text="")), "buy 1 lot nifty futures")
    [c] = [c for c in reply.cards if c.type == "pending_order"]
    assert c.pending.quantity == 75 and c.pending.instrument.is_future and c.pending.risk_ack_required


def test_the_tool_is_refused_for_a_voice_message_even_if_the_model_asks(client):
    call = _propose(future_underlying="NIFTY", lots=1)
    reply = _ask(client, ScriptedLLM(LLMTurn(tool_calls=[call]), LLMTurn(text="")), "buy 1 lot nifty futures", via_voice=True)
    assert not any(c.type == "pending_order" for c in reply.cards)


def test_a_future_with_a_strike_is_invalid(client):
    call = _propose(future_underlying="NIFTY", strike_rupees=24500, option_type="CE", lots=1)
    reply = _ask(client, ScriptedLLM(LLMTurn(tool_calls=[call]), LLMTurn(text="Which one?")), "buy 1 lot nifty futures")
    assert not any(c.type == "pending_order" for c in reply.cards)


def test_a_number_the_trader_didnt_type_stops_the_card(client):
    call = _propose(future_underlying="NIFTY", lots=3)
    reply = _ask(client, ScriptedLLM(LLMTurn(tool_calls=[call]), LLMTurn(text="")), "buy 1 lot nifty futures")
    assert not any(c.type == "pending_order" for c in reply.cards)
    assert "the number of lots as 3" in reply.text


def test_the_built_in_reader_understands_futures_wording(client, broker):
    [c] = [c for c in client.post("/api/chat", json={"message": "buy 2 lots nifty futures 2026-11-24"}).json()["cards"]
           if c["type"] == "pending_order"]
    assert c["pending"]["instrument"]["symbol"] == "NIFTY261124FUT" and c["pending"]["quantity"] == 150
    assert not [c for c in client.post("/api/chat", json={"message": "buy nifty futures"}).json()["cards"]
                if c["type"] == "pending_order"]  # no lots: nothing is guessed
    assert not [c for c in client.post("/api/chat", json={"message": "sell nifty futures"}).json()["cards"]
                if c["type"] == "pending_order"]  # a short is never implied by "sell"
    seed(client, broker, 75)
    [c] = [c for c in client.post("/api/chat", json={"message": "sell all my nifty futures"}).json()["cards"]
           if c["type"] == "pending_order"]
    assert c["pending"]["quantity"] == 75 and c["pending"]["risk_ack_required"] is False
