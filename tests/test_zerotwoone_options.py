"""Options on the 021 adapter (against the fake 021): the contract comes from 021's instrument file (real lot size and
token), the order goes out as NSEFO / NRML, and F&O rows in the order book and positions are read back, not dropped."""

import asyncio
import functools
import time
from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.broker.zerotwoone import ZeroTwoOneAdapter
from app.config import Settings
from app.main import create_app
from app.schemas import OptionType, Product, paise
from fake021 import Fake021, FakeConnector, ltp_packet

T0 = datetime(2026, 10, 9, 5, 0, tzinfo=timezone.utc)  # the fake file's NIFTY expiries: 13 and 20 Oct 2026


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


def option(side="BUY", **over):
    body = dict(action="PLACE", option={"underlying": "NIFTY", "strike": paise(24500), "option_type": "CE"},
                side=side, lots=1, order_type="MARKET")
    body.update(over)
    return body


def test_the_contract_comes_from_021s_file_with_its_real_lot_size(setup):
    client, adapter = setup
    nearest = call(client, adapter.find_option, "NIFTY", paise(24500), OptionType.CE)
    assert (nearest.symbol, nearest.expiry, nearest.lot_size, nearest.series) == ("NIFTY26101324500CE", date(2026, 10, 13), 75, "OPT")
    later = call(client, adapter.find_option, "NIFTY", paise(24500), OptionType.PE, date(2026, 10, 20))
    assert later.symbol == "NIFTY26102024500PE"
    assert call(client, adapter.find_option, "NIFTY", paise(24525), OptionType.CE) is None  # no such strike
    assert call(client, adapter.find_option, "NIFTY", paise(24500), OptionType.CE, date(2026, 10, 27)) is None
    listing = adapter.master.get(nearest.key)
    assert listing.request_exchange == "NSEFO" and adapter.master.by_token("NSEFO", listing.token) is listing


def test_an_approved_option_goes_out_as_nsefo_nrml_and_is_read_back(setup, fake, conn):
    client, adapter = setup
    inst = call(client, adapter.find_option, "NIFTY", paise(24500), OptionType.CE)
    token = adapter.master.get(inst.key).token
    call(client, _price, conn, token, 12000)  # premium ₹120.00
    reply = client.post("/api/orders/preview", json=option()).json()
    [c] = [c for c in reply["cards"] if c["type"] == "pending_order"]
    p = c["pending"]
    assert p["quantity"] == 75 and p["product"] == "NRML"
    r = client.post(f"/api/approvals/{p['id']}/approve", json={"order_hash": p["order_hash"]})
    assert r.status_code == 200, r.text
    row = fake.orders[max(fake.orders)]
    assert (row["exchange"], row["token"], row["product"], row["qtyRemaining"]) == ("NSEFO", token, "NRML", 75)
    [order] = [o for o in call(client, adapter.get_orders) if o.instrument.is_option]  # no longer skipped
    assert order.instrument.key == inst.key and order.product is Product.NRML


def test_an_option_position_is_read_and_only_that_much_can_be_sold(setup, fake, conn):
    client, adapter = setup
    inst = call(client, adapter.find_option, "NIFTY", paise(24500), OptionType.CE)
    token = adapter.master.get(inst.key).token
    call(client, _price, conn, token, 12500)
    fake.positions = [{"exchange": "NSEFO", "token": token, "product": "NRML", "netQuantity": 75, "netPrice": 12000,
                       "prevClose": 11800}]
    [pos] = [x for x in call(client, adapter.get_positions) if x.instrument.is_option]
    assert (pos.quantity, pos.product, pos.avg_price, pos.ltp) == (75, Product.NRML, 12000, 12500)

    sell = client.post("/api/orders/preview", json=option(side="SELL")).json()
    assert sell["cards"][0]["type"] == "pending_order"
    writing = client.post("/api/orders/preview", json=option(side="SELL", lots=2)).json()
    assert writing["cards"][0]["level"] == "blocked" and "writing" in writing["text"]
