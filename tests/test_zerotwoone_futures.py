"""Futures on the 021 adapter (against the fake 021): the contract comes from 021's instrument file (real lot size and
token), the order goes out as NSEFO / NRML, positions are read back, and an open future is not counted as cash spent."""

import asyncio
import functools
import time
from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.broker.zerotwoone import ZeroTwoOneAdapter
from app.config import Settings
from app.main import create_app
from app.schemas import Product
from fake021 import Fake021, FakeConnector, ltp_packet

T0 = datetime(2026, 10, 9, 5, 0, tzinfo=timezone.utc)  # the fake file's NIFTY futures: 27 Oct and 24 Nov 2026, lot 65


@pytest.fixture
def fake():
    return Fake021()


@pytest.fixture
def conn():
    return FakeConnector()


@pytest.fixture
def setup(fake, conn):
    adapter = ZeroTwoOneAdapter(username="HACK1234", password="pw", http=fake.client(), connect=conn, clock=lambda: T0,
                                cache_dir=None, retry_delay=0.001, price_wait=0.3)
    settings = Settings(ticker_interval=None, reconcile_interval=None, external_sync_interval=None,
                        timeout_reconcile_delay=0, account_push_interval=3600)
    with TestClient(create_app(settings, broker=adapter, clock=lambda: T0)) as client:
        deadline = time.time() + 2
        while not conn.market and time.time() < deadline:
            time.sleep(0.01)
        yield client, adapter


def call(client, fn, *args, **kw):
    return client.portal.call(functools.partial(fn, *args, **kw))


async def _price(conn, token, ltp):
    conn.market[0].push(ltp_packet(2, token, ltp))  # exchange code 2 = NSE F&O on the market socket
    await asyncio.sleep(0.05)


def future(side="BUY", **over):
    body = dict(action="PLACE", future={"underlying": "NIFTY"}, side=side, lots=1, order_type="MARKET")
    body.update(over)
    return body


def test_the_contract_comes_from_021s_file_with_its_real_lot_size(setup):
    client, adapter = setup
    nearest = call(client, adapter.find_future, "NIFTY")
    assert (nearest.symbol, nearest.expiry, nearest.lot_size, nearest.series) == ("NIFTY261027FUT", date(2026, 10, 27), 65, "FUT")
    assert nearest.tick_size == 10 and nearest.label == "NIFTY FUT (27 Oct 2026)"
    later = call(client, adapter.find_future, "NIFTY", date(2026, 11, 24))
    assert later.symbol == "NIFTY261124FUT"
    assert call(client, adapter.find_future, "NIFTY", date(2026, 12, 29)) is None
    assert call(client, adapter.find_future, "BANKNIFTY") is None
    listing = adapter.master.get(nearest.key)
    assert listing.token == 48704 and listing.request_exchange == "NSEFO"
    assert adapter.master.by_token("NSEFO", listing.token) is listing


def test_an_approved_future_goes_out_as_nsefo_nrml_in_the_real_lot_size(setup, fake, conn):
    client, adapter = setup
    call(client, _price, conn, 48704, 2460000)  # Rs 24,600.00
    reply = client.post("/api/orders/preview", json=future(lots=2)).json()
    [c] = [c for c in reply["cards"] if c["type"] == "pending_order"]
    p = c["pending"]
    assert (p["quantity"], p["product"], p["risk_ack_required"]) == (130, "NRML", True)  # 2 lots of 65
    no_ack = client.post(f"/api/approvals/{p['id']}/approve", json={"order_hash": p["order_hash"]})
    assert no_ack.status_code == 409 and not fake.orders  # nothing reached 021
    r = client.post(f"/api/approvals/{p['id']}/approve", json={"order_hash": p["order_hash"], "acknowledgment": "I UNDERSTAND"})
    assert r.status_code == 200, r.text
    row = fake.orders[max(fake.orders)]
    assert (row["exchange"], row["token"], row["product"], row["qtyRemaining"]) == ("NSEFO", 48704, "NRML", 130)
    [order] = [o for o in call(client, adapter.get_orders) if o.instrument.is_future]
    assert order.instrument.symbol == "NIFTY261027FUT" and order.product is Product.NRML


def test_a_short_future_position_is_read_back_and_can_be_closed_without_the_acknowledgment(setup, fake, conn):
    client, adapter = setup
    call(client, _price, conn, 48704, 2450000)
    fake.positions = [{"exchange": "NSEFO", "token": 48704, "product": "NRML", "netQuantity": -65, "netPrice": 2460000,
                       "prevClose": 2455000}]
    [pos] = [x for x in call(client, adapter.get_positions) if x.instrument.is_future]
    assert (pos.quantity, pos.product, pos.avg_price, pos.ltp) == (-65, Product.NRML, 2460000, 2450000)

    reply = client.post("/api/orders/preview", json=future(side="BUY")).json()  # a buy against a short closes it
    p = reply["cards"][0]["pending"]
    assert p["risk_ack_required"] is False and "closes that part" in " ".join(p["warnings"])
    assert client.post(f"/api/approvals/{p['id']}/approve", json={"order_hash": p["order_hash"]}).status_code == 200


def test_an_open_future_is_not_counted_as_cash_spent_but_a_closed_one_counts_its_gain_or_loss(setup, fake):
    client, adapter = setup
    before = call(client, adapter.get_funds).available_cash
    fake.trades = {1: [{"quantity": 65, "price": 2460000}]}  # bought 1 lot at Rs 24,600: worth Rs 15.99 lakh
    fake.positions = [{"exchange": "NSEFO", "token": 48704, "product": "NRML", "netQuantity": 65, "netPrice": 2460000,
                       "prevClose": 2455000}]
    assert call(client, adapter.get_funds).available_cash == before  # open: no cash has moved
    fake.trades = {1: [{"quantity": 65, "price": 2460000}], 2: [{"quantity": -65, "price": 2470000}]}
    fake.positions = []
    assert call(client, adapter.get_funds).available_cash == before + 65 * 10000  # closed: Rs 10 x 65 gained
