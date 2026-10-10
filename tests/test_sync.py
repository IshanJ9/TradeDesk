import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import httpx
import pytest

from app.api_models import ExternalOrderEvent
from app.broker.mock import MockBroker
from app.config import Settings
from app.history.sqlite_store import SqliteActivityStore
from app.main import create_app
from app.schemas import OrderIntent, OrderStatus, PendingState
from app.sync.external import ExternalOrderSync

NOW = datetime(2026, 10, 9, 5, tzinfo=timezone.utc)
INTENT = dict(action="PLACE", instrument_ref="INFY", side="BUY", quantity=10,
              order_type="LIMIT", limit_price=140000)


@pytest.fixture
def rig():
    time = [NOW]
    clock = lambda: time[0]
    broker = MockBroker(clock=clock)
    settings = Settings(broker="mock", llm_provider="rules", ticker_interval=None,
                        reconcile_interval=None, external_sync_interval=None)
    app = create_app(settings, broker=broker, clock=clock)
    sync = ExternalOrderSync(broker, app.state.db, app.state.history, app.state.hub, clock, 120, user_id=app.state.workspaces.all()[0].user_id)
    queue = app.state.hub.subscribe()
    yield app, broker, sync, queue, time
    app.state.db.close()


def events(queue):
    found = []
    while not queue.empty():
        event = queue.get_nowait()
        if isinstance(event, ExternalOrderEvent):
            found.append(event)
    return found


async def external(app, broker, **updates):
    # Simulates the broker's own app: no TradeDesk execution-ledger entry.
    pending = await app.state.builder.build(OrderIntent(**(INTENT | updates)))
    return await broker.place_order(pending.transition(PendingState.APPROVED))


def execution(app, time, status="UNKNOWN", oid=None, age=0, cid="cid"):
    at = (time[0] - timedelta(seconds=age)).isoformat()
    app.state.db.execute(
        "INSERT INTO executions(client_order_id,user_id,pending_id,action,status,broker_order_id,created_at,updated_at,detail) "
        "VALUES (?,?,?,?,?,?,?,?,?)", (cid, app.state.workspaces.all()[0].user_id, "pending", "PLACE", status, oid, at, at, "{}"),
    )


async def test_direct_broker_order_emits_once_and_is_saved(rig):
    app, broker, sync, queue, _ = rig
    await sync.poll()
    order = await external(app, broker)
    await sync.poll()
    await sync.poll()
    assert [e.order.order_id for e in events(queue)] == [order.order_id]
    record, = app.state.history.orders_on(NOW.date())
    assert record.source == "external"
    assert record.order == order
    assert app.state.history.days() == []  # Person C owns daily summaries


async def test_preview_approve_is_ours_and_never_emits_external(rig):
    app, _, sync, queue, _ = rig
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        response = await client.post("/api/orders/preview", json=INTENT)
        card = response.json()["cards"][0]["pending"]
        response = await client.post(f"/api/approvals/{card['id']}/approve", json={"order_hash": card["order_hash"]})
        assert response.status_code == 200
        assert response.json()["outcome"] == "SENT"
    await sync.poll()
    assert events(queue) == []
    record, = app.state.history.orders_on(NOW.date())
    assert record.source == "app"


@pytest.mark.parametrize("status,oid", [("SENDING", None), ("UNKNOWN", "unmatched"), ("SENT", None), ("REJECTED", "")])
async def test_recent_unresolved_execution_defers_source(rig, status, oid):
    app, broker, sync, queue, time = rig
    order = await external(app, broker)
    execution(app, time, status, oid)
    await sync.poll()
    assert not events(queue)
    assert app.state.history.orders_on(NOW.date()) == []
    app.state.db.execute("UPDATE executions SET status='SENT', broker_order_id=?", (order.order_id,))
    await sync.poll()
    assert not events(queue)
    assert app.state.history.orders_on(NOW.date())[0].source == "app"


async def test_grace_boundary_and_known_orders_still_update(rig):
    app, broker, sync, queue, time = rig
    order = await external(app, broker)
    await sync.poll()
    events(queue)
    execution(app, time)
    other = await external(app, broker)
    broker.set_price("NSE:INFY", 140000)
    time[0] += timedelta(seconds=119.999)
    await sync.poll()
    assert [e.order.order_id for e in events(queue)] == [order.order_id]
    assert len(app.state.history.orders_on(NOW.date())) == 1
    time[0] += timedelta(milliseconds=1)
    app.state.db.execute("UPDATE executions SET status='NOT_SENT'")
    await sync.poll()
    assert [e.order.order_id for e in events(queue)] == [other.order_id]
    assert len(app.state.history.orders_on(NOW.date())) == 2


async def test_startup_records_finals_quietly_but_emits_open_orders(rig):
    app, broker, sync, queue, _ = rig
    filled = await external(app, broker, limit_price=150000)
    assert filled.status is OrderStatus.FILLED
    opened = await external(app, broker)
    await sync.poll()
    assert [e.order.order_id for e in events(queue)] == [opened.order_id]
    assert len(app.state.history.orders_on(NOW.date())) == 2
    await sync.poll()
    assert not events(queue)


async def test_deferred_startup_final_does_not_flood_after_grace(rig):
    app, broker, sync, queue, time = rig
    await external(app, broker, limit_price=150000)
    execution(app, time)
    await sync.poll()
    time[0] += timedelta(seconds=120)
    app.state.db.execute("UPDATE executions SET status='NOT_SENT'")
    await sync.poll()
    assert not events(queue)
    assert len(app.state.history.orders_on(NOW.date())) == 1


async def test_status_and_fill_changes_emit_once(rig):
    app, broker, sync, queue, _ = rig
    await sync.poll()
    broker.partial_fill_next(0.5)
    order = await external(app, broker, limit_price=150000)
    assert order.status is OrderStatus.PARTIAL
    await sync.poll()
    assert events(queue)[0].order.filled_quantity == 5
    broker.set_price("NSE:INFY", 145000)
    await sync.poll()
    final, = events(queue)
    assert final.order.status is OrderStatus.FILLED
    assert final.order.filled_quantity == 10
    await sync.poll()
    assert not events(queue)
    assert app.state.history.orders_on(NOW.date())[0].order.filled_quantity == 10


async def test_racing_polls_emit_only_once(rig):
    app, broker, sync, queue, _ = rig
    await external(app, broker)
    await asyncio.gather(sync.poll(), sync.poll(), sync.poll())
    assert len(events(queue)) == 1


@pytest.mark.parametrize("change", [{"filled_quantity": 6}, {"avg_fill_price": 144995}])
async def test_fill_changes_emit_even_when_status_is_unchanged(rig, change):
    app, broker, sync, queue, _ = rig
    broker.partial_fill_next(0.5)
    order = await external(app, broker, limit_price=150000)
    await sync.poll()
    events(queue)
    updated = order.model_copy(update=change)
    broker._orders[order.order_id] = updated
    await sync.poll()
    event, = events(queue)
    assert event.order == updated
    await sync.poll()
    assert not events(queue)


async def test_resolved_execution_does_not_hide_another_external_order(rig):
    app, broker, sync, queue, time = rig
    ours = await external(app, broker)
    execution(app, time, status="SENT", oid=ours.order_id)
    other = await external(app, broker)
    await sync.poll()
    assert [event.order.order_id for event in events(queue)] == [other.order_id]
    assert {record.order.order_id: record.source for record in app.state.history.orders_on(NOW.date())} == {
        ours.order_id: "app", other.order_id: "external",
    }


async def test_ledger_is_read_after_awaiting_broker(rig, monkeypatch):
    app, broker, sync, queue, time = rig
    order = await external(app, broker)
    original = broker.get_orders

    async def racing_get():
        execution(app, time, status="SENT", oid=order.order_id)
        return await original()

    monkeypatch.setattr(broker, "get_orders", racing_get)
    await sync.poll()
    assert not events(queue)
    assert app.state.history.orders_on(NOW.date())[0].source == "app"


async def test_restart_preserves_source_and_suppresses_old_finals(rig):
    app, broker, sync, queue, time = rig
    await external(app, broker, limit_price=150000)
    await sync.poll()
    restarted = ExternalOrderSync(broker, app.state.db, SqliteActivityStore(app.state.db, user_id=app.state.workspaces.all()[0].user_id), app.state.hub, lambda: time[0], 120, user_id=app.state.workspaces.all()[0].user_id)
    await restarted.poll()
    assert not events(queue)
    assert app.state.history.orders_on(NOW.date())[0].source == "external"


async def test_loop_survives_outage_and_cancels_cleanly(rig):
    app, broker, sync, queue, _ = rig
    await external(app, broker)
    broker.network_down = True
    task = asyncio.create_task(sync.run(0.001))
    try:
        await asyncio.sleep(0.01)
        assert not task.done()
        broker.network_down = False
        event = await asyncio.wait_for(queue.get(), 1)
        assert isinstance(event, ExternalOrderEvent)
        assert not task.done()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_loop_survives_other_errors_without_logging_raw_text(rig, monkeypatch, caplog):
    _, broker, sync, _, _ = rig
    recovered = asyncio.Event()

    async def get_orders():
        if not recovered.is_set():
            recovered.set()
            raise RuntimeError("private broker exception")
        return []

    monkeypatch.setattr(broker, "get_orders", get_orders)
    task = asyncio.create_task(sync.run(0.001))
    try:
        await asyncio.wait_for(recovered.wait(), 1)
        await asyncio.sleep(0.01)
        assert not task.done()
        assert "retrying" in caplog.text
        assert "private broker exception" not in caplog.text
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.parametrize("interval,expected", [(None, False), (0.001, True)])
async def test_lifespan_enables_and_stops_sync(interval, expected):
    broker = MockBroker(clock=lambda: NOW)
    broker.get_orders = AsyncMock(return_value=[])
    app = create_app(Settings(broker="mock", llm_provider="rules", ticker_interval=None,
                             reconcile_interval=None, external_sync_interval=interval), broker=broker)
    assert isinstance(app.state.history, SqliteActivityStore)  # an account exists, so its desk is started with the app
    async with app.router.lifespan_context(app):
        await asyncio.sleep(0.02)
        assert bool(broker.get_orders.await_count) is expected
    count = broker.get_orders.await_count
    await asyncio.sleep(0.01)
    assert broker.get_orders.await_count == count


async def test_fake021_same_login_and_partial_fills(rig):
    from app.broker.zerotwoone import ZeroTwoOneAdapter
    from fake021 import Fake021, FakeConnector

    app, _, _, queue, time = rig
    fake = Fake021()
    adapter = ZeroTwoOneAdapter(username="HACK1234", password="pw", http=fake.client(),
                               connect=FakeConnector(), clock=lambda: time[0], cache_dir=None)
    try:
        await adapter.start()
        fake._place(dict(exchange="NSE", token=1594, qty=10, price=140000, book="RL", product="CNC", validity="Day"))
        sync = ExternalOrderSync(adapter, app.state.db, app.state.history, app.state.hub, lambda: time[0], 120, user_id=app.state.workspaces.all()[0].user_id)
        await sync.poll()
        assert len(events(queue)) == 1
        fake.fill(1042, quantity=4, price=139995)
        await sync.poll()
        event, = events(queue)
        assert event.order.filled_quantity == 4
        assert event.order.avg_fill_price == 139995
        assert fake.logins == 1
        assert app.state.history.orders_on(event.order.created_at.astimezone(timezone(timedelta(hours=5, minutes=30))).date())[0].source == "external"
    finally:
        await adapter.close()


@pytest.mark.parametrize("demo", [False, True])
async def test_trace_sample_is_demo_only_and_never_calls_broker(demo):
    from app.api_models import TraceEvent
    from unittest.mock import Mock

    app = create_app(Settings(broker="mock", llm_provider="rules", demo_mode=demo,
                             ticker_interval=None, external_sync_interval=None, reconcile_interval=None))
    broker, copilot, approvals = Mock(), Mock(), Mock()
    app.state.broker, app.state.copilot, app.state.approvals = broker, copilot, approvals
    queue = app.state.hub.subscribe()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            response = await client.post("/api/dev/trace-sample")
            assert response.status_code == (200 if demo else 404)
            if demo:
                assert response.json()["demo"] is True
                run_id = response.json()["run_id"]
                trace = [queue.get_nowait() for _ in range(queue.qsize())]
                assert len(trace) == 8
                assert all(isinstance(event, TraceEvent) and event.run_id == run_id for event in trace)
                assert [event.seq for event in trace] == list(range(1, 9))
                assert trace[-1].status == "blocked"
                assert all("DEMO DATA" in event.detail for event in trace if event.status != "start")
                second = await client.post("/api/dev/trace-sample")
                assert second.json()["run_id"] != run_id
            else:
                assert queue.empty()
            assert broker.mock_calls == copilot.mock_calls == approvals.mock_calls == []
            assert app.state.db.query("SELECT * FROM executions") == []
            assert app.state.audit.list() == []
    finally:
        app.state.db.close()


async def test_unknown_past_grace_waits_for_real_reconciliation(rig):
    app, broker, sync, queue, time = rig
    order = await external(app, broker)
    execution(app, time, age=121)
    await sync.poll()
    assert not events(queue)
    assert app.state.history.orders_on(NOW.date()) == []
    time[0] += timedelta(hours=1)
    await sync.poll()
    assert app.state.history.orders_on(NOW.date()) == []
    app.state.db.execute("UPDATE executions SET status='SENT', broker_order_id=?", (order.order_id,))
    await sync.poll()
    assert app.state.history.orders_on(NOW.date())[0].source == "app"
    assert not events(queue)


async def test_late_confirmed_id_corrects_legacy_external_history(rig):
    app, broker, sync, queue, time = rig
    order = await external(app, broker)
    await sync.poll()
    assert app.state.history.orders_on(NOW.date())[0].source == "external"
    events(queue)
    execution(app, time, status="SENT", oid=order.order_id, age=121)
    broker.set_price("NSE:INFY", 140000)
    await sync.poll()
    assert app.state.history.orders_on(NOW.date())[0].source == "app"
    assert not events(queue)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        assert (await client.get("/api/activity/external")).json()["orders"] == []


async def test_external_history_endpoint_restores_quiet_startup_orders(rig):
    app, broker, sync, queue, _ = rig
    order = await external(app, broker, limit_price=150000)
    await sync.poll()
    assert not events(queue)  # completed order was intentionally not broadcast
    broker.get_orders = AsyncMock(side_effect=AssertionError("History GET must not call broker"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        for _ in range(2):  # refreshing or opening a new client gets the same saved data
            response = await client.get("/api/activity/external")
            assert response.status_code == 200
            data = response.json()
            assert data["orders"][0]["order_id"] == order.order_id
            assert data["day"] == NOW.date().isoformat()
            assert data["attribution_pending"] is False
    broker.get_orders.assert_not_awaited()


async def test_external_history_endpoint_explains_unresolved_source(rig):
    app, broker, sync, _, time = rig
    await external(app, broker)
    execution(app, time, age=999)
    await sync.poll()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        data = (await client.get("/api/activity/external")).json()
        assert data["attribution_pending"] is True
        assert data["orders"] == []
