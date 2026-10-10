"""Store by store: data written by one user is invisible to, and untouchable by, another user of the same database.
Each test names the store it protects, so a failing test says which `user_id` filter went missing."""

from datetime import date, datetime, timedelta, timezone

import pytest
from conftest import SignedInClient
from test_risk_analytics import FACTS

from app.audit import AuditLog
from app.config import Settings
from app.db import Database
from app.history.sqlite_store import SqliteActivityStore
from app.history.store import DaySummary
from app.main import create_app
from app.risk.models import Goal
from app.risk.presets import preset
from app.risk.report import demo_days
from app.risk.report_store import ReportStore
from app.risk.store import ProfileStore
from app.rules.store import RuleStore
from app.schemas import (AuditKind, Comparator, Exchange, Instrument, Order, OrderStatus, OrderType, Rule, RuleCondition,
                         RuleKind, RuleStatus, Side)

NOW = datetime(2026, 10, 9, 5, tzinfo=timezone.utc)
A, B = "user-a", "user-b"


@pytest.fixture
def db():
    d = Database()
    yield d
    d.close()


def an_order(oid="ORD-1", **over):
    values = dict(order_id=oid, instrument=Instrument(symbol="INFY", exchange=Exchange.NSE), side=Side.BUY, quantity=10,
                  order_type=OrderType.LIMIT, limit_price=145005, status=OrderStatus.OPEN, created_at=NOW, updated_at=NOW)
    return Order(**(values | over))


def an_alert(rid="r1"):
    return Rule(id=rid, kind=RuleKind.ALERT, created_at=NOW,
                condition=RuleCondition(instrument_key="NSE:TCS", comparator=Comparator.BELOW, absolute_price=380000))


# ---- audit_events ------------------------------------------------------------------------------------------------- #


def test_audit_log_lists_and_exports_only_the_users_own_events(db):
    a, b = AuditLog(db, lambda: NOW, user_id=A), AuditLog(db, lambda: NOW, user_id=B)
    a.record(AuditKind.USER_MESSAGE, "user", "a's message")
    b.record(AuditKind.USER_MESSAGE, "user", "b's message")
    assert [e.summary for e in a.list()] == ["a's message"]
    assert [e.summary for e in b.list(kind=AuditKind.USER_MESSAGE)] == ["b's message"]
    assert "b's message" not in a.export_jsonl() and "a's message" not in b.export_jsonl()
    assert {r["user_id"] for r in db.query("SELECT user_id FROM audit_events")} == {A, B}


# ---- rules ------------------------------------------------------------------------------------------------------- #


def test_rule_store_cannot_read_cancel_fire_or_mark_another_users_rule(db):
    a, b = RuleStore(db, user_id=A), RuleStore(db, user_id=B)
    a.add(an_alert("r1"))
    assert b.get("r1") is None and b.list() == [] and b.active_count() == 0 and b.active_for("NSE:TCS") == []
    assert b.cancel("r1") is None
    assert b.mark_fired("r1", NOW) is None
    b.mark_delivered("r1")
    assert a.get("r1").status is RuleStatus.ACTIVE  # untouched by all of the above
    assert a.mark_fired("r1", NOW) is not None
    assert b.undelivered_fired() == [] and len(a.undelivered_fired()) == 1
    b.mark_delivered("r1")
    assert len(a.undelivered_fired()) == 1  # B's "delivered" did not mark A's rule delivered


# ---- activity_orders / activity_days ------------------------------------------------------------------------------ #


def test_activity_store_keeps_two_users_with_the_same_broker_order_id_apart(db):
    a, b = SqliteActivityStore(db, user_id=A), SqliteActivityStore(db, user_id=B)
    a.record_order(an_order("ORD-1", quantity=10), "app")
    b.record_order(an_order("ORD-1", quantity=99), "external")  # 021 and the mock both count orders from 1
    (ra,), (rb,) = a.orders_on(NOW.date()), b.orders_on(NOW.date())
    assert (ra.order.quantity, ra.source) == (10, "app") and (rb.order.quantity, rb.source) == (99, "external")
    a.save_day(DaySummary(day=NOW.date(), orders=3, turnover=100, pnl=5, charges=1))
    assert [d.orders for d in a.days()] == [3] and b.days() == []
    b.save_day(DaySummary(day=NOW.date(), orders=7, turnover=100, pnl=5, charges=1))
    assert [d.orders for d in a.days()] == [3] and [d.orders for d in b.days()] == [7]


def test_an_orders_attribution_uses_only_that_users_send_log(db):
    a, b = SqliteActivityStore(db, user_id=A), SqliteActivityStore(db, user_id=B)
    db.execute("INSERT INTO executions(client_order_id,user_id,pending_id,action,status,broker_order_id,created_at,updated_at,detail)"
               " VALUES ('c1',?,'p','PLACE','SENT','ORD-1',?,?,'{}')", (A, NOW.isoformat(), NOW.isoformat()))
    a.record_order(an_order("ORD-1"), "external")
    b.record_order(an_order("ORD-1"), "external")  # B's ORD-1 is a different order that merely has the same number
    assert a.orders_on(NOW.date())[0].source == "app"
    assert b.orders_on(NOW.date())[0].source == "external"


# ---- risk_profile / risk_goal / risk_cooldown ---------------------------------------------------------------------- #


def test_profile_goal_and_cooldown_are_per_user(db):
    a, b = ProfileStore(db, user_id=A), ProfileStore(db, user_id=B)
    mine = preset("aggressive")
    a.save_profile(mine)
    goal = Goal(target_pct=10, start_date=date(2026, 10, 10), end_date=date(2026, 12, 1), start_value=1_000_000,
                max_acceptable_loss_paise=100_000)
    a.save_goal(goal)
    assert b.get_profile() is None and b.get_goal() is None
    b.save_profile(preset("conservative"))
    assert a.get_profile() == mine and b.get_profile() == preset("conservative")
    b.delete_goal()
    assert a.get_goal() == goal
    # a cooling-off pause latched for A is not B's
    hard = mine.model_copy(update={"hard_cooling_off": True, "cooling_off_after_losses": 1, "cooling_off_minutes": 30})
    a.save_profile(hard)
    b.save_profile(hard)
    facts = FACTS.model_copy(update={"consecutive_losses": 1, "last_loss_at": NOW - timedelta(minutes=1)})
    assert a.cooldown_until(hard, facts, NOW) is not None
    calm = FACTS.model_copy(update={"consecutive_losses": 0, "last_loss_at": None})
    assert b.cooldown_until(hard, calm, NOW) is None
    assert a.cooldown_until(hard, calm, NOW) is not None  # still latched for A
    b.save_profile(preset("balanced"))  # switching B's stop off clears B's pause only
    assert a.cooldown_until(hard, calm, NOW) is not None


# ---- risk_days / risk_meta / risk_observations_v2 ------------------------------------------------------------------- #


def test_discipline_report_store_is_per_user(db):
    a, b = ReportStore(db, user_id=A), ReportStore(db, user_id=B)
    days = demo_days(date(2026, 10, 9))
    a.save_day(days[0])
    assert [d.day for d in a.days()] == [days[0].day] and b.days() == []
    assert not a.seed_attempted() and not b.seed_attempted()
    a.mark_seed_attempted()
    assert a.seed_attempted() and not b.seed_attempted()
    b.save_day(days[1])
    b.clear_demo()  # B clearing the demo history leaves A's rows alone
    assert [d.day for d in a.days()] == [days[0].day]
    first = a.observe(FACTS, 40, NOW)
    other = b.observe(FACTS.model_copy(update={"portfolio_value": 123}), 90, NOW + timedelta(minutes=10))
    assert first["risk_samples"] == 1 and other["risk_samples"] == 1
    assert first["opening_portfolio_paise"] == FACTS.portfolio_value and other["opening_portfolio_paise"] == 123
    assert a.covered(NOW, NOW, source="unknown") is True and b.covered(NOW, NOW, source="unknown") is False


# ---- across a restart: what each user's desk loads back ------------------------------------------------------------- #


def test_after_a_restart_each_desk_loads_back_only_its_own_cards_rules_plans_and_settings(tmp_path):
    url = f"sqlite:///{(tmp_path / 'two.db').as_posix()}"
    settings = Settings(database_url=url, ticker_interval=None, reconcile_interval=None, external_sync_interval=None)
    intent = dict(action="PLACE", instrument_ref="INFY", side="BUY", quantity=10, order_type="LIMIT", limit_price=140000)
    rule = dict(kind="ALERT", instrument="tcs", comparator="BELOW", price_rupees=3800)
    plan = dict(legs=[dict(instrument="infosys", side="SELL", fraction_of_holding=0.5), dict(instrument="itc", side="BUY", proceeds_of_leg=0)])
    made = {}
    with SignedInClient(create_app(settings), email="a@example.com") as a:
        b = SignedInClient(a.app, email="b@example.com")
        b.portal = a.portal
        for name, c in (("a", a), ("b", b)):
            c.sign_in()
            made[name] = dict(
                card=c.post("/api/orders/preview", json=intent).json()["cards"][0]["pending"]["id"],
                rule=c.post("/api/rules", json=rule).json()["cards"][0]["rule"]["id"],
                plan=c.post("/api/plans/preview", json=plan).json()["cards"][0]["plan"]["id"],
            )
            c.put("/api/profile", json=preset("aggressive" if name == "a" else "conservative").model_dump(mode="json"))
            c.post("/api/chat", json={"message": f"hello from {name}"})
    assert made["a"]["card"] != made["b"]["card"]
    with SignedInClient(create_app(settings), email="a@example.com") as a:  # the app starts again on the same file
        b = SignedInClient(a.app, email="b@example.com")
        b.portal = a.portal
        for name, c in (("a", a), ("b", b)):
            other = "b" if name == "a" else "a"
            c.sign_in()
            pending = c.get("/api/pending").json()
            assert [p["id"] for p in pending["orders"]] == [made[name]["card"]]
            assert [p["id"] for p in pending["plans"]] == [made[name]["plan"]]
            assert [r["id"] for r in c.get("/api/rules").json()] == [made[name]["rule"]]
            assert c.get("/api/profile").json()["style"] == ("aggressive" if name == "a" else "conservative")
            texts = " ".join(e["summary"] for e in c.get("/api/audit").json())
            assert f"hello from {name}" in texts or f"hello from {name}" in str(c.get("/api/audit").json())
            assert f"hello from {other}" not in str(c.get("/api/audit").json())
            assert c.get(f"/api/plans/{made[other]['plan']}/report").status_code == 404
            assert c.delete(f"/api/rules/{made[other]['rule']}").status_code == 404


# ---- the send log: one executor, one user's rows ----------------------------------------------------------------------- #


def test_an_executor_only_sees_and_reconciles_its_own_users_unresolved_sends(tmp_path):
    settings = Settings(ticker_interval=None, reconcile_interval=None, external_sync_interval=None)
    app = create_app(settings)
    with SignedInClient(app, email="a@example.com") as a:
        b = SignedInClient(app, email="b@example.com")
        b.portal = a.portal
        a.sign_in()
        b.sign_in()
        ids = {name: c.get("/api/auth/me").json()["user"]["id"] for name, c in (("a", a), ("b", b))}
        for name, cid in (("a", "c-a"), ("b", "c-b")):
            app.state.db.execute(
                "INSERT INTO executions(client_order_id,user_id,pending_id,action,status,created_at,updated_at,detail)"
                " VALUES (?,?,'p','PLACE','UNKNOWN',?,?,'{}')", (cid, ids[name], NOW.isoformat(), NOW.isoformat()))
        ex_a = app.state.workspaces.peek(ids["a"]).executor
        ex_b = app.state.workspaces.peek(ids["b"]).executor
        assert [r["client_order_id"] for r in ex_a.unresolved()] == ["c-a"]
        assert [r["client_order_id"] for r in ex_b.unresolved()] == ["c-b"]
        assert a.post("/api/executions/reconcile").json()["unresolved"] == 1  # A is told about A's one, not both


# ---- guards that two users with the same ids would otherwise trip over ------------------------------------------------ #


def test_one_user_cannot_overwrite_another_users_stored_card_even_with_the_same_card_id(tmp_path):
    app = create_app(Settings(ticker_interval=None, reconcile_interval=None, external_sync_interval=None))
    with SignedInClient(app, email="a@example.com") as a:
        a.sign_in()
        p = a.post("/api/orders/preview", json=dict(action="PLACE", instrument_ref="INFY", side="BUY", quantity=10,
                                                    order_type="LIMIT", limit_price=140000)).json()["cards"][0]["pending"]
        uid = a.get("/api/auth/me").json()["user"]["id"]
        db = app.state.db
        from app.pending import PendingStore
        from app.schemas import PendingState

        stolen = app.state.workspaces.peek(uid).pending.get(p["id"]).transition(PendingState.REJECTED)
        PendingStore(db, user_id="user-b").put(stolen)  # B writes a card that happens to have A's id
        assert PendingStore(db, user_id=uid).get(p["id"]).state is PendingState.PENDING  # A's stored card is unchanged
        assert [r["user_id"] for r in db.query("SELECT user_id FROM pending_cards WHERE id = ?", (p["id"],))] == [uid]


def test_a_users_reconcile_is_not_confused_by_another_users_order_with_the_same_number():
    """Both desks' first order is 'MO-…1'. B's timed-out send must still be found in B's own order book."""
    app = create_app(Settings(ticker_interval=None, reconcile_interval=None, external_sync_interval=None, timeout_reconcile_delay=0))
    with SignedInClient(app, email="a@example.com") as a:
        b = SignedInClient(app, email="b@example.com")
        b.portal = a.portal
        a.sign_in()
        b.sign_in()
        intent = dict(action="PLACE", instrument_ref="INFY", side="BUY", quantity=10, order_type="MARKET")
        pa = a.post("/api/orders/preview", json=intent).json()["cards"][0]["pending"]
        assert a.post(f"/api/approvals/{pa['id']}/approve", json={"order_hash": pa["order_hash"]}).json()["outcome"] == "SENT"
        b_ws = app.state.workspaces.peek(b.get("/api/auth/me").json()["user"]["id"])
        b_ws.broker.timeout_next_place(accepted=True)  # 021 received it; the reply was lost
        pb = b.post("/api/orders/preview", json=intent).json()["cards"][0]["pending"]
        result = b.post(f"/api/approvals/{pb['id']}/approve", json={"order_hash": pb["order_hash"]}).json()
        a.portal.call(b_ws.executor.reconcile)
        rows = app.state.db.query("SELECT status, broker_order_id FROM executions WHERE client_order_id = ?", (pb["client_order_id"],))
        a_order = app.state.db.query("SELECT broker_order_id FROM executions WHERE client_order_id = ?", (pa["client_order_id"],))[0]["broker_order_id"]
        assert rows[0]["status"] == "SENT" and rows[0]["broker_order_id"] == a_order, (result, dict(rows[0]), a_order)


def test_external_orders_are_not_hidden_by_another_users_order_with_the_same_number():
    from app.schemas import OrderIntent, PendingState
    from app.sync.external import ExternalOrderSync

    app = create_app(Settings(ticker_interval=None, reconcile_interval=None, external_sync_interval=None))
    with SignedInClient(app, email="a@example.com") as a:
        b = SignedInClient(app, email="b@example.com")
        b.portal = a.portal
        a.sign_in()
        b.sign_in()
        intent = dict(action="PLACE", instrument_ref="INFY", side="BUY", quantity=10, order_type="MARKET")
        pa = a.post("/api/orders/preview", json=intent).json()["cards"][0]["pending"]
        a.post(f"/api/approvals/{pa['id']}/approve", json={"order_hash": pa["order_hash"]})  # A's send: order number N
        b_ws = app.state.workspaces.peek(b.get("/api/auth/me").json()["user"]["id"])

        async def outside_order_then_sync():  # as if B placed it in 021's own app: it is also order number N, in B's book
            pending = await b_ws.builder.build(OrderIntent(**{**intent, "order_type": "LIMIT", "limit_price": 140000}))
            await b_ws.broker.place_order(pending.transition(PendingState.APPROVED))
            sync = ExternalOrderSync(b_ws.broker, app.state.db, b_ws.history, b_ws.hub, app.state.clock, 0, user_id=b_ws.user_id)
            await sync.poll()

        a.portal.call(outside_order_then_sync)
        assert len(b.get("/api/activity/external").json()["orders"]) == 1
        assert a.get("/api/activity/external").json()["orders"] == []
