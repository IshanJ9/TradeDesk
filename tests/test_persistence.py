"""Cards and plans waiting for approval survive a restart; a plan that was running is halted, never resumed."""

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.broker.mock import MockBroker
from app.config import Settings
from app.main import create_app
from app.schemas import PlanState, paise

NOW = datetime(2026, 10, 8, 5, 0, tzinfo=timezone.utc)
CARD = dict(action="PLACE", instrument_ref="INFY", side="BUY", quantity=2, order_type="LIMIT", limit_price=paise(1450))
PLAN = {"legs": [{"instrument": "infosys", "side": "SELL", "quantity": 2}, {"instrument": "itc", "side": "BUY", "quantity": 3}]}


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


def app_on(tmp_path, clock, broker=None):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'td.db'}", ticker_interval=None, reconcile_interval=None,
                        account_push_interval=3600, external_sync_interval=None)
    broker = broker or MockBroker(clock=clock)
    return TestClient(create_app(settings, broker=broker, clock=clock)), broker


def test_a_waiting_card_survives_a_restart_and_can_still_be_approved_exactly_once(tmp_path):
    clock = Clock()
    first, _ = app_on(tmp_path, clock)
    with first:
        card = first.post("/api/orders/preview", json=CARD).json()["cards"][0]["pending"]

    second, broker = app_on(tmp_path, clock)
    with second:
        waiting = second.get("/api/pending").json()["orders"]
        assert [(p["id"], p["order_hash"]) for p in waiting] == [(card["id"], card["order_hash"])]  # same card, same hash
        r = second.post(f"/api/approvals/{card['id']}/approve", json={"order_hash": card["order_hash"]})
        assert r.status_code == 200 and r.json()["outcome"] == "SENT"
        assert len(broker._orders) == 1

    third, broker3 = app_on(tmp_path, clock)
    with third:
        assert third.get("/api/pending").json()["orders"] == []
        again = third.post(f"/api/approvals/{card['id']}/approve", json={"order_hash": card["order_hash"]})
        assert again.status_code == 409 and again.json()["code"] == "NOT_PENDING"  # its sent state was saved too
        assert broker3._orders == {}


def test_a_card_that_expired_while_the_app_was_down_is_still_refused(tmp_path):
    clock = Clock()
    first, _ = app_on(tmp_path, clock)
    with first:
        card = first.post("/api/orders/preview", json=CARD).json()["cards"][0]["pending"]
    clock.now += timedelta(minutes=5)
    second, broker = app_on(tmp_path, clock)
    with second:
        r = second.post(f"/api/approvals/{card['id']}/approve", json={"order_hash": card["order_hash"]})
        assert r.status_code == 409 and r.json()["code"] == "EXPIRED"
        assert broker._orders == {}


def test_a_waiting_plan_survives_a_restart_and_still_runs(tmp_path):
    clock = Clock()
    first, _ = app_on(tmp_path, clock)
    with first:
        plan = first.post("/api/plans/preview", json=PLAN).json()["cards"][0]["plan"]

    second, broker = app_on(tmp_path, clock)
    with second:
        waiting = second.get("/api/pending").json()["plans"]
        assert [(p["id"], p["plan_hash"]) for p in waiting] == [(plan["id"], plan["plan_hash"])]
        r = second.post(f"/api/plans/{plan['id']}/approve", json={"plan_hash": plan["plan_hash"]})
        assert r.status_code == 200
        second.portal.call(second.app.state.plans.join, plan["id"])
        assert len(broker._orders) == 2


def test_a_plan_that_was_running_when_the_app_stopped_is_halted_not_resumed(tmp_path):
    clock = Clock()
    first, _ = app_on(tmp_path, clock)
    with first:
        plan = first.post("/api/plans/preview", json=PLAN).json()["cards"][0]["plan"]
        store = first.app.state.plan_store
        store.put(store.get(plan["id"]).model_copy(update={"state": PlanState.RUNNING}))  # the app stops mid-run

    second, broker = app_on(tmp_path, clock)
    with second:
        state = second.app.state.plan_store.get(plan["id"]).state
        report = second.app.state.plan_store.report(plan["id"])
        assert state is PlanState.HALTED and report.state is PlanState.HALTED
        assert "The app restarted while this plan was running" in report.summary
        assert broker._orders == {}  # nothing more was sent
        assert second.get("/api/pending").json()["plans"] == []
