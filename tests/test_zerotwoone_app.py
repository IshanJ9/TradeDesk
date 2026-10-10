"""The whole app running on the 021 adapter (against the fake 021): cards, approval, the send log, and the
timeout cases that matter, with no client order id anywhere on the wire."""

import asyncio
import functools
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.broker.zerotwoone import ZeroTwoOneAdapter
from app.broker.zerotwoone.adapter import BrokerAuthFailed
from app.config import Settings
from app.main import create_app, make_broker
from app.schemas import paise
from fake021 import Fake021, FakeConnector, full_nse_cash, ltp_packet

T0 = datetime(2026, 10, 9, 10, 0, tzinfo=timezone.utc)


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
def fake():
    return Fake021()


@pytest.fixture
def conn():
    return FakeConnector()


@pytest.fixture
def client(fake, conn, clock):
    adapter = ZeroTwoOneAdapter(
        username="HACK1234", password="pw", http=fake.client(), connect=conn, clock=clock, cache_dir=None,
        retry_delay=0.001, price_wait=0.3,
    )
    settings = Settings(ticker_interval=None, reconcile_interval=None, timeout_reconcile_delay=0, account_push_interval=3600)
    with TestClient(create_app(settings, broker=adapter, clock=clock)) as c:
        deadline = time.time() + 2
        while not conn.market and time.time() < deadline:
            time.sleep(0.01)
        on_loop(c, push, conn, ltp_packet(1, 1594, 145000))  # INFY at 1450.00
        on_loop(c, push, conn, full_nse_cash(1594, 145000, 144000, 144500, 146000, 143500))
        yield c


async def push(conn, frame):
    conn.market[0].push(frame)
    await asyncio.sleep(0.02)


def on_loop(client, fn, *args, **kwargs):
    return client.portal.call(functools.partial(fn, *args, **kwargs))


def card(client, **over):
    body = dict(action="PLACE", instrument_ref="INFY", side="BUY", quantity=10, order_type="LIMIT", limit_price=paise(1450))
    body.update(over)
    r = client.post("/api/orders/preview", json=body)
    assert r.status_code == 200, r.text
    c = r.json()["cards"][0]
    assert c["type"] == "pending_order", r.json()
    return c["pending"]


def approve(client, p):
    return client.post(f"/api/approvals/{p['id']}/approve", json={"order_hash": p["order_hash"]})


def posts(fake):
    return [r for r in fake.requests if r[0] == "POST" and r[1] == "/orders"]


def test_the_pitch_order_end_to_end_on_the_021_adapter(client, fake):
    p = card(client)
    assert p["instrument"]["symbol"] == "INFY" and p["ref_ltp"] == paise(1450)
    assert fake.orders == {}  # a card sends nothing
    fake.auto_fill = True
    body = approve(client, p).json()
    assert body["outcome"] == "SENT" and body["order"]["status"] == "FILLED" and body["order"]["avg_fill_price"] == paise(1450)
    assert len(fake.orders) == 1 and len(posts(fake)) == 1
    assert "client" not in str(posts(fake)[0][2]).lower()  # 021 never sees our idempotency key


def test_a_double_click_reaches_021_once(client, fake):
    p = card(client)
    first, second = approve(client, p), approve(client, p)
    assert first.status_code == 200 and second.status_code == 409
    assert len(posts(fake)) == 1


def test_reply_lost_after_021_took_the_order_it_is_found_not_re_sent(client, fake):
    p = card(client)
    fake.fail_next.append({"match": "POST /orders", "apply": True, "status": 503, "error": "Service temporarily unavailable"})
    body = approve(client, p).json()
    assert body["outcome"] == "SENT" and "Confirmed in your order book" in body["message"]
    assert len(fake.orders) == 1 and len(posts(fake)) == 1  # one order, one POST: never re-sent
    [row] = client.app.state.db.query("SELECT status, broker_order_id FROM executions")
    assert (row["status"], row["broker_order_id"]) == ("SENT", "1042")


def test_request_lost_before_021_saw_it_is_unknown_then_not_sent_after_the_wait_and_never_retried(client, fake, clock):
    p = card(client)
    fake.fail_next.append({"match": "POST /orders", "status": 503, "error": "down"})
    body = approve(client, p).json()
    assert body["outcome"] == "UNKNOWN" and "NOT been re-sent" in body["message"]
    assert client.post("/api/executions/reconcile").json() == {"resolved": 0, "unresolved": 1}
    clock.advance(seconds=121)
    assert client.post("/api/executions/reconcile").json() == {"resolved": 1, "unresolved": 0}
    [row] = client.app.state.db.query("SELECT status FROM executions")
    assert row["status"] == "NOT_SENT" and fake.orders == {} and len(posts(fake)) == 1


def test_an_identical_order_already_in_the_book_makes_a_timeout_unknown_not_a_guess(client, fake):
    first = card(client, limit_price=paise(1440))
    assert approve(client, first).json()["outcome"] == "SENT"
    twin = dict(fake.orders[1042], orderId=2000, exchangeOrderNumber="x")  # the same order, placed elsewhere
    fake.orders[2000] = twin
    fake.trades[2000] = []
    second = card(client, limit_price=paise(1440))
    fake.fail_next.append({"match": "POST /orders", "apply": True, "status": 500, "error": "Internal Server Error"})
    body = approve(client, second).json()
    assert body["outcome"] == "UNKNOWN" and body["order"] is None
    assert len(fake.orders) == 3 and len(posts(fake)) == 2  # still no re-send
    assert client.post("/api/executions/reconcile").json() == {"resolved": 0, "unresolved": 1}


def test_a_risk_refusal_from_021_is_reported_as_a_rejection(client, fake):
    p = card(client)
    fake.fail_next.append({"match": "POST /orders", "status": 500, "error": "Insufficient funds"})
    body = approve(client, p).json()
    assert body["outcome"] == "REJECTED" and body["order"] is None
    assert [r["status"] for r in client.app.state.db.query("SELECT status FROM executions")] == ["REJECTED"]


def test_a_stop_loss_goes_to_021_in_the_sl_book_and_waits(client, fake):
    fake.holdings = [{"isin": "INE009A01021", "symbol": "INFY", "freeQty": 20, "btstQty": 0, "sellQty": 0, "price": 138000, "prevClose": 144000}]
    p = card(client, side="SELL", quantity=5, order_type="STOP_LIMIT", trigger_price=paise(1400), limit_price=None)
    assert p["trigger_price"] == paise(1400) and p["limit_price"] < paise(1400)
    body = approve(client, p).json()
    assert body["outcome"] == "SENT" and body["order"]["status"] == "OPEN"
    row = fake.orders[1042]
    assert row["book"] == "SL" and row["triggerPrice"] == paise(1400) and row["qtyRemaining"] == -5


def test_changing_the_price_at_the_broker_between_card_and_click_asks_again(client, fake, conn):
    p = card(client)
    on_loop(client, push, conn, ltp_packet(1, 1594, 147000))  # +1.4%
    r = approve(client, p)
    assert r.status_code == 409 and r.json()["code"] == "REQUOTE_REQUIRED"
    assert fake.orders == {}


# ---- starting up -------------------------------------------------------------------------------- #


def test_the_app_does_not_start_with_a_wrong_password(fake, conn):
    adapter = ZeroTwoOneAdapter(username="HACK1234", password="nope", http=fake.client(), connect=conn, cache_dir=None)
    app = create_app(Settings(ticker_interval=None, reconcile_interval=None), broker=adapter)
    with pytest.raises(BrokerAuthFailed):
        with TestClient(app):
            pass


def test_choosing_the_021_broker_without_credentials_says_what_is_missing():
    with pytest.raises(RuntimeError, match="ZEROTWOONE_USERNAME"):
        make_broker(Settings(broker="zerotwoone"))


def test_credentials_never_show_up_in_the_settings_repr():
    s = Settings(broker="zerotwoone", zerotwoone_username="HACK1234", zerotwoone_password="hunter2")
    assert "hunter2" not in repr(s) and "HACK1234" not in repr(s)


# ---- the chaos tool used against the live sandbox, proven here against the fake one ------------------- #


import httpx  # noqa: E402

from app.broker.zerotwoone.chaos import ChaosTransport  # noqa: E402


@pytest.fixture
def chaos_client(fake, conn, clock):
    chaos = ChaosTransport(httpx.MockTransport(fake._handle))
    adapter = ZeroTwoOneAdapter(
        username="HACK1234", password="pw", http=httpx.AsyncClient(transport=chaos, timeout=5), connect=conn,
        clock=clock, cache_dir=None, retry_delay=0.001, price_wait=0.3,
    )
    settings = Settings(ticker_interval=None, reconcile_interval=None, timeout_reconcile_delay=0, account_push_interval=3600)
    with TestClient(create_app(settings, broker=adapter, clock=clock)) as c:
        deadline = time.time() + 2
        while not conn.market and time.time() < deadline:
            time.sleep(0.01)
        on_loop(c, push, conn, full_nse_cash(1594, 145000, 144000, 144500, 146000, 143500))
        yield c, chaos


@pytest.mark.parametrize("mode", ["lost-reply", "http-500", "http-503"])
def test_when_021_took_the_order_but_the_answer_was_lost_we_find_it_and_never_send_twice(chaos_client, fake, mode):
    client, chaos = chaos_client
    p = card(client)
    chaos.arm(mode)
    body = approve(client, p).json()
    assert body["outcome"] == "SENT" and "Confirmed in your order book" in body["message"]
    assert chaos.sabotaged == 1 and len(fake.orders) == 1 and len(posts(fake)) == 1


def test_when_the_request_never_arrived_nothing_exists_and_nothing_is_resent(chaos_client, fake, clock):
    client, chaos = chaos_client
    p = card(client)
    chaos.arm("lost-request")
    body = approve(client, p).json()
    assert body["outcome"] == "UNKNOWN" and "NOT been re-sent" in body["message"]
    assert fake.orders == {} and chaos.sabotaged == 1
    clock.advance(seconds=121)
    assert client.post("/api/executions/reconcile").json() == {"resolved": 1, "unresolved": 0}
    assert [r["status"] for r in client.app.state.db.query("SELECT status FROM executions")] == ["NOT_SENT"]


def test_the_chaos_tool_only_ever_touches_one_order_call_and_rejects_unknown_modes():
    chaos = ChaosTransport(httpx.MockTransport(lambda r: httpx.Response(200, json=[])))
    with pytest.raises(ValueError):
        chaos.arm("explode")
    assert not chaos.armed
