"""Two traders on one server: neither can read, change, approve or hear anything that belongs to the other.

Both are signed in to the same app at the same moment, trade the same instruments, and each has their own (mock)
broker account. One test per kind of data; the stores are also checked directly in test_user_scoped_stores.py."""

from datetime import datetime, timezone

import pytest
from conftest import SignedInClient
from starlette.testclient import TestClient as RawClient
from starlette.websockets import WebSocketDisconnect

from app.broker.mock import MockBroker
from app.config import Settings
from app.llm.types import LLMTurn
from app.main import create_app
from app.risk.presets import preset
from app.schemas import paise

T0 = datetime(2026, 10, 8, 10, 0, tzinfo=timezone.utc)
INTENT = dict(action="PLACE", instrument_ref="INFY", side="BUY", quantity=10, order_type="LIMIT", limit_price=140000)
RULE = dict(kind="TRIGGER_ORDER", instrument="tcs", comparator="BELOW", price_rupees=3800, side="BUY", quantity=5)
PLAN = dict(legs=[dict(instrument="infosys", side="SELL", fraction_of_holding=0.5),
                  dict(instrument="itc", side="BUY", proceeds_of_leg=0)])


class Desk:
    def __init__(self, app, a, b):
        self.app, self.a, self.b = app, a, b
        self.a_id = a.get("/api/auth/me").json()["user"]["id"]
        self.b_id = b.get("/api/auth/me").json()["user"]["id"]

    def ws(self, who):  # that user's workspace
        return self.app.state.workspaces.peek(self.a_id if who == "a" else self.b_id)

    def on_loop(self, fn, *args):
        return self.a.portal.call(lambda: fn(*args))


@pytest.fixture
def desk():
    settings = Settings(ticker_interval=None, reconcile_interval=None, external_sync_interval=None, account_push_interval=3600)
    app = create_app(settings, broker=MockBroker(clock=lambda: T0), clock=lambda: T0)
    with SignedInClient(app, email="a@example.com") as a:
        b = SignedInClient(app, email="b@example.com")
        b.portal = a.portal  # one event loop for both desks, as in a real server
        a.sign_in()
        b.sign_in()
        yield Desk(app, a, b)


def until(ws, kind, limit=10):
    """Events from a socket up to and including the first of this type."""
    seen = []
    for _ in range(limit):
        seen.append(ws.receive_json())
        if seen[-1]["type"] == kind:
            return seen
    raise AssertionError(f"no {kind} event within {limit}: {[e['type'] for e in seen]}")


def card(client, **over):
    r = client.post("/api/orders/preview", json={**INTENT, **over})
    assert r.status_code == 200, r.text
    return r.json()["cards"][0]["pending"]


def approve(client, p, **kw):
    return client.post(f"/api/approvals/{p['id']}/approve", json={"order_hash": p["order_hash"]}, **kw)


# ---- the accounts themselves ---------------------------------------------------------------------------------- #


def test_each_user_has_their_own_broker_account_and_orders(desk):
    p = card(desk.a)
    assert approve(desk.a, p).json()["outcome"] == "SENT"
    assert len(desk.a.get("/api/orders").json()) == 1
    assert desk.b.get("/api/orders").json() == []
    assert desk.ws("a").broker is not desk.ws("b").broker


# ---- cards and approvals (1.4) -------------------------------------------------------------------------------- #


def test_user_b_cannot_see_approve_or_reject_user_as_card(desk):
    p = card(desk.a)
    assert [c["id"] for c in desk.a.get("/api/pending").json()["orders"]] == [p["id"]]
    assert desk.b.get("/api/pending").json() == {"orders": [], "plans": []}
    r = approve(desk.b, p)  # B has a valid session, a valid CSRF token and A's exact id and hash
    assert r.status_code == 404 and r.json() == {"detail": "unknown approval id"}  # does not reveal that the card exists
    assert desk.b.post(f"/api/approvals/{p['id']}/reject").status_code == 404
    assert desk.a.get("/api/orders").json() == [] and desk.b.get("/api/orders").json() == []  # nothing was sent
    assert desk.ws("a").pending.get(p["id"]).state.value == "PENDING"  # and A's card is untouched
    assert approve(desk.a, p).json()["outcome"] == "SENT"  # A can still approve it


def test_approve_needs_a_session_a_csrf_token_and_the_owner(desk):
    p = card(desk.a)
    path = f"/api/approvals/{p['id']}/approve"
    body = {"order_hash": p["order_hash"]}
    raw = RawClient(desk.app)
    raw.portal = desk.a.portal
    assert raw.post(path, json=body).status_code == 401  # no session
    assert desk.a.post(path, json=body, headers={"X-CSRF-Token": ""}).status_code == 403  # a session, but no token
    assert desk.a.post(path, json=body, headers={"X-CSRF-Token": desk.b.headers["X-CSRF-Token"]}).status_code == 403  # B's token
    assert desk.a.get("/api/orders").json() == []  # none of that sent anything
    assert desk.a.post(path, json=body).status_code == 200


def test_a_double_click_still_sends_once(desk):
    p = card(desk.a)
    first, second = approve(desk.a, p), approve(desk.a, p)
    assert first.status_code == 200 and second.status_code == 409
    assert len(desk.a.get("/api/orders").json()) == 1
    rows = desk.app.state.db.query("SELECT user_id FROM executions")
    assert [r["user_id"] for r in rows] == [desk.a_id]  # one send, written under A


def test_the_send_log_belongs_to_the_user_who_sent(desk):
    pa, pb = card(desk.a), card(desk.b)
    assert approve(desk.a, pa).status_code == 200 and approve(desk.b, pb).status_code == 200
    rows = {r["client_order_id"]: r["user_id"] for r in desk.app.state.db.query("SELECT client_order_id, user_id FROM executions")}
    assert rows[pa["client_order_id"]] == desk.a_id and rows[pb["client_order_id"]] == desk.b_id
    assert len(rows) == 2


# ---- plans ----------------------------------------------------------------------------------------------------- #


def test_plans_are_private(desk):
    plan = desk.a.post("/api/plans/preview", json=PLAN).json()["cards"][0]["plan"]
    assert desk.b.get("/api/pending").json()["plans"] == []
    body = {"plan_hash": plan["plan_hash"]}
    assert desk.b.post(f"/api/plans/{plan['id']}/approve", json=body).status_code == 404
    assert desk.b.post(f"/api/plans/{plan['id']}/reject").status_code == 404
    assert desk.b.get(f"/api/plans/{plan['id']}/report").status_code == 404
    assert desk.a.get("/api/orders").json() == [] and desk.b.get("/api/orders").json() == []
    assert [p["id"] for p in desk.a.get("/api/pending").json()["plans"]] == [plan["id"]]


# ---- standing rules --------------------------------------------------------------------------------------------- #


def test_rules_are_private(desk):
    rule = desk.a.post("/api/rules", json=RULE).json()["cards"][0]["rule"]
    assert desk.b.get("/api/rules").json() == []
    assert desk.b.delete(f"/api/rules/{rule['id']}").status_code == 404
    assert [r["id"] for r in desk.a.get("/api/rules").json()] == [rule["id"]]  # still there, still active
    assert desk.a.get("/api/rules").json()[0]["status"] == "ACTIVE"


def test_a_rule_made_by_a_only_ever_makes_cards_for_a(desk):
    desk.a.post("/api/rules", json=RULE)
    desk.on_loop(desk.ws("b").broker.set_price, "NSE:TCS", paise(3799))  # B's market crosses the line: nothing to fire
    desk.on_loop(desk.ws("a").broker.set_price, "NSE:TCS", paise(3799))  # A's market does
    import time
    deadline = time.time() + 3
    while time.time() < deadline and not desk.a.get("/api/pending").json()["orders"]:
        time.sleep(0.05)
    cards = desk.a.get("/api/pending").json()["orders"]
    assert len(cards) == 1 and cards[0]["instrument"]["symbol"] == "TCS"
    assert desk.b.get("/api/pending").json()["orders"] == []
    assert desk.a.get("/api/rules").json()[0]["status"] == "FIRED"


# ---- the Discipline settings ------------------------------------------------------------------------------------ #


def test_risk_profile_and_goal_are_per_user(desk):
    mine = preset("aggressive").model_copy(update={"max_orders_per_day": 7})
    assert desk.a.put("/api/profile", json=mine.model_dump(mode="json")).status_code == 200
    assert desk.b.get("/api/profile").json() is None
    assert desk.a.get("/api/profile").json()["max_orders_per_day"] == 7
    goal = dict(target_pct=10, end_date="2026-12-31", max_acceptable_loss_paise=100_000)
    assert desk.a.put("/api/goal", json=goal).status_code == 200
    assert desk.b.get("/api/goal").json() is None
    assert desk.b.delete("/api/goal").status_code == 204
    assert desk.a.get("/api/goal").json() is not None  # B deleting "their" goal did not touch A's


def test_a_hard_limit_set_by_a_does_not_stop_b(desk):
    mine = preset("balanced").model_copy(update={"max_orders_per_day": 1, "hard_order_limit": True})
    desk.a.put("/api/profile", json=mine.model_dump(mode="json"))
    assert approve(desk.a, card(desk.a)).status_code == 200
    refused = desk.a.post("/api/orders/preview", json={**INTENT, "quantity": 11}).json()["cards"][0]
    assert refused["type"] != "pending_order" and "limit" in str(refused).lower()  # A is past the limit A chose
    assert approve(desk.b, card(desk.b)).status_code == 200
    assert approve(desk.b, card(desk.b, quantity=11)).status_code == 200  # B never set a limit


# ---- the audit log ---------------------------------------------------------------------------------------------- #


def test_audit_log_and_export_are_per_user(desk):
    desk.a.post("/api/chat", json={"message": "what is my P&L today"})
    mine = desk.a.get("/api/audit").json()
    assert any(e["kind"] == "USER_MESSAGE" for e in mine)
    assert desk.b.get("/api/audit").json() == []
    assert desk.b.get("/api/audit/export").text == ""
    assert "P&L" in desk.a.get("/api/audit/export").text
    rows = desk.app.state.db.query("SELECT DISTINCT user_id FROM audit_events")
    assert [r["user_id"] for r in rows] == [desk.a_id]  # every event carries the user it belongs to


def test_external_activity_is_per_user(desk):
    desk.a.portal.call(_place_direct, desk.ws("a"))  # an order placed straight at A's broker, as 021's own app would
    desk.a.portal.call(desk.ws("a").order_publisher.publish_changes)
    assert desk.b.get("/api/activity/external").json()["orders"] == []


async def _place_direct(ws):
    from app.schemas import OrderIntent, PendingState

    pending = await ws.builder.build(OrderIntent(**INTENT))
    return await ws.broker.place_order(pending.transition(PendingState.APPROVED))


# ---- chat: one user's words never reach another user's model call ------------------------------------------------ #


class Recorder:
    def __init__(self):
        self.seen: list[str] = []

    async def complete(self, *, system, messages, tools):
        self.seen += [m.text for m in messages]
        return LLMTurn(text="Okay.")


def test_chat_history_never_crosses_users(desk):
    rec_a, rec_b = Recorder(), Recorder()
    desk.ws("a").copilot._llm, desk.ws("b").copilot._llm = rec_a, rec_b
    desk.a.post("/api/chat", json={"message": "my secret watchlist is ZEBRA123"})
    desk.b.post("/api/chat", json={"message": "hello there"})
    desk.b.post("/api/chat", json={"message": "and again"})
    assert "ZEBRA123" in " ".join(rec_a.seen)
    assert "ZEBRA123" not in " ".join(rec_b.seen)
    assert not any("ZEBRA123" in m.text for m in desk.ws("b").copilot._history)


# ---- live events ------------------------------------------------------------------------------------------------- #


def test_events_are_delivered_only_to_the_owner(desk):
    hub = desk.app.state.events
    qa, qb = hub.subscribe(desk.a_id), hub.subscribe(desk.b_id)
    card(desk.a)
    assert not qa.empty() and qb.empty()  # A's card made events for A and none for B
    seqs = []
    while not qa.empty():
        e = qa.get_nowait()
        seqs.append(e.seq)
        assert e.user_id == desk.a_id
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    before = hub.seq(desk.b_id)
    card(desk.b)
    assert not qb.empty() and qa.empty()
    first_b = qb.get_nowait()
    assert first_b.seq == before + 1  # B's numbering continues from B's own last event, whatever A has been doing
    assert hub.seq(desk.a_id) > 0 and hub.seq("somebody-else") == 0


def test_the_websocket_needs_a_session_and_our_origin(desk):
    anonymous = RawClient(desk.app)
    anonymous.portal = desk.a.portal
    with pytest.raises(WebSocketDisconnect):
        with anonymous.websocket_connect("/ws"):
            pass
    with pytest.raises(WebSocketDisconnect):
        with desk.a.websocket_connect("/ws", headers={"Origin": "https://evil.example"}):
            pass
    with desk.a.websocket_connect("/ws", headers={"Origin": "http://localhost:5173"}) as ws:
        assert ws.receive_json()["type"] == "snapshot"


def test_a_websocket_snapshot_holds_only_that_users_things(desk):
    p = card(desk.a)
    desk.a.post("/api/rules", json=RULE)
    with desk.b.websocket_connect("/ws") as ws:
        snap = ws.receive_json()
    assert snap["pending"] == {"orders": [], "plans": []} and snap["rules"] == [] and snap["orders"] == []
    with desk.a.websocket_connect("/ws") as ws:
        snap = ws.receive_json()
    assert [o["id"] for o in snap["pending"]["orders"]] == [p["id"]] and len(snap["rules"]) == 1


def test_b_socket_never_receives_a_events_and_a_user_with_two_tabs_gets_both(desk):
    with desk.b.websocket_connect("/ws") as wb, desk.a.websocket_connect("/ws") as wa1, desk.a.websocket_connect("/ws") as wa2:
        for s in (wb, wa1, wa2):
            assert s.receive_json()["type"] == "snapshot"
        pa = card(desk.a)  # A's events: audit rows and pending_created
        for s in (wa1, wa2):  # both of A's tabs hear it
            assert pa["id"] in str(until(s, "pending_created"))
        pb = card(desk.b)  # if A's events had leaked to B, they would be queued ahead of this one
        got = until(wb, "pending_created")
        assert all(pa["id"] not in str(e) for e in got), "B's socket was sent something about A's card"
        assert got[-1]["pending"]["id"] == pb["id"]


# ---- background loops: one user's trouble does not stall another's --------------------------------------------------- #


def test_a_stuck_or_failing_desk_does_not_stall_another_users_rules(desk):
    import asyncio
    import time

    async def never_returns(_tick):
        await asyncio.sleep(3600)  # B's rule engine hangs on every price tick

    async def explodes(_tick):
        raise RuntimeError("B's rule engine is broken")

    def status(rule_id):
        return next(r for r in desk.a.get("/api/rules").json() if r["id"] == rule_id)["status"]

    for broken in (never_returns, explodes):
        desk.ws("b").rule_engine.on_tick = broken
        rule = desk.a.post("/api/rules", json=RULE).json()["cards"][0]["rule"]
        desk.on_loop(desk.ws("b").broker.set_price, "NSE:TCS", paise(3790))  # B's loop is now stuck or failing...
        desk.on_loop(desk.ws("a").broker.set_price, "NSE:TCS", paise(3799))  # ...A's rule must still fire
        deadline = time.time() + 3
        while time.time() < deadline and status(rule["id"]) != "FIRED":
            time.sleep(0.05)
        assert status(rule["id"]) == "FIRED", broken.__name__
        desk.on_loop(desk.ws("a").broker.set_price, "NSE:TCS", paise(3900))  # back above, ready for the next round
