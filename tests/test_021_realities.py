"""What 021's real API forces on us, tested against a mock that behaves the same way.

- There is no client order id on the wire, so 'did my order get there?' is answered by matching what the
  order book contains. Look-alikes must never be guessed at.
- Stop-loss orders (book SL with a trigger) are first-class: preview, approve, move, fill.
- An ambiguous HTTP 500/503 on an order is never reported as a clean rejection.
"""

import functools
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.broker.base import BrokerRejected, BrokerTimeout
from app.broker.errors import classify_order_failure, reason_from_text
from app.broker.mock import MockBroker
from app.config import Settings
from app.main import create_app
from app.schemas import PendingState, RejectionReason, paise

T0 = datetime(2026, 10, 8, 10, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def broker(clock):
    return MockBroker(clock=clock)


@pytest.fixture
def client(broker, clock):
    settings = Settings(ticker_interval=None, reconcile_interval=None, timeout_reconcile_delay=0, account_push_interval=3600)
    with TestClient(create_app(settings, broker=broker, clock=clock)) as c:
        yield c


def on_loop(client, fn, *args, **kwargs):
    return client.portal.call(functools.partial(fn, *args, **kwargs))


def body(symbol="INFY", **over):
    b = dict(action="PLACE", instrument_ref=symbol, side="BUY", quantity=10, order_type="LIMIT", limit_price=paise(1400))
    b.update(over)
    return b


def card(client, **over):
    r = client.post("/api/orders/preview", json=body(**over))
    assert r.status_code == 200, r.text
    c = r.json()["cards"][0]
    assert c["type"] == "pending_order", r.json()
    return c["pending"]


def approve(client, p):
    return client.post(f"/api/approvals/{p['id']}/approve", json={"order_hash": p["order_hash"]})


def reconcile(client):
    return client.post("/api/executions/reconcile").json()


def statuses(client):
    return [r["status"] for r in client.app.state.db.query("SELECT status FROM executions ORDER BY created_at, rowid")]


# ============================================================================================ #
# Reconciling without a client order id
# ============================================================================================ #


def test_an_order_that_looks_like_someone_elses_is_not_claimed_as_ours(client, broker, clock):
    """Two identical cards. The first send times out and never arrives; the second goes through.
    The book now holds one matching order, but it belongs to the second send, not the first."""
    first, second = card(client), card(client)
    broker.timeout_next_place(accepted=False)
    assert approve(client, first).json()["outcome"] == "UNKNOWN"
    assert approve(client, second).json()["outcome"] == "SENT"

    assert reconcile(client) == {"resolved": 0, "unresolved": 1}  # the book's one order is already accounted for
    assert statuses(client) == ["UNKNOWN", "SENT"]
    clock.advance(seconds=300)
    assert reconcile(client) == {"resolved": 1, "unresolved": 0}
    assert statuses(client) == ["NOT_SENT", "SENT"]
    assert len(client.get("/api/orders").json()) == 1  # and nothing was ever re-sent


def test_look_alike_orders_leave_the_outcome_unknown_instead_of_guessing(client, broker, clock):
    first = card(client)
    assert approve(client, first).json()["outcome"] == "SENT"
    # the trader (or another app) placed the very same order elsewhere: an unclaimed look-alike
    lookalike = next(iter(broker._orders.values())).model_copy(update={"order_id": "FOREIGN1"})
    broker._orders["FOREIGN1"] = lookalike

    second = card(client)
    broker.timeout_next_place(accepted=True)  # the order did arrive, but we never saw the answer
    result = approve(client, second).json()
    assert result["outcome"] == "UNKNOWN" and result["order"] is None
    assert "NOT been re-sent" in result["message"]
    assert len(client.get("/api/orders").json()) == 3  # first, the look-alike, and the unacknowledged one

    clock.advance(seconds=3600)  # time never turns a guess into a fact
    assert reconcile(client) == {"resolved": 0, "unresolved": 1}
    assert statuses(client)[-1] == "UNKNOWN"
    summaries = " ".join(e["summary"] for e in client.get("/api/audit?kind=RECONCILE").json())
    assert "can't tell which one is ours" in summaries


def test_never_placed_is_only_concluded_after_the_longer_grace(client, broker, clock):
    p = card(client)
    broker.timeout_next_place(accepted=False)
    approve(client, p)
    clock.advance(seconds=60)  # the old 30s rule would already have said 'never placed'
    assert reconcile(client) == {"resolved": 0, "unresolved": 1}
    clock.advance(seconds=61)
    assert reconcile(client) == {"resolved": 1, "unresolved": 0}


def test_a_late_arriving_order_is_still_found_while_we_wait(client, broker, clock):
    """The order shows up in the book a little after the timeout: it is found, not declared missing."""
    p = card(client)
    broker.timeout_next_place(accepted=False)
    approve(client, p)
    clock.advance(seconds=20)
    # it turns out the broker did place it after all (a slow queue)
    stored = client.app.state.pending.get(p["id"]).model_copy(update={"state": PendingState.APPROVED})
    on_loop(client, broker.place_order, stored)
    assert reconcile(client) == {"resolved": 1, "unresolved": 0}
    [row] = client.app.state.db.query("SELECT status, broker_order_id FROM executions")
    assert row["status"] == "SENT" and row["broker_order_id"]
    assert len(client.get("/api/orders").json()) == 1


# ============================================================================================ #
# Stop-loss orders
# ============================================================================================ #


def stop_body(**over):
    return body(side="SELL", quantity=5, order_type="STOP_LIMIT", trigger_price=paise(1400), limit_price=None, **over)


def test_a_stop_loss_card_says_what_will_happen_and_when(client):
    p = card(client, **{k: v for k, v in stop_body().items() if k not in ("instrument_ref",)})
    assert p["order_type"] == "STOP_LIMIT" and p["trigger_price"] == paise(1400)
    assert p["limit_price"] < p["trigger_price"]  # a sell stop's limit sits just under its trigger
    text = client.post("/api/orders/preview", json=stop_body()).json()["text"]
    assert "stop-loss" in text and "falls to ₹1,400.00" in text and "Nothing is traded until that price is reached" in text
    assert any("Nothing is sold or bought now" in w for w in p["warnings"])
    assert client.get("/api/orders").json() == []  # a card sends nothing


def test_an_approved_stop_loss_sleeps_then_sells_when_the_price_falls(client, broker):
    p = card(client, **{k: v for k, v in stop_body().items() if k != "instrument_ref"})
    result = approve(client, p).json()
    assert result["outcome"] == "SENT" and result["order"]["status"] == "OPEN"  # waiting, not filled
    assert result["order"]["trigger_price"] == paise(1400)
    held = lambda: next(h for h in client.get("/api/account").json()["holdings"] if h["instrument"]["symbol"] == "INFY")["quantity"]
    assert held() == 20
    on_loop(client, broker.set_price, "NSE:INFY", paise(1399))
    assert client.get("/api/orders").json()[0]["status"] == "FILLED"
    assert held() == 15


@pytest.mark.parametrize("trigger, why", [(1460, "below the current price"), (1448, "below the current price")])
def test_a_sell_stop_above_the_price_is_refused_before_a_card_exists(client, trigger, why):
    r = client.post("/api/orders/preview", json=stop_body() | {"trigger_price": paise(trigger)})
    reply = r.json()
    assert reply["cards"][0]["level"] == "blocked" and why in reply["text"] and "straight away" in reply["text"]
    assert client.get("/api/pending").json()["orders"] == []


def test_a_buy_stop_below_the_price_is_refused(client):
    r = client.post("/api/orders/preview", json=body(side="BUY", order_type="STOP_LIMIT", trigger_price=paise(1440), limit_price=None))
    assert r.json()["cards"][0]["level"] == "blocked" and "above the current price" in r.json()["text"]


def test_a_stop_limit_on_the_wrong_side_of_its_trigger_is_refused(client):
    r = client.post("/api/orders/preview", json=stop_body() | {"limit_price": paise(1410)})  # a sell limit above its trigger
    assert r.json()["cards"][0]["level"] == "blocked" and "at or below the trigger" in r.json()["text"]


def test_moving_a_stop_loss_up_builds_a_modify_card_with_a_new_trigger(client, broker):
    placed = approve(client, card(client, **{k: v for k, v in stop_body().items() if k != "instrument_ref"})).json()["order"]
    r = client.post(
        "/api/orders/preview",
        json=dict(action="MODIFY", target_order_id=placed["order_id"], trigger_price=paise(1420)),
    )
    m = r.json()["cards"][0]["pending"]
    assert m["action"] == "MODIFY" and m["trigger_price"] == paise(1420) and m["limit_price"] < paise(1420)
    assert approve(client, m).json()["outcome"] == "SENT"
    order = client.get("/api/orders").json()[0]
    assert order["trigger_price"] == paise(1420) and order["status"] == "OPEN"


def test_a_trigger_cannot_be_moved_on_an_ordinary_limit_order(client):
    placed = approve(client, card(client)).json()["order"]
    assert placed["status"] == "OPEN"
    r = client.post("/api/orders/preview", json=dict(action="MODIFY", target_order_id=placed["order_id"], trigger_price=paise(1420)))
    assert r.json()["cards"][0]["level"] == "blocked" and "not a stop-loss" in r.json()["text"]


def test_a_stop_card_that_drifts_is_requoted_with_the_same_stop(client, broker):
    p = card(client, **{k: v for k, v in stop_body().items() if k != "instrument_ref"})
    on_loop(client, broker.set_price, "NSE:INFY", paise(1470))  # +1.5%: over the 1% drift limit
    r = approve(client, p)
    assert r.status_code == 409 and r.json()["code"] == "REQUOTE_REQUIRED"
    fresh = r.json()["pending"]
    assert fresh["order_type"] == "STOP_LIMIT" and fresh["trigger_price"] == paise(1400) and fresh["id"] != p["id"]


# ---- through the chat, with the built-in stand-in model ------------------------------------------- #


def chat(client, text):
    r = client.post("/api/chat", json={"message": text})
    assert r.status_code == 200, r.text
    return r.json()


def test_chat_understands_a_stop_loss_request(client):
    reply = chat(client, "sell 5 infosys with a stop loss at 1400")
    [c] = [c for c in reply["cards"] if c["type"] == "pending_order"]
    assert c["pending"]["order_type"] == "STOP_LIMIT" and c["pending"]["trigger_price"] == paise(1400)
    assert client.get("/api/orders").json() == []


def test_chat_can_move_my_stop_loss_on_a_stock(client):
    approve(client, card(client, **{k: v for k, v in stop_body().items() if k != "instrument_ref"}))
    reply = chat(client, "move my stop loss on infosys up to 1420")
    [c] = [c for c in reply["cards"] if c["type"] == "pending_order"]
    assert c["pending"]["action"] == "MODIFY" and c["pending"]["trigger_price"] == paise(1420)


def test_chat_says_so_when_there_is_no_stop_loss_to_move(client):
    reply = chat(client, "move my stop loss on infosys up to 1420")
    assert not [c for c in reply["cards"] if c["type"] == "pending_order"]
    assert "can't find an open stop-loss" in reply["text"]


# ============================================================================================ #
# HTTP failures: only a clear refusal counts as a rejection
# ============================================================================================ #


@pytest.mark.parametrize("status", [400, 401, 403, 421, 422])
def test_statuses_that_mean_not_processed_are_rejections(status):
    err = classify_order_failure(status, "Blocked by safe mode")
    assert isinstance(err, BrokerRejected)


@pytest.mark.parametrize(
    "text, reason",
    [
        ("Insufficient funds for this order", RejectionReason.INSUFFICIENT_FUNDS),
        ("Price outside circuit limits", RejectionReason.PRICE_BAND),
        ("Quantity is not a multiple of lot size", RejectionReason.INVALID_QUANTITY),
        ("Trigger price must be above LTP", RejectionReason.RISK_CHECK),
        ("Order rejected by risk checks", RejectionReason.RISK_CHECK),
    ],
)
def test_a_500_that_clearly_describes_a_refusal_is_a_rejection(text, reason):
    err = classify_order_failure(500, text)
    assert isinstance(err, BrokerRejected) and err.reason is reason


@pytest.mark.parametrize("text", ["", "Internal Server Error", "Something went wrong", "upstream connect error", "NullPointerException at x.y"])
def test_a_500_without_a_clear_refusal_is_unknown_not_rejected(text):
    assert isinstance(classify_order_failure(500, text), BrokerTimeout)


@pytest.mark.parametrize("status", [503, 502, 504, 429, None])
def test_503_rate_limits_and_dropped_connections_are_unknown_for_an_order(status):
    assert isinstance(classify_order_failure(status, "Service temporarily unavailable"), BrokerTimeout)


def test_reason_from_text_is_conservative():
    assert reason_from_text("") is None and reason_from_text(None) is None
    assert reason_from_text("We encountered a problem") is None
