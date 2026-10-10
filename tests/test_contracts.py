"""The shared hooks the parallel workstreams build on: the risk guard, the trace events and the activity store."""

from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.api_models import TraceEvent
from app.broker.mock import MockBroker
from app.config import Settings
from app.events import EventHub
from app.history.store import DaySummary, InMemoryActivityStore, trading_day
from app.main import create_app
from app.risk.guard import RiskVerdict
from app.schemas import Exchange, Instrument, Order, OrderStatus, OrderType, Side
from app.trace import DETAIL_LIMIT, Tracer

NOW = datetime(2026, 10, 8, 5, 0, tzinfo=timezone.utc)  # 10:30 in India: market open


class FakeGuard:
    def __init__(self, preview=RiskVerdict(), approve=RiskVerdict()):
        self.verdicts = {"preview": preview, "approve": approve}
        self.stages: list[str] = []

    async def check(self, pending, stage):
        self.stages.append(stage)
        return self.verdicts[stage]


@pytest.fixture
def broker():
    return MockBroker(clock=lambda: NOW)


@pytest.fixture
def client(broker):
    settings = Settings(ticker_interval=None, account_push_interval=3600, reconcile_interval=None)
    with TestClient(create_app(settings, broker=broker, clock=lambda: NOW)) as c:
        yield c


def use_guard(client, guard):
    client.app.state.cards._risk = guard
    client.app.state.approvals._risk = guard


def preview(client, qty=2):
    return client.post(
        "/api/orders/preview",
        json=dict(action="PLACE", instrument_ref="INFY", side="BUY", quantity=qty, order_type="LIMIT", limit_price=145000),
    ).json()


# ---- risk guard hook ------------------------------------------------------------- #


def test_default_guard_adds_nothing(client):
    card = preview(client)["cards"][0]
    assert card["type"] == "pending_order"
    assert not any("limit" in w.lower() and "today" in w.lower() for w in card["pending"]["warnings"])


def test_guard_warnings_go_on_the_card_first(client):
    use_guard(client, FakeGuard(preview=RiskVerdict(warnings=("6th order today; your limit is 5",))))
    pending = preview(client)["cards"][0]["pending"]
    assert pending["warnings"][0] == "6th order today; your limit is 5"


def test_guard_block_at_preview_makes_no_card(client):
    use_guard(client, FakeGuard(preview=RiskVerdict(block="You set a limit of 5 orders a day.")))
    reply = preview(client)
    assert reply["text"] == "You set a limit of 5 orders a day."
    assert reply["cards"][0]["type"] == "notice"
    assert client.get("/api/pending").json()["orders"] == []


def test_guard_block_at_approve_sends_nothing(client, broker):
    pending = preview(client)["cards"][0]["pending"]
    guard = FakeGuard(approve=RiskVerdict(block="You set a limit of 5 orders a day."))
    use_guard(client, guard)
    r = client.post(f"/api/approvals/{pending['id']}/approve", json={"order_hash": pending["order_hash"]})
    assert r.status_code == 409
    assert r.json()["code"] == "BLOCKED"
    assert "5 orders a day" in r.json()["message"]
    assert guard.stages == ["approve"]
    assert client.get("/api/orders").json() == []


def test_guard_is_asked_at_both_stages(client):
    guard = FakeGuard()
    use_guard(client, guard)
    pending = preview(client)["cards"][0]["pending"]
    client.post(f"/api/approvals/{pending['id']}/approve", json={"order_hash": pending["order_hash"]})
    assert guard.stages == ["preview", "approve"]


# ---- trace events ---------------------------------------------------------------- #


def drain(queue):
    out = []
    while not queue.empty():
        out.append(queue.get_nowait())
    return out


def test_tracer_reports_start_and_end_with_time():
    hub = EventHub().for_user("u1")
    q = hub.subscribe()
    tracer = Tracer(hub, run_id="r1")
    with tracer.step("router"):
        pass
    events = drain(q)
    assert [(e.node, e.status) for e in events] == [("router", "start"), ("router", "end")]
    assert all(isinstance(e, TraceEvent) and e.run_id == "r1" for e in events)
    assert events[1].ms is not None


def test_tracer_reports_errors_and_re_raises():
    hub = EventHub().for_user("u1")
    q = hub.subscribe()
    with pytest.raises(ValueError):
        with Tracer(hub).step("tool:get_quote", "tool"):
            raise ValueError("boom")
    assert [e.status for e in drain(q)] == ["start", "error"]


def test_tracer_keeps_detail_short():
    hub = EventHub().for_user("u1")
    q = hub.subscribe()
    Tracer(hub).emit("output_guard", "guard", "blocked", "x" * 500)
    assert len(drain(q)[0].detail) == DETAIL_LIMIT


def test_new_events_are_in_the_contract(client):
    names = client.get("/openapi.json").json()["components"]["schemas"]
    assert {"TraceEvent", "ExternalOrderEvent", "DisciplineUpdateEvent", "DisciplineSummary"} <= names.keys()


# ---- activity store -------------------------------------------------------------- #


def order(oid, at, status=OrderStatus.OPEN):
    return Order(
        order_id=oid,
        instrument=Instrument(symbol="INFY", exchange=Exchange.NSE),
        side=Side.BUY,
        quantity=1,
        order_type=OrderType.LIMIT,
        limit_price=145000,
        status=status,
        created_at=at,
        updated_at=at,
    )


def test_days_follow_indian_dates():
    assert trading_day(datetime(2026, 10, 8, 20, 0, tzinfo=timezone.utc)) == date(2026, 10, 9)


def test_store_updates_an_order_but_keeps_where_it_came_from():
    store = InMemoryActivityStore()
    store.record_order(order("1", NOW), "external")
    store.record_order(order("1", NOW, OrderStatus.FILLED), "app")
    [rec] = store.orders_on(date(2026, 10, 8))
    assert rec.source == "external"
    assert rec.order.status is OrderStatus.FILLED


def test_store_lists_days_newest_first():
    store = InMemoryActivityStore()
    for n in range(5):
        store.save_day(DaySummary(day=date(2026, 10, 1) + timedelta(days=n), orders=n, turnover=0, pnl=0, charges=0))
    assert [d.orders for d in store.days(limit=3)] == [4, 3, 2]
