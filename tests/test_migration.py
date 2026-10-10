"""A database written before accounts existed: its rows end up owned by the first account (or the configured owner),
nothing is lost, the primary keys are rebuilt to include the user, and running it again changes nothing."""

import sqlite3
from datetime import date, datetime, timezone

import pytest
from conftest import TEST_PASSWORD, SignedInClient
from starlette.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.history.store import DaySummary
from app.main import create_app
from app.risk.presets import preset
from app.risk.report import demo_days
from app.schema import USER_DATA_TABLES, adopt_legacy_rows, ensure_schema

NOW = datetime(2026, 10, 9, 5, tzinfo=timezone.utc)

# The tables exactly as they were before accounts (no user_id; singleton and order-id primary keys).
OLD_DDL = {
    "audit_events": "CREATE TABLE audit_events (seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE, ts TEXT NOT NULL, kind TEXT NOT NULL, actor TEXT NOT NULL, subject_id TEXT, summary TEXT NOT NULL, data TEXT NOT NULL)",
    "rules": "CREATE TABLE rules (id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL, instrument_key TEXT NOT NULL, created_at TEXT NOT NULL, fired_at TEXT, delivered INTEGER NOT NULL DEFAULT 0, data TEXT NOT NULL)",
    "executions": "CREATE TABLE executions (client_order_id TEXT PRIMARY KEY, pending_id TEXT NOT NULL, action TEXT NOT NULL, status TEXT NOT NULL, broker_order_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, detail TEXT NOT NULL)",
    "pending_cards": "CREATE TABLE pending_cards (id TEXT PRIMARY KEY, state TEXT NOT NULL, created_at TEXT NOT NULL, data TEXT NOT NULL)",
    "plans": "CREATE TABLE plans (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, data TEXT NOT NULL)",
    "plan_requests": "CREATE TABLE plan_requests (plan_id TEXT PRIMARY KEY, data TEXT NOT NULL)",
    "plan_reports": "CREATE TABLE plan_reports (plan_id TEXT PRIMARY KEY, data TEXT NOT NULL)",
    "activity_orders": "CREATE TABLE activity_orders (order_id TEXT PRIMARY KEY, source TEXT NOT NULL CHECK(source IN ('app','external')), day TEXT NOT NULL, data TEXT NOT NULL, recording_source TEXT NOT NULL DEFAULT 'unknown')",
    "activity_days": "CREATE TABLE activity_days (day TEXT PRIMARY KEY, data TEXT NOT NULL)",
    "risk_profile": "CREATE TABLE risk_profile (id INTEGER PRIMARY KEY CHECK(id = 1), data TEXT NOT NULL)",
    "risk_goal": "CREATE TABLE risk_goal (id INTEGER PRIMARY KEY CHECK(id = 1), data TEXT NOT NULL)",
    "risk_cooldown": "CREATE TABLE risk_cooldown (id INTEGER PRIMARY KEY CHECK(id = 1), until_at TEXT NOT NULL)",
    "risk_days": "CREATE TABLE risk_days (day TEXT NOT NULL, demo INTEGER NOT NULL, data TEXT NOT NULL, PRIMARY KEY(day, demo))",
    "risk_meta": "CREATE TABLE risk_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "risk_observations_v2": "CREATE TABLE risk_observations_v2 (source TEXT NOT NULL, day TEXT NOT NULL, bucket INTEGER NOT NULL, at TEXT NOT NULL, risk REAL, portfolio INTEGER NOT NULL, pnl INTEGER NOT NULL, PRIMARY KEY(source,day,bucket))",
}
PLAN = dict(legs=[dict(instrument="infosys", side="SELL", fraction_of_holding=0.5), dict(instrument="itc", side="BUY", proceeds_of_leg=0)])


def settings_for(path, **over):
    return Settings(database_url=f"sqlite:///{path.as_posix()}", ticker_interval=None, reconcile_interval=None,
                    external_sync_interval=None, **over)


def counts(path) -> dict[str, int]:
    con = sqlite3.connect(path)
    out = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in USER_DATA_TABLES}
    con.close()
    return out


def downgrade_to_the_old_shape(path) -> dict[str, int]:
    """Turn a database made by the current code into what the previous release would have left behind."""
    con = sqlite3.connect(path)
    before = {}
    for table, ddl in OLD_DDL.items():
        old_cols = [c.strip().split()[0] for c in ddl[ddl.index("(") + 1:ddl.rindex(")")].split(",") if c.strip().split()[0] not in ("PRIMARY", "CHECK")]
        new_cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        con.execute(f"ALTER TABLE {table} RENAME TO {table}__new")
        con.execute(ddl)
        shared = [c for c in old_cols if c in new_cols]
        if table.startswith("risk_") and table in ("risk_profile", "risk_goal", "risk_cooldown"):
            shared = [c for c in shared if c != "id"]
            con.execute(f"INSERT INTO {table} (id, {', '.join(shared)}) SELECT 1, {', '.join(shared)} FROM {table}__new")
        else:
            con.execute(f"INSERT INTO {table} ({', '.join(shared)}) SELECT {', '.join(shared)} FROM {table}__new")
        con.execute(f"DROP TABLE {table}__new")
        before[table] = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    con.execute("DELETE FROM sessions")
    con.execute("DELETE FROM users")
    con.commit()
    con.close()
    return before


@pytest.fixture
def legacy_db(tmp_path):
    """A database file in the old shape, holding one trader's cards, rule, plan, settings, history and audit trail."""
    path = tmp_path / "legacy.db"
    with SignedInClient(create_app(settings_for(path)), email="old@example.com") as c:
        c.post("/api/orders/preview", json=dict(action="PLACE", instrument_ref="INFY", side="BUY", quantity=10, order_type="LIMIT", limit_price=140000))
        c.post("/api/rules", json=dict(kind="ALERT", instrument="tcs", comparator="BELOW", price_rupees=3800))
        c.post("/api/plans/preview", json=PLAN)
        c.put("/api/profile", json=preset("aggressive").model_dump(mode="json"))
        c.post("/api/chat", json={"message": "hello"})
        ws = c.app.state.workspaces.all()[0]
        from test_user_scoped_stores import an_order
        ws.history.record_order(an_order("ORD-1"), "app")
        ws.history.save_day(DaySummary(day=NOW.date(), orders=1, turnover=1, pnl=1, charges=1))
        ws.discipline.reports.save_day(demo_days(date(2026, 10, 9))[0])
        ws.discipline.reports.mark_seed_attempted()
        db = ws.db
        db.execute("INSERT INTO executions(client_order_id,user_id,pending_id,action,status,created_at,updated_at,detail)"
                   " VALUES ('c1',?,'p','PLACE','SENT',?,?,'{}')", (ws.user_id, NOW.isoformat(), NOW.isoformat()))
    before = downgrade_to_the_old_shape(path)
    assert all(n > 0 for t, n in before.items() if t not in ("risk_goal", "risk_cooldown", "risk_observations_v2", "plan_reports")), before
    return path, before


def test_legacy_rows_are_unowned_and_invisible_until_the_first_account_exists(legacy_db):
    path, before = legacy_db
    db = Database(f"sqlite:///{path.as_posix()}")  # opening it upgrades the shape
    assert {t: db.query(f"SELECT COUNT(*) AS n FROM {t} WHERE user_id = ''")[0]["n"] for t in before} == before
    db.close()


def test_the_first_account_inherits_everything_and_a_second_account_gets_nothing(legacy_db):
    path, before = legacy_db
    with SignedInClient(create_app(settings_for(path)), email="first@example.com") as first:
        second = SignedInClient(first.app, email="second@example.com")
        second.portal = first.portal
        first.sign_in()
        uid = first.get("/api/auth/me").json()["user"]["id"]
        owners = {t: {r["user_id"] for r in first.app.state.db.query(f"SELECT user_id FROM {t}")} for t in USER_DATA_TABLES}
        assert all(o == {uid} for t, o in owners.items() if before.get(t)), owners
        assert counts(path) == before  # nothing lost, nothing duplicated
        assert len(first.get("/api/pending").json()["orders"]) == 1 and len(first.get("/api/rules").json()) == 1
        assert len(first.get("/api/pending").json()["plans"]) == 1
        assert first.get("/api/profile").json()["style"] == "aggressive"
        assert any(e["kind"] == "USER_MESSAGE" for e in first.get("/api/audit").json())
        second.sign_in()
        assert second.get("/api/pending").json() == {"orders": [], "plans": []}
        assert second.get("/api/rules").json() == [] and second.get("/api/profile").json() is None
        assert second.get("/api/audit").json() == []


def test_running_the_upgrade_twice_changes_nothing(legacy_db):
    path, before = legacy_db
    with SignedInClient(create_app(settings_for(path)), email="first@example.com") as c:
        c.sign_in()
        uid = c.get("/api/auth/me").json()["user"]["id"]
        db = c.app.state.db
        snapshot = {t: [tuple(r) for r in db.query(f"SELECT * FROM {t} ORDER BY 1, 2")] for t in USER_DATA_TABLES}
        ensure_schema(db)
        assert adopt_legacy_rows(db, uid) == {}  # nothing left to adopt
        assert {t: [tuple(r) for r in db.query(f"SELECT * FROM {t} ORDER BY 1, 2")] for t in USER_DATA_TABLES} == snapshot
    with SignedInClient(create_app(settings_for(path)), email="first@example.com") as again:  # and a restart
        again.sign_in()
        assert counts(path) == before and len(again.get("/api/rules").json()) == 1


def test_a_configured_owner_gets_the_data_not_whoever_registers_first(legacy_db):
    path, before = legacy_db
    with SignedInClient(create_app(settings_for(path, owner_email="boss@example.com")), email="someone@example.com") as other:
        owner = SignedInClient(other.app, email="boss@example.com")
        owner.portal = other.portal
        other.sign_in()
        assert other.get("/api/rules").json() == []  # not the owner: nothing handed over
        assert other.app.state.db.query("SELECT COUNT(*) AS n FROM rules WHERE user_id = ''")[0]["n"] == 1
        owner.sign_in()
        assert len(owner.get("/api/rules").json()) == 1 and other.get("/api/rules").json() == []


def test_the_rebuilt_primary_keys_let_two_users_share_an_order_number(tmp_path):
    db = Database(f"sqlite:///{(tmp_path / 'k.db').as_posix()}")
    for user in ("a", "b"):
        db.execute("INSERT INTO activity_orders(user_id,order_id,source,day,data) VALUES (?,?,?,?,?)", (user, "ORD-1", "app", "2026-10-09", "{}"))
        db.execute("INSERT INTO risk_profile(user_id,data) VALUES (?,?)", (user, "{}"))
    assert db.query("SELECT COUNT(*) AS n FROM activity_orders")[0]["n"] == 2
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO activity_orders(user_id,order_id,source,day,data) VALUES ('a','ORD-1','app','d','{}')")
    db.close()


def test_adopting_requires_a_real_user_id():
    db = Database()
    with pytest.raises(ValueError):
        adopt_legacy_rows(db, "")
    db.close()
