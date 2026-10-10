"""Linking a 021 account to a TradeDesk user: the login is checked for real, stored encrypted, never shown or logged;
each user gets their own 021 session; switching accounts rejects pending cards; a session 021 revoked shows as
"needs reconnect" and refuses approvals instead of crashing. The fake 021 stands in for the real one."""

import asyncio
import base64
import functools
import logging
import os
import time
from datetime import datetime, timezone

import httpx
import pytest
from conftest import SignedInClient
from fake021 import Fake021, FakeConnector, full_nse_cash, ltp_packet
from starlette.testclient import TestClient as RawClient
from starlette.websockets import WebSocketDisconnect

from app.broker.disconnected import DisconnectedBroker
from app.broker.zerotwoone import ZeroTwoOneAdapter
from app.config import Settings
from app.main import create_app
from app.schemas import paise
from app.vault import Vault, VaultCorrupt, VaultUnavailable

T0 = datetime(2026, 10, 9, 10, 0, tzinfo=timezone.utc)
KEY = base64.urlsafe_b64encode(bytes(range(32))).decode()
KEY2 = base64.urlsafe_b64encode(bytes(range(32, 64))).decode()
PW_A, PW_B = "Sup3r-secret-021-pass-A", "Sup3r-secret-021-pass-B"


class Rig:
    """Two 021 accounts (HACK1234, HACK5678) behind a factory the app uses instead of the real adapter."""

    def __init__(self):
        self.fakes = {"HACK1234": Fake021(ucc="HACK1234", password=PW_A), "HACK5678": Fake021(ucc="HACK5678", password=PW_B)}
        self.conns: dict[str, FakeConnector] = {}
        self.unreachable = False
        self.closed: list[str] = []  # adapters whose session was closed, by client id

    def __call__(self, username, password):
        if self.unreachable:
            http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ConnectError("down"))))
        else:
            http = (self.fakes.get(username) or Fake021(ucc="NOBODY", password="x")).client()
        self.conns[username] = FakeConnector()
        adapter = ZeroTwoOneAdapter(username=username, password=password, http=http, connect=self.conns[username],
                                    clock=lambda: T0, cache_dir=None, retry_delay=0.001, price_wait=0.3)
        original_close = adapter.close

        async def close():
            self.closed.append(username)
            await original_close()

        adapter.close = close
        return adapter


def settings(path=None, **over):
    base = dict(ticker_interval=None, reconcile_interval=None, external_sync_interval=None, timeout_reconcile_delay=0,
                account_push_interval=3600, secret_key=KEY)
    if path:
        base["database_url"] = f"sqlite:///{path.as_posix()}"
    return Settings(**{**base, **over})


@pytest.fixture
def rig():
    return Rig()


@pytest.fixture
def desk(rig):
    app = create_app(settings(), clock=lambda: T0, zerotwoone_factory=rig)
    with SignedInClient(app, email="a@example.com") as a:
        b = SignedInClient(app, email="b@example.com")
        b.portal = a.portal
        a.sign_in()
        b.sign_in()
        yield app, a, b


def link(client, user="HACK1234", pw=PW_A):
    return client.post("/api/broker/link", json={"username": user, "password": pw})


def on_loop(client, fn, *args):
    return client.portal.call(functools.partial(fn, *args))


async def push(conn, frame):
    conn.market[0].push(frame)
    await asyncio.sleep(0.02)


def feed_prices(client, rig, ucc):
    deadline = time.time() + 2
    while not rig.conns[ucc].market and time.time() < deadline:
        time.sleep(0.01)
    on_loop(client, push, rig.conns[ucc], ltp_packet(1, 1594, 145000))
    on_loop(client, push, rig.conns[ucc], full_nse_cash(1594, 145000, 144000, 144500, 146000, 143500))


def card(client):
    r = client.post("/api/orders/preview", json=dict(action="PLACE", instrument_ref="INFY", side="BUY", quantity=10,
                                                      order_type="LIMIT", limit_price=paise(1450)))
    assert r.json()["cards"][0]["type"] == "pending_order", r.json()
    return r.json()["cards"][0]["pending"]


# ---- the vault -------------------------------------------------------------------------------------------------------- #


def test_the_vault_seals_per_user_and_never_in_the_clear():
    v = Vault(KEY)
    nonce, blob = v.seal("user-a", {"username": "HACK1234", "password": PW_A})
    assert PW_A.encode() not in blob and b"HACK1234" not in blob
    assert v.open("user-a", nonce, blob)[0] == {"username": "HACK1234", "password": PW_A}
    with pytest.raises(VaultCorrupt):  # the ciphertext copied onto another user's row does not open
        v.open("user-b", nonce, blob)
    with pytest.raises(VaultCorrupt):
        v.open("user-a", nonce, blob[:-1] + bytes([blob[-1] ^ 1]))  # damaged
    with pytest.raises(VaultCorrupt):
        Vault(KEY2).open("user-a", nonce, blob)  # wrong key
    assert v.seal("user-a", {"x": 1})[0] != v.seal("user-a", {"x": 1})[0]  # a fresh nonce every time


def test_the_vault_refuses_to_work_without_a_good_key():
    for bad in ("", "not base64!!", base64.urlsafe_b64encode(b"short").decode()):
        v = Vault(bad)
        assert not v.available
        with pytest.raises(VaultUnavailable):
            v.seal("u", {})


def test_the_ucc_fingerprint_ignores_case_and_differs_per_account():
    v = Vault(KEY)
    assert v.fingerprint("hack1234") == v.fingerprint(" HACK1234 ") != v.fingerprint("HACK5678")
    assert "HACK1234" not in v.fingerprint("HACK1234")


# ---- linking ------------------------------------------------------------------------------------------------------------ #


def test_a_user_starts_on_the_simulated_account_and_can_link_a_real_one(desk, rig):
    app, a, b = desk
    start = a.get("/api/broker").json()
    assert start == {"kind": "mock", "status": "mock", "ucc_hint": None, "server_account": False, "can_link": True,
                     "link_unavailable_reason": None}
    assert a.get("/api/account").json()["account_kind"] == "mock"
    r = link(a)
    assert r.status_code == 200, r.text
    assert r.json() == {"kind": "021", "status": "connected", "ucc_hint": "…1234", "server_account": False,
                        "can_link": True, "link_unavailable_reason": None}
    assert a.get("/api/account").json()["account_kind"] == "021"
    assert rig.fakes["HACK1234"].logins == 1  # we logged in once: the session that checked the login is the desk's session
    assert b.get("/api/broker").json()["kind"] == "mock"  # B is unaffected


def test_the_login_is_never_in_the_database_in_the_clear_nor_in_any_response_or_log(tmp_path, rig, caplog):
    caplog.set_level(logging.DEBUG)
    path = tmp_path / "t.db"
    app = create_app(settings(path), clock=lambda: T0, zerotwoone_factory=rig)
    seen = []
    with SignedInClient(app, email="a@example.com") as a:
        seen.append(link(a))
        seen += [a.get("/api/broker"), a.get("/api/account"), a.get("/api/audit"), a.get("/api/audit/export")]
        a.delete("/api/broker/link")
        seen.append(link(a))
        row = app.state.db.query("SELECT * FROM broker_links")[0]
        assert row["ucc_last4"] == "1234" and row["status"] == "connected"
    bodies = " ".join(r.text for r in seen) + caplog.text
    for secret in (PW_A, "HACK1234"):
        assert secret not in bodies, f"{secret!r} leaked into a response or a log"
    raw = b"".join(p.read_bytes() for p in tmp_path.iterdir())  # the database file and its WAL
    assert PW_A.encode() not in raw and b"HACK1234" not in raw and KEY.encode() not in raw


def test_a_wrong_password_saves_nothing_and_keeps_the_simulated_account(desk, rig):
    app, a, _ = desk
    r = link(a, pw="wrong password")
    assert r.status_code == 422 and "did not accept" in r.json()["detail"] and "wrong password" not in r.text
    assert app.state.db.query("SELECT * FROM broker_links") == []
    assert a.get("/api/broker").json()["kind"] == "mock"
    assert rig.closed == ["HACK1234"]  # the session that was opened to check the login is not left running
    r = link(a, user="NOBODY1", pw="whatever")
    assert r.status_code == 422


def test_if_021_cannot_be_reached_nothing_is_saved(desk, rig):
    app, a, _ = desk
    rig.unreachable = True
    r = link(a)
    assert r.status_code == 503 and "Nothing was saved" in r.json()["detail"]
    assert app.state.db.query("SELECT * FROM broker_links") == [] and a.get("/api/broker").json()["kind"] == "mock"


def test_one_021_account_cannot_be_linked_by_two_users(desk, rig):
    app, a, b = desk
    assert link(a).status_code == 200
    for name in ("HACK1234", "hack1234", " Hack1234 "):
        r = link(b, user=name)
        assert r.status_code == 409 and "already in use" in r.json()["detail"]
    assert b.get("/api/broker").json()["kind"] == "mock"
    assert rig.fakes["HACK1234"].logins == 1  # B's attempts never logged in, so A's session was not revoked
    assert link(b, user="HACK5678", pw=PW_B).status_code == 200  # a different account is fine


def test_two_users_have_two_separate_021_sessions(desk, rig):
    app, a, b = desk
    assert link(a).status_code == 200 and link(b, user="HACK5678", pw=PW_B).status_code == 200
    fa, fb = rig.fakes["HACK1234"], rig.fakes["HACK5678"]
    assert (fa.logins, fb.logins) == (1, 1) and fa is not fb  # each account was logged in once, by its own user
    ids = {n: c.get("/api/auth/me").json()["user"]["id"] for n, c in (("a", a), ("b", b))}
    ba, bb = (app.state.workspaces.peek(ids[n]).broker for n in ("a", "b"))
    assert ba is not bb and ba._token == fa.token and bb._token == fb.token
    assert a.get("/api/broker").json()["ucc_hint"] == "…1234" and b.get("/api/broker").json()["ucc_hint"] == "…5678"


def test_linking_needs_the_server_key_and_everything_else_still_works_without_it(rig):
    app = create_app(settings(secret_key=""), clock=lambda: T0, zerotwoone_factory=rig)
    with SignedInClient(app, email="a@example.com") as a:
        st = a.get("/api/broker").json()
        assert st["can_link"] is False and "TRADEDESK_SECRET_KEY" in st["link_unavailable_reason"]
        assert link(a).status_code == 503 and rig.fakes["HACK1234"].logins == 0
        assert a.get("/api/account").status_code == 200 and a.post("/api/orders/preview", json=dict(
            action="PLACE", instrument_ref="INFY", side="BUY", quantity=1, order_type="MARKET")).status_code == 200


def test_linking_is_throttled(desk):
    _, a, _ = desk
    for _ in range(5):
        assert link(a, pw="nope").status_code == 422
    r = link(a)  # even the right password, while locked
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0


def test_the_server_account_belongs_to_the_owner_and_cannot_be_linked_to_anyone_else(rig):
    fake = Fake021(ucc="HACK9999", password="srv-pw")
    server = ZeroTwoOneAdapter(username="HACK9999", password="srv-pw", http=fake.client(), connect=FakeConnector(),
                               clock=lambda: T0, cache_dir=None, retry_delay=0.001, price_wait=0.3)
    rig.fakes["HACK9999"] = fake
    app = create_app(settings(broker="zerotwoone", zerotwoone_username="HACK9999", zerotwoone_password="srv-pw"),
                     broker=server, clock=lambda: T0, zerotwoone_factory=rig)
    with SignedInClient(app, email="owner@example.com") as owner:
        other = SignedInClient(app, email="other@example.com")
        other.portal = owner.portal
        owner.sign_in()
        other.sign_in()
        st = owner.get("/api/broker").json()
        assert st["server_account"] is True and st["kind"] == "021" and st["can_link"] is False and st["ucc_hint"] == "…9999"
        assert link(owner, "HACK1234").status_code == 409 and owner.delete("/api/broker/link").status_code == 409
        assert other.get("/api/broker").json()["kind"] == "mock"  # everyone else starts on the simulated account
        r = link(other, "HACK9999", "srv-pw")  # the server's own account cannot be taken by a second login
        assert r.status_code == 409 and fake.logins == 1
        assert link(other).status_code == 200


# ---- switching accounts ---------------------------------------------------------------------------------------------- #


def test_linking_and_unlinking_reject_pending_cards_and_plans(desk, rig):
    app, a, _ = desk
    mock_card = card(a)
    plan = a.post("/api/plans/preview", json=dict(legs=[dict(instrument="infosys", side="SELL", fraction_of_holding=0.5),
                                                        dict(instrument="itc", side="BUY", proceeds_of_leg=0)])).json()["cards"][0]["plan"]
    assert a.get("/api/pending").json()["orders"] and a.get("/api/pending").json()["plans"]
    assert link(a).status_code == 200
    assert a.get("/api/pending").json() == {"orders": [], "plans": []}  # priced against the old account: gone
    ws_cards = app.state.workspaces.peek(a.get("/api/auth/me").json()["user"]["id"]).pending
    assert ws_cards.get(mock_card["id"]) is None or ws_cards.get(mock_card["id"]).state.value == "REJECTED"
    assert a.post(f"/api/approvals/{mock_card['id']}/approve", json={"order_hash": mock_card["order_hash"]}).status_code in (404, 409)
    assert a.post(f"/api/plans/{plan['id']}/approve", json={"plan_hash": plan["plan_hash"]}).status_code in (404, 409)
    feed_prices(a, rig, "HACK1234")
    real_card = card(a)
    assert a.delete("/api/broker/link").status_code == 200
    assert a.get("/api/pending").json()["orders"] == []
    assert a.post(f"/api/approvals/{real_card['id']}/approve", json={"order_hash": real_card["order_hash"]}).status_code in (404, 409)
    assert rig.fakes["HACK1234"].orders == {}  # nothing reached 021 from any of this


def test_the_account_change_is_audited_without_the_login(desk):
    _, a, _ = desk
    link(a)
    a.delete("/api/broker/link")
    entries = [e for e in a.get("/api/audit").json() if e["kind"] == "BROKER_LINK"]
    assert [e["summary"] for e in reversed(entries)] == ["Linked a 021 account (client id ends …1234)",
                                                         "Unlinked the 021 account; back on the simulated account"]
    assert PW_A not in str(entries)


def test_unlinking_deletes_the_saved_login_and_goes_back_to_the_simulated_account(desk, rig):
    app, a, _ = desk
    link(a)
    assert app.state.db.query("SELECT COUNT(*) AS n FROM broker_links")[0]["n"] == 1
    r = a.delete("/api/broker/link")
    assert r.status_code == 200 and r.json()["kind"] == "mock" and r.json()["ucc_hint"] is None
    assert app.state.db.query("SELECT COUNT(*) AS n FROM broker_links")[0]["n"] == 0
    assert a.delete("/api/broker/link").status_code == 409  # nothing left to unlink
    assert link(a).status_code == 200  # and the account is free to link again


def test_an_order_still_waiting_for_021_blocks_changing_the_account(desk):
    app, a, _ = desk
    uid = a.get("/api/auth/me").json()["user"]["id"]
    app.state.db.execute("INSERT INTO executions(client_order_id,user_id,pending_id,action,status,created_at,updated_at,detail)"
                         " VALUES ('c1',?,'p','PLACE','UNKNOWN',?,?,'{}')", (uid, T0.isoformat(), T0.isoformat()))
    r = link(a)
    assert r.status_code == 409 and "waiting for 021" in r.json()["detail"]
    assert a.get("/api/broker").json()["kind"] == "mock"


def test_open_pages_are_told_to_reconnect_when_the_account_changes(desk):
    _, a, _ = desk
    with a.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "snapshot"
        link(a)
        with pytest.raises(WebSocketDisconnect):
            for _ in range(20):  # audit/pending events may arrive first; then the socket is closed
                ws.receive_json()
    with a.websocket_connect("/ws") as ws:
        assert ws.receive_json()["account"]["account_kind"] == "021"  # the fresh snapshot is the new account's


# ---- a session 021 revoked, or that never opened ---------------------------------------------------------------------- #


def test_a_revoked_021_session_shows_reconnect_and_refuses_approvals_then_reconnect_recovers(desk, rig):
    app, a, _ = desk
    link(a)
    feed_prices(a, rig, "HACK1234")
    fake = rig.fakes["HACK1234"]
    p = card(a)
    fake.lock_out = True  # another copy of the app keeps logging in: every token is revoked at once
    a.get("/api/orders")  # any read finds out
    st = a.get("/api/broker").json()
    assert st["status"] == "needs_reconnect" and st["kind"] == "021"
    r = a.post(f"/api/approvals/{p['id']}/approve", json={"order_hash": p["order_hash"]})
    assert r.status_code == 409 and r.json()["code"] == "BLOCKED" and "reconnected" in r.json()["message"]
    assert fake.orders == {} and [x for x in fake.requests if x[0] == "POST" and x[1] == "/orders"] == []
    assert a.get("/api/pending").json()["orders"][0]["state"] == "PENDING"  # the card was not consumed
    still = a.post("/api/broker/reconnect")  # 021 still revokes every token: told plainly, no crash, still not connected
    assert still.status_code == 503 and "in use somewhere else" in still.json()["detail"]
    assert a.get("/api/broker").json()["status"] == "needs_reconnect"
    fake.lock_out = False
    ok = a.post("/api/broker/reconnect")
    assert ok.status_code == 200 and ok.json()["status"] == "connected"
    after = a.post(f"/api/approvals/{p['id']}/approve", json={"order_hash": p["order_hash"]})
    assert "reconnected" not in after.text  # no longer refused for that reason


def test_a_plan_is_also_refused_while_the_021_session_is_revoked(desk, rig):
    app, a, _ = desk
    link(a)
    feed_prices(a, rig, "HACK1234")
    r = a.post("/api/plans/preview", json=dict(legs=[dict(instrument="infosys", side="BUY", quantity=1),
                                                     dict(instrument="infosys", side="BUY", quantity=2)]))
    plan = r.json()["cards"][0]["plan"]
    rig.fakes["HACK1234"].lock_out = True
    a.get("/api/orders")  # a read finds out the token was revoked
    r = a.post(f"/api/plans/{plan['id']}/approve", json={"plan_hash": plan["plan_hash"]})
    assert r.status_code == 409 and r.json()["code"] == "BLOCKED" and "reconnected" in r.json()["message"]
    assert rig.fakes["HACK1234"].orders == {}
    assert a.get("/api/pending").json()["plans"][0]["state"] == "PENDING"


def test_a_linked_user_whose_021_login_fails_at_startup_gets_a_reconnect_state_not_a_crash(tmp_path, rig):
    path = tmp_path / "r.db"
    with SignedInClient(create_app(settings(path), clock=lambda: T0, zerotwoone_factory=rig), email="a@example.com") as a:
        assert link(a).status_code == 200
    rig.fakes["HACK1234"].password = "changed at 021"  # the saved login no longer works
    with SignedInClient(create_app(settings(path), clock=lambda: T0, zerotwoone_factory=rig), email="a@example.com") as a:
        a.sign_in()
        st = a.get("/api/broker").json()
        assert st["status"] == "needs_reconnect" and st["ucc_hint"] == "…1234" and a.get("/api/auth/me").status_code == 200
        ws = a.app.state.workspaces.all()[0]
        assert isinstance(ws.broker, DisconnectedBroker)
        assert a.get("/api/orders").status_code == 503  # reads say "unreachable"; nothing is sent anywhere
        r = a.post("/api/broker/reconnect")
        assert r.status_code == 422 and "link it again" in r.json()["detail"]
        rig.fakes["HACK1234"].password = PW_A  # 021 accepts the saved login again
        assert a.post("/api/broker/reconnect").json()["status"] == "connected"
        assert a.get("/api/orders").status_code == 200


def test_changing_or_losing_the_server_key_degrades_to_reconnect_and_never_crashes(tmp_path, rig):
    path = tmp_path / "k.db"
    with SignedInClient(create_app(settings(path), clock=lambda: T0, zerotwoone_factory=rig), email="a@example.com") as a:
        link(a)
    for key in ("", KEY2):  # no key at all; a different key
        with SignedInClient(create_app(settings(path, secret_key=key), clock=lambda: T0, zerotwoone_factory=rig), email="a@example.com") as a:
            a.sign_in()
            assert a.get("/api/broker").json()["status"] == "needs_reconnect"
            assert a.get("/api/auth/me").status_code == 200


def test_the_key_can_be_changed_with_the_old_one_kept_as_previous(tmp_path, rig):
    path = tmp_path / "rot.db"
    with SignedInClient(create_app(settings(path), clock=lambda: T0, zerotwoone_factory=rig), email="a@example.com") as a:
        link(a)
    with SignedInClient(create_app(settings(path, secret_key=KEY2, secret_key_previous=KEY), clock=lambda: T0, zerotwoone_factory=rig),
                        email="a@example.com") as a:
        a.sign_in()
        assert a.get("/api/broker").json()["status"] == "connected"
    with SignedInClient(create_app(settings(path, secret_key=KEY2), clock=lambda: T0, zerotwoone_factory=rig), email="a@example.com") as a:
        a.sign_in()  # the previous key is no longer needed: the record was re-sealed under the new one
        assert a.get("/api/broker").json()["status"] == "connected"
        assert a.get("/api/broker").json()["ucc_hint"] == "…1234"


def test_a_saved_login_copied_onto_another_users_row_does_not_open(tmp_path, rig):
    app = create_app(settings(), clock=lambda: T0, zerotwoone_factory=rig)
    with SignedInClient(app, email="a@example.com") as a:
        link(a)
        row = app.state.db.query("SELECT * FROM broker_links")[0]
        app.state.db.execute("UPDATE broker_links SET user_id = 'someone-else'")
        with pytest.raises(VaultCorrupt):
            app.state.links.credentials("someone-else")
        assert row["ciphertext"]  # (the data is still there; it simply is not theirs to open)
