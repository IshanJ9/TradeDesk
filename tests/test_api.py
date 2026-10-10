import asyncio
import functools
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.api_models import AccountUpdateEvent, PendingUpdatedEvent, TickEvent
from app.broker.mock import MockBroker
from app.config import Settings
from app.events import EventHub
from app.main import create_app
from app.schemas import (
    Exchange,
    Instrument,
    OrderAction,
    OrderType,
    PendingOrder,
    PendingState,
    Side,
    Tick,
    paise,
)

NOW = datetime(2026, 10, 8, 10, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


def make_pending(pid="p1", **over):
    fields = dict(
        id=pid,
        action=OrderAction.PLACE,
        instrument=Instrument(symbol="INFY", exchange=Exchange.NSE, name="Infosys Ltd"),
        side=Side.BUY,
        quantity=10,
        order_type=OrderType.LIMIT,
        limit_price=paise(1450),
        client_order_id=f"c-{pid}",
        ref_ltp=paise(1448),
        created_at=NOW,
        expires_at=NOW + timedelta(seconds=60),
    )
    fields.update(over)
    return PendingOrder(**fields)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def broker():
    return MockBroker(clock=lambda: NOW)


@pytest.fixture
def client(broker, clock):
    settings = Settings(ticker_interval=None, account_push_interval=3600)
    with TestClient(create_app(settings, broker=broker, clock=clock)) as c:
        yield c


def on_loop(client, fn, *args, **kwargs):
    """Run a plain function on the app's event loop thread (queues are not thread-safe)."""
    return client.portal.call(functools.partial(fn, *args, **kwargs))


# ---- REST: reads --------------------------------------------------------------- #


def test_account_matches_the_mock_and_computes_pnl_server_side(client):
    body = client.get("/api/account").json()
    by_symbol = {h["instrument"]["symbol"]: h for h in body["holdings"]}
    assert len(body["holdings"]) == 6
    assert by_symbol["TATAMOTORS"]["pnl_pct"] == -7.24
    assert by_symbol["TATAMOTORS"]["pnl"] == -71 * 100 * 10
    assert body["funds"]["available_cash"] == paise(250000)
    assert body["locks"]["anchor_active"] is False
    assert [p["instrument"]["symbol"] for p in body["positions"]] == ["RELIANCE"]


def test_orders_start_empty(client):
    assert client.get("/api/orders").json() == []


def test_pending_lists_only_cards_awaiting_approval(client):
    client.app.state.pending.put(make_pending("a"))
    client.app.state.pending.put(make_pending("b").transition(PendingState.REJECTED))
    body = client.get("/api/pending").json()
    assert [o["id"] for o in body["orders"]] == ["a"]
    assert body["plans"] == []
    assert len(body["orders"][0]["order_hash"]) == 64


def test_broker_outage_is_a_503_not_a_crash(client, broker):
    broker.network_down = True
    assert client.get("/api/account").status_code == 503


# ---- REST: approvals ------------------------------------------------------------ #


def approve(client, pid, order_hash):
    return client.post(f"/api/approvals/{pid}/approve", json={"order_hash": order_hash})


def test_unknown_approval_is_404(client):
    assert approve(client, "nope", "0" * 64).status_code == 404


def test_malformed_hash_is_rejected_before_anything_else(client):
    client.app.state.pending.put(make_pending())
    assert approve(client, "p1", "short").status_code == 422
    assert client.app.state.pending.get("p1").state is PendingState.PENDING


def test_wrong_hash_voids_the_card(client):
    p = client.app.state.pending.put(make_pending())
    r = approve(client, "p1", "0" * 64)
    assert r.status_code == 409 and r.json()["code"] == "HASH_MISMATCH"
    assert client.app.state.pending.get("p1").state is PendingState.VOID
    assert client.get("/api/pending").json()["orders"] == []
    # even the correct hash cannot revive it
    again = approve(client, "p1", p.order_hash)
    assert again.status_code == 409 and again.json()["code"] == "NOT_PENDING"


def test_stale_hash_after_the_order_changed_is_refused(client):
    """The trader saw 10 shares; the card in the store now says 100. The old approval must fail."""
    old = make_pending()
    client.app.state.pending.put(make_pending(quantity=100))
    r = approve(client, "p1", old.order_hash)
    assert r.status_code == 409 and r.json()["code"] == "HASH_MISMATCH"


def test_expired_card_is_refused_even_with_the_right_hash(client, clock):
    p = client.app.state.pending.put(make_pending())
    clock.now = NOW + timedelta(seconds=60)
    r = approve(client, "p1", p.order_hash)
    assert r.status_code == 409 and r.json()["code"] == "EXPIRED"
    assert client.app.state.pending.get("p1").state is PendingState.EXPIRED


def test_reject_then_nothing_more_can_be_done(client):
    p = client.app.state.pending.put(make_pending())
    r = client.post("/api/approvals/p1/reject")
    assert r.status_code == 200 and r.json()["state"] == "REJECTED"
    assert client.post("/api/approvals/p1/reject").status_code == 409
    assert approve(client, "p1", p.order_hash).json()["code"] == "NOT_PENDING"
    assert client.post("/api/approvals/missing/reject").status_code == 404


def test_chat_validates_its_input(client):
    assert client.post("/api/chat", json={"message": "hi"}).status_code == 200
    assert client.post("/api/chat", json={"message": ""}).status_code == 422
    assert client.post("/api/chat", json={"message": "x", "approved": True}).status_code == 422


# ---- WebSocket ------------------------------------------------------------------- #


def test_websocket_starts_with_a_snapshot(client):
    client.app.state.pending.put(make_pending("a"))
    with client.websocket_connect("/ws") as ws:
        snap = ws.receive_json()
    assert snap["type"] == "snapshot" and snap["seq"] == 0
    assert len(snap["account"]["holdings"]) == 6
    assert [o["id"] for o in snap["pending"]["orders"]] == ["a"]
    assert snap["orders"] == [] and snap["rules"] == []


def test_ticks_arrive_in_order_with_increasing_seq(client, broker):
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        on_loop(client, broker.set_price, "NSE:INFY", paise(1449))
        on_loop(client, broker.set_price, "NSE:INFY", paise(1450))
        a, b = ws.receive_json(), ws.receive_json()
    assert (a["type"], b["type"]) == ("tick", "tick")
    assert a["tick"]["ltp"] == paise(1449) and b["tick"]["ltp"] == paise(1450)
    assert b["seq"] == a["seq"] + 1
    assert b["tick"]["seq"] == a["tick"]["seq"] + 1


def test_every_client_sees_every_event(client, broker):
    with client.websocket_connect("/ws") as one, client.websocket_connect("/ws") as two:
        one.receive_json(), two.receive_json()
        on_loop(client, broker.set_price, "NSE:ITC", paise(416))
        assert one.receive_json()["tick"]["ltp"] == two.receive_json()["tick"]["ltp"] == paise(416)


def test_rejecting_a_card_pushes_pending_updated(client):
    client.app.state.pending.put(make_pending())
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        client.post("/api/approvals/p1/reject")
        event = ws.receive_json()
    assert event["type"] == "pending_updated" and event["pending"]["state"] == "REJECTED"


def test_live_account_update_follows_a_tick(broker, clock):
    settings = Settings(ticker_interval=None, account_push_interval=0)
    with TestClient(create_app(settings, broker=broker, clock=clock)) as c, c.websocket_connect("/ws") as ws:
        ws.receive_json()
        on_loop(c, broker.set_price, "NSE:TATAMOTORS", paise(900))
        assert ws.receive_json()["type"] == "tick"
        update = ws.receive_json()
    assert update["type"] == "account_update"
    tata = next(h for h in update["account"]["holdings"] if h["instrument"]["symbol"] == "TATAMOTORS")
    assert tata["ltp"] == paise(900) and tata["pnl_pct"] == round((900 - 980) * 100 / 980, 2)


def test_automatic_ticker_feeds_the_websocket(broker, clock):
    settings = Settings(ticker_interval=0.01, account_push_interval=3600)
    with TestClient(create_app(settings, broker=broker, clock=clock)) as c, c.websocket_connect("/ws") as ws:
        snapshot_seq = ws.receive_json()["seq"]
        ticks = [ws.receive_json() for _ in range(20)]
    assert all(t["type"] == "tick" and t["seq"] > snapshot_seq for t in ticks)
    assert [t["seq"] for t in ticks] == sorted(t["seq"] for t in ticks)
    last: dict[str, int] = {}
    for t in ticks:
        key, seq = t["tick"]["instrument_key"], t["tick"]["seq"]
        assert seq > last.get(key, 0)  # per-instrument sequence never goes backwards
        last[key] = seq


def test_websocket_closes_cleanly_when_the_broker_is_down(client, broker):
    broker.network_down = True
    with client.websocket_connect("/ws") as ws:
        msg = ws.receive()
    assert msg["type"] == "websocket.close" and msg["code"] == 1013


# ---- EventHub --------------------------------------------------------------------- #


def tick_event_fields():
    return dict(tick=Tick(instrument_key="NSE:INFY", ltp=100, seq=1, ts=NOW))


async def test_hub_sequences_events_and_fans_out():
    hub = EventHub().for_user("u1")
    a, b = hub.subscribe(), hub.subscribe()
    e1 = hub.publish(TickEvent, **tick_event_fields())
    e2 = hub.publish(TickEvent, **tick_event_fields())
    assert (e1.seq, e2.seq) == (1, 2)
    assert (await a.get()).seq == 1 and (await b.get()).seq == 1
    hub.unsubscribe(a)
    hub.publish(TickEvent, **tick_event_fields())
    assert a.qsize() == 1 and b.qsize() == 2  # `a` left after its first read; gets no more


async def test_hub_drops_a_client_that_cannot_keep_up():
    hub = EventHub(queue_limit=2).for_user("u1")
    slow, fast = hub.subscribe(), hub.subscribe()
    for _ in range(2):
        hub.publish(TickEvent, **tick_event_fields())
    await fast.get(), await fast.get()  # fast drains, slow does not
    hub.publish(TickEvent, **tick_event_fields())  # third event overflows `slow`
    assert slow.get_nowait() is None and slow.empty()  # backlog cleared, closing sentinel left
    assert (await fast.get()).seq == 3  # others unaffected
    hub.publish(TickEvent, **tick_event_fields())
    assert slow.empty()  # dropped subscribers receive nothing further


# ---- the contract the frontend is generated from ------------------------------------ #


def test_openapi_exposes_the_whole_contract(client):
    spec = client.get("/openapi.json").json()
    schemas = spec["components"]["schemas"]
    for name in (
        "AccountSnapshot", "PendingList", "PendingOrder", "Order", "ApprovalConflict", "ApproveRequest",
        "ChatReply", "ChatRequest", "SnapshotEvent", "TickEvent", "AccountUpdateEvent", "OrderUpdateEvent",
        "PendingCreatedEvent", "PendingUpdatedEvent", "PlanReportUpdateEvent", "RuleUpdateEvent", "RuleFiredEvent", "CreateRuleRequest",
        "LockUpdateEvent", "AuditEventMessage", "ChaosStatusEvent", "PendingOrderCard", "PlanCard",
        "RuleCard", "AmbiguityCard", "NoticeCard",
    ):
        assert name in schemas or f"{name}-Output" in schemas, name


def test_generated_types_will_not_have_spurious_optionals(client):
    spec = client.get("/openapi.json").json()["components"]["schemas"]
    pending = spec["PendingOrder"]
    assert "order_hash" in pending["required"]  # computed hash is always sent
    assert "state" in pending["required"]  # defaulted field is still required in responses
    tick_event = spec["TickEvent"]
    assert "type" in tick_event["required"]  # discriminator must be required for TS narrowing
    assert tick_event["properties"]["type"]["const"] == "tick"


def test_ws_event_union_is_discriminated(client):
    spec = client.get("/openapi.json").json()
    items = spec["paths"]["/api/ws-events"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]["items"]
    assert items["discriminator"]["propertyName"] == "type"
    assert len(items["discriminator"]["mapping"]) == 18
