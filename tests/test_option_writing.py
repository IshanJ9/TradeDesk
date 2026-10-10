"""Selling options the trader does not hold (writing), on the mock broker: capped in lots, a factual notice with the
real worst case, the typed acknowledgment enforced by the SERVER, never by voice, buying back a short is closing, and
both futures and writing pause while the trader is past a limit they set themselves."""

import functools
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.broker.mock import MockBroker
from app.config import Settings
from app.main import create_app
from app.orders.charges import compute_charges
from app.risk.presets import preset
from app.schemas import Exchange, OptionType, Product, Side, paise

T0 = datetime(2026, 10, 10, 5, 0, tzinfo=timezone.utc)  # NIFTY 24500; mock expiries 13, 20, 27 Oct 2026
CALL_KEY = "NSE:NIFTY26101324500CE"
ACK = "I UNDERSTAND"


@pytest.fixture
def broker():
    return MockBroker(clock=lambda: T0)


@pytest.fixture
def client(broker):
    settings = Settings(allow_unlimited_risk_fo=True, ticker_interval=None, reconcile_interval=None, timeout_reconcile_delay=0, account_push_interval=3600)
    with TestClient(create_app(settings, broker=broker, clock=lambda: T0)) as c:
        yield c


def opt(side="SELL", kind="CE", **over):
    body = dict(action="PLACE", option={"underlying": "NIFTY", "strike": paise(24500), "option_type": kind},
                side=side, lots=1, order_type="MARKET")
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


def seed(client, broker, quantity, avg=12000):
    client.portal.call(functools.partial(broker.find_option, "NIFTY", paise(24500), OptionType.CE))
    broker._positions[CALL_KEY] = (quantity, avg)
    broker._position_product[CALL_KEY] = Product.NRML


# ---- the card ---------------------------------------------------------------------------------------- #


def test_writing_a_call_says_the_loss_has_no_upper_limit_and_the_gain_is_the_premium(client):
    p, text = card(client, opt(side="SELL", kind="CE"))
    notice = " ".join(p["warnings"])
    assert p["risk_ack_required"] is True and (p["quantity"], p["product"]) == (75, "NRML")
    assert "sells 75 units of a call you don't hold (writing it)" in notice
    assert "that is the most you can gain" in notice
    assert "If NIFTY rises above ₹24,500.00, the loss grows with the price and has no upper limit." in notice
    assert "021's API does not report margin" in notice and "expires on 13 Oct 2026" in notice
    assert "93% of individual traders" in notice
    assert "expire worthless" not in notice  # that notice belongs to buying


def test_writing_a_put_states_the_worst_case_as_the_strike_less_the_premium(client, broker):
    p, _ = card(client, opt(side="SELL", kind="PE"))
    premium = p["protection_price"]  # the card prices a market order at its protected limit
    units = 75
    worst = units * paise(24500) - units * premium
    notice = " ".join(p["warnings"])
    assert "If NIFTY falls below ₹24,500.00, you lose on every point it falls" in notice
    from app.schemas import fmt_rupees
    assert f"if it fell to zero the loss would be about {fmt_rupees(worst)}" in notice
    assert "no upper limit" not in notice  # a put's loss is bounded by the strike


def test_a_written_option_is_charged_on_021s_options_column(client):
    p, _ = card(client, opt(side="SELL"))
    want = compute_charges(exchange=Exchange.NSE, side=Side.SELL, product=Product.NRML, quantity=75,
                           price=p["protection_price"], option=True)
    assert p["charges"]["stt"] == want.stt > 0 and p["charges"]["brokerage"] == 2000  # STT 0.15% on the sell side


# ---- the cap on new exposure -------------------------------------------------------------------------- #


def test_new_short_exposure_is_capped_in_lots(client):
    card(client, opt(lots=2))
    reply = preview(client, opt(lots=3))
    assert reply["cards"][0]["level"] == "blocked" and "3 lots of new exposure; the limit is 2 per order" in reply["text"]


def test_only_the_part_beyond_what_you_hold_is_capped(client, broker):
    seed(client, broker, 225)  # 3 lots long
    card(client, opt(lots=5))  # closes 3, opens 2: allowed
    reply = preview(client, opt(lots=6))  # closes 3, opens 3: over the cap
    assert reply["cards"][0]["level"] == "blocked" and "3 lots of new exposure" in reply["text"]


# ---- the server checks the acknowledgment ------------------------------------------------------------- #


@pytest.mark.parametrize("ack", [None, "", "i understand", "ok"])
def test_approving_a_write_without_the_exact_words_sends_nothing(client, broker, ack):
    p, _ = card(client, opt())
    r = approve(client, p, ack=ack)
    assert r.status_code == 409 and r.json()["code"] == "ACK_REQUIRED"
    assert broker._orders == {} and client.app.state.pending.get(p["id"]).state.value == "PENDING"


def test_a_written_option_becomes_a_short_position_that_can_be_bought_back_without_the_words(client, broker):
    p, _ = card(client, opt(lots=2))
    assert approve(client, p).status_code == 200
    [pos] = [x for x in client.portal.call(broker.get_positions) if x.instrument.is_option]
    assert (pos.quantity, pos.product) == (-150, Product.NRML)  # short 2 lots

    back, text = card(client, opt(side="BUY", lots=1))  # buying back part of a short is closing
    notice = " ".join(back["warnings"])
    assert back["risk_ack_required"] is False
    assert "against your 150 units short, which closes that part" in notice
    assert "expire worthless" not in notice and "93%" not in notice  # no premium is being put at risk
    assert approve(client, back, ack=None).status_code == 200

    over, _ = card(client, opt(side="BUY", lots=2))  # 1 lot closes the rest, 1 lot is a new long
    assert over["risk_ack_required"] is False
    assert "against your 75 units short" in " ".join(over["warnings"]) and "expire worthless" in " ".join(over["warnings"])


# ---- never by voice, rule or plan --------------------------------------------------------------------- #


def test_a_dictated_message_cannot_write_an_option_but_can_close_one(client, broker):
    r = client.post("/api/chat", json={"message": "sell 1 lot nifty 24500 ce", "via_voice": True}).json()
    assert not any(c["type"] == "pending_order" for c in r["cards"])
    assert "can't be started from a voice message" in r["text"]
    typed = client.post("/api/chat", json={"message": "sell 1 lot nifty 24500 ce"}).json()
    assert any(c["type"] == "pending_order" for c in typed["cards"])
    seed(client, broker, 75)
    close = client.post("/api/chat", json={"message": "sell 1 lot nifty 24500 ce", "via_voice": True}).json()
    assert any(c["type"] == "pending_order" for c in close["cards"])  # selling what you hold is not writing


# ---- paused while past a limit you set yourself ------------------------------------------------------- #


def go_past_a_limit_you_set(client):
    """One order placed today, and a limit of one order a day: every further order carries your own warning."""
    shares = client.post("/api/orders/preview", json=dict(action="PLACE", instrument_ref="ITC", side="BUY", quantity=1,
                                                         order_type="MARKET")).json()["cards"][0]["pending"]
    assert approve(client, shares, ack=None).status_code == 200
    profile = preset("balanced").model_copy(update={"max_orders_per_day": 1})
    assert client.put("/api/profile", json=profile.model_dump(mode="json")).status_code == 200


def test_futures_and_writing_pause_while_past_your_own_limit_but_closing_still_works(client, broker):
    seed(client, broker, 75)  # 1 lot of the call held
    client.portal.call(broker.find_future, "NIFTY")
    broker._positions["NSE:NIFTY261027FUT"] = (75, paise(24550))
    broker._position_product["NSE:NIFTY261027FUT"] = Product.NRML
    go_past_a_limit_you_set(client)

    for body in (opt(side="SELL", kind="PE"),
                 dict(action="PLACE", future={"underlying": "NIFTY"}, side="BUY", lots=1, order_type="MARKET")):
        reply = preview(client, body)
        assert reply["cards"][0]["level"] == "blocked"
        assert "You are past a limit you set for yourself, so new futures and short options are paused" in reply["text"]
    close_option, _ = card(client, opt(side="SELL"))  # closes the long 1 lot: not new exposure
    close_future, _ = card(client, dict(action="PLACE", future={"underlying": "NIFTY"}, side="SELL", lots=1, order_type="MARKET"))
    assert not close_option["risk_ack_required"] and not close_future["risk_ack_required"]
    assert any("You set" in w for w in close_option["warnings"])  # the ordinary warning still shows on a closing card


def test_the_pause_is_checked_again_at_the_click(client, broker):
    p, _ = card(client, opt(side="SELL", kind="PE"))  # no limit crossed yet: a card is made
    go_past_a_limit_you_set(client)
    r = approve(client, p)
    assert r.status_code == 409 and "paused" in r.json()["message"]
    assert not [o for o in broker._orders.values() if o.instrument.is_option]  # nothing was sent
