"""With the default settings, TradeDesk never opens futures or writes options: the card is refused in code, nothing is
sent, and closing or buying what is allowed still works. ALLOW_UNLIMITED_RISK_FO=true is the only way to turn it on."""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.broker.mock import MockBroker
from app.config import Settings
from app.main import create_app
from app.schemas import paise

T0 = datetime(2026, 10, 10, 5, 0, tzinfo=timezone.utc)
REFUSAL = "does not open futures or sell options you don't hold"


@pytest.fixture
def broker():
    return MockBroker(clock=lambda: T0)


@pytest.fixture
def client(broker):
    settings = Settings(ticker_interval=None, reconcile_interval=None, timeout_reconcile_delay=0, account_push_interval=3600)
    assert settings.allow_unlimited_risk_fo is False
    with TestClient(create_app(settings, broker=broker, clock=lambda: T0)) as c:
        yield c


def preview(client, body):
    r = client.post("/api/orders/preview", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def option(side, kind="CE"):
    return dict(action="PLACE", option={"underlying": "NIFTY", "strike": paise(24500), "option_type": kind},
                side=side, lots=1, order_type="MARKET")


def future(side):
    return dict(action="PLACE", future={"underlying": "NIFTY"}, side=side, lots=1, order_type="MARKET")


def test_default_is_off_and_env_switch_parses(monkeypatch):
    monkeypatch.delenv("ALLOW_UNLIMITED_RISK_FO", raising=False)
    assert Settings.from_env().allow_unlimited_risk_fo is False
    for value, expected in (("true", True), ("1", True), ("no", False), ("", False)):
        monkeypatch.setenv("ALLOW_UNLIMITED_RISK_FO", value)
        assert Settings.from_env().allow_unlimited_risk_fo is expected


@pytest.mark.parametrize("kind", ["CE", "PE"])
def test_writing_an_option_is_refused(client, kind):
    reply = preview(client, option("SELL", kind))
    assert not any(c["type"] == "pending_order" for c in reply["cards"]), reply
    assert REFUSAL in reply["text"]


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_opening_a_future_is_refused_both_ways(client, side):
    reply = preview(client, future(side))
    assert not any(c["type"] == "pending_order" for c in reply["cards"]), reply
    assert REFUSAL in reply["text"]


def test_buying_an_option_still_works(client):
    reply = preview(client, option("BUY"))
    assert reply["cards"][0]["type"] == "pending_order"
