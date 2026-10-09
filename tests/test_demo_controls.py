"""Demo controls exist only in demo mode on the mock broker, and they change the fake market, not the rules."""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.broker.mock import MockBroker
from app.config import Settings
from app.main import create_app
from app.schemas import paise

NOW = datetime(2026, 10, 8, 5, 0, tzinfo=timezone.utc)
ROUTES = [("post", "/api/demo/price-jump", {"symbol": "INFY", "percent": 2}), ("post", "/api/demo/timeout-next", {"accepted": True}),
          ("post", "/api/demo/network", {"on": True}), ("post", "/api/demo/poison", None), ("post", "/api/demo/losers", None),
          ("post", "/api/demo/anchor", {"on": True}), ("post", "/api/demo/external-order", None), ("get", "/api/demo/status", None)]


def client_for(demo_mode: bool, broker=None):
    settings = Settings(ticker_interval=None, reconcile_interval=None, account_push_interval=3600, demo_mode=demo_mode)
    return TestClient(create_app(settings, broker=broker or MockBroker(clock=lambda: NOW), clock=lambda: NOW))


@pytest.mark.parametrize("method,path,body", ROUTES)
def test_every_switch_is_absent_without_demo_mode(method, path, body):
    with client_for(False) as c:
        assert getattr(c, method)(path, **({"json": body} if body else {})).status_code == 404


def test_price_jump_moves_the_price_and_a_shown_card_must_be_re_quoted():
    broker = MockBroker(clock=lambda: NOW)
    with client_for(True, broker) as c:
        card = c.post("/api/orders/preview", json=dict(action="PLACE", instrument_ref="INFY", side="BUY", quantity=1,
                                                       order_type="LIMIT", limit_price=paise(1500))).json()["cards"][0]["pending"]
        before = broker._prices["NSE:INFY"]
        assert c.post("/api/demo/price-jump", json={"symbol": "INFY", "percent": 2}).status_code == 200
        assert broker._prices["NSE:INFY"] > before
        r = c.post(f"/api/approvals/{card['id']}/approve", json={"order_hash": card["order_hash"]})
        assert r.status_code == 409 and r.json()["code"] == "REQUOTE_REQUIRED"
        assert broker._orders == {}


def test_losers_and_poison_and_anchor():
    broker = MockBroker(clock=lambda: NOW)
    with client_for(True, broker) as c:
        c.post("/api/demo/losers")
        losing = [p for p in c.get("/api/account").json()["positions"] if p["pnl"] < 0]
        assert {p["instrument"]["symbol"] for p in losing} == {"INFY", "ZOMATO"}
        c.post("/api/demo/poison")
        assert "NSE:EVILCORP" in broker._instruments
        assert c.post("/api/demo/anchor", json={"on": True}).json()["anchor_active"] is True
        reply = c.post("/api/orders/preview", json=dict(action="PLACE", instrument_ref="ITC", side="BUY", quantity=1, order_type="MARKET")).json()
        assert reply["cards"][0]["type"] == "notice"  # Anchor on: no card


def test_timeout_next_is_one_shot_and_shows_in_status():
    with client_for(True) as c:
        assert c.post("/api/demo/timeout-next", json={"accepted": True}).json()["timeout_armed"] is True


def test_an_order_from_021s_app_is_placed_outside_our_send_log():
    broker = MockBroker(clock=lambda: NOW)
    with client_for(True, broker) as c:
        assert c.post("/api/demo/external-order").status_code == 200
        assert [o.quantity for o in broker._orders.values()] == [2]
        assert c.app.state.db.query("SELECT * FROM executions") == []  # not sent by TradeDesk
