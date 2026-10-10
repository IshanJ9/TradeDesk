"""Accounts and sessions: hashing, tokens, expiry, login throttling, CSRF, cookies. Uses the real app factory with
the raw Starlette client (the shared test client in conftest.py logs in for you; these tests must not)."""

import re
from datetime import datetime, timedelta, timezone

import pytest
from starlette.testclient import TestClient

from app.auth import passwords
from app.auth.ratelimit import LoginLimiter
from app.auth.tokens import hash_token, new_token
from app.config import Settings
from app.main import create_app

T0 = datetime(2026, 10, 10, 5, 0, tzinfo=timezone.utc)
PW = "correct horse battery"
GOOD = dict(email="Ada@Example.com", password=PW, display_name="Ada", accepts_no_advice=True)


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


@pytest.fixture
def clock():
    return Clock()


def make(clock, **settings):
    base = dict(ticker_interval=None, reconcile_interval=None, external_sync_interval=None, account_push_interval=3600)
    return create_app(Settings(**{**base, **settings}), clock=clock)


@pytest.fixture
def app(clock):
    return make(clock)


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


def register(client, **over):
    return client.post("/api/auth/register", json={**GOOD, **over})


def csrf(client) -> dict:
    return {"X-CSRF-Token": client.get("/api/auth/me").json()["csrf_token"]}


# ---- hashing ---------------------------------------------------------------------------------------------------- #


def test_passwords_are_argon2id_and_verify():
    h = passwords.hash_password(PW)
    assert h.startswith("$argon2id$") and PW not in h
    assert passwords.verify_password(h, PW)
    assert not passwords.verify_password(h, PW + "x")
    assert not passwords.verify_password("not a hash", PW)
    assert passwords.hash_password(PW) != h  # salted


@pytest.mark.parametrize("pw,email,ok", [
    ("short", "a@b.co", False),
    ("a" * 9, "a@b.co", False),
    ("a" * 10, "a@b.co", True),  # no composition rules
    ("A@B.co", "a@b.co", False),  # the email itself (and too short)
    ("somebody@example.com", "SomeBody@example.com", False),  # equal to the email, case-insensitive
    ("a" * 257, "a@b.co", False),
])
def test_password_rules(pw, email, ok):
    assert (passwords.password_problem(pw, email) is None) is ok


def test_tokens_are_random_and_only_their_hash_is_stored(app, client):
    assert new_token() != new_token() and len(new_token()) >= 40
    assert hash_token("x") == hash_token("x") != hash_token("y")
    r = register(client)
    token = client.cookies.get("tradedesk_session")
    db = app.state.db
    rows = db.query("SELECT token_hash FROM sessions")
    assert [r["token_hash"] for r in rows] == [hash_token(token)]
    assert token not in tuple(db.query("SELECT * FROM sessions")[0])  # the raw token is nowhere in the row
    assert r.status_code == 201


# ---- register --------------------------------------------------------------------------------------------------- #


def test_register_requires_the_no_advice_acknowledgment(client):
    r = register(client, accepts_no_advice=False)
    assert r.status_code == 422 and "no investment advice" in r.json()["detail"]
    assert client.get("/api/auth/me").status_code == 401  # nothing was created


def test_register_records_the_acknowledgment_time_and_lowercases_the_email(app, client):
    register(client)
    row = app.state.db.query("SELECT * FROM users")[0]
    assert row["email"] == "ada@example.com"
    assert row["accepted_no_advice_at"] == T0.isoformat()


def test_register_rejects_bad_email_short_password_and_duplicates(client):
    assert register(client, email="nope").status_code == 422
    assert register(client, password="short").status_code == 422
    assert register(client, password="ada@example.com").status_code == 422
    assert register(client).status_code == 201
    dup = register(client, email="ADA@example.com")
    assert dup.status_code == 409


def test_a_password_or_hash_is_never_returned_or_logged(app, client, caplog):
    caplog.set_level("DEBUG")
    body = register(client).text
    login = client.post("/api/auth/login", json={"email": GOOD["email"], "password": PW}).text
    me = client.get("/api/auth/me").text
    stored = app.state.db.query("SELECT password_hash FROM users")[0]["password_hash"]
    for text in (body, login, me, caplog.text):
        assert PW not in text and stored not in text and "argon2" not in text


# ---- cookie ------------------------------------------------------------------------------------------------------ #


def test_cookie_is_httponly_samesite_lax_and_secure_off_localhost(clock):
    with TestClient(make(clock), base_url="http://localhost") as c:
        h = register(c).headers["set-cookie"].lower()
        assert "httponly" in h and "samesite=lax" in h and "secure" not in h and "path=/" in h
    with TestClient(make(clock), base_url="https://desk.example.com") as c:
        assert "secure" in register(c, email="b@example.com").headers["set-cookie"].lower()


def test_cookie_secure_can_be_forced(clock):
    with TestClient(make(clock, cookie_secure=True), base_url="http://localhost") as c:
        assert "secure" in register(c).headers["set-cookie"].lower()


# ---- sessions: login, me, logout, expiry ------------------------------------------------------------------------- #


def test_login_me_logout(client):
    register(client)
    client.cookies.clear()
    assert client.get("/api/auth/me").status_code == 401
    r = client.post("/api/auth/login", json={"email": " ADA@example.com ", "password": PW})
    assert r.status_code == 200 and r.json()["user"]["email"] == "ada@example.com" and r.json()["csrf_token"]
    me = client.get("/api/auth/me").json()
    assert me["user"]["display_name"] == "Ada" and me["csrf_token"] == r.json()["csrf_token"]
    assert client.post("/api/auth/logout", headers=csrf(client)).status_code == 204
    assert client.get("/api/auth/me").status_code == 401


def test_logout_deletes_the_session_server_side(app, client):
    register(client)
    token = client.cookies.get("tradedesk_session")
    client.post("/api/auth/logout", headers=csrf(client))
    assert app.state.auth.store.count_sessions() == 0
    client.cookies.set("tradedesk_session", token)  # replaying the old cookie gets nothing
    assert client.get("/api/auth/me").status_code == 401


def test_idle_expiry(clock, client):
    register(client)
    clock.advance(hours=11)
    assert client.get("/api/auth/me").status_code == 200  # activity keeps it alive
    clock.advance(hours=11)
    assert client.get("/api/auth/me").status_code == 200
    clock.advance(hours=13)  # idle for longer than 12 hours
    assert client.get("/api/auth/me").status_code == 401


def test_absolute_expiry_even_when_active(clock, client):
    register(client)
    for _ in range(14):  # active every 11 hours: never idle, but more than 7 days pass
        clock.advance(hours=11)
        client.get("/api/auth/me")
    assert clock.now - T0 > timedelta(days=6)
    for _ in range(3):
        clock.advance(hours=11)
        client.get("/api/auth/me")
    assert clock.now - T0 > timedelta(days=7)
    assert client.get("/api/auth/me").status_code == 401


def test_a_disabled_user_with_a_leftover_session_is_still_refused(app, client):
    register(client)
    uid = app.state.auth.store.first_user().id
    app.state.db.execute("UPDATE users SET disabled = 1 WHERE id = ?", (uid,))  # the session row is still there
    assert app.state.auth.store.count_sessions(uid) == 1
    assert client.get("/api/auth/me").status_code == 401


def test_disabled_user_is_signed_out_everywhere(app, client):
    register(client)
    uid = app.state.auth.store.first_user().id
    app.state.auth.store.set_disabled(uid, True)
    assert client.get("/api/auth/me").status_code == 401
    assert app.state.auth.store.count_sessions(uid) == 0
    r = client.post("/api/auth/login", json={"email": GOOD["email"], "password": PW})
    assert r.status_code == 401 and r.json()["detail"] == "Incorrect email or password."


def test_change_password_kills_every_other_session(app, client):
    register(client)
    other = TestClient(app)
    other.__enter__()
    assert other.post("/api/auth/login", json={"email": GOOD["email"], "password": PW}).status_code == 200
    assert other.get("/api/auth/me").status_code == 200
    bad = client.post("/api/auth/change-password", headers=csrf(client),
                      json={"current_password": "wrong password!", "new_password": "a brand new passphrase"})
    assert bad.status_code == 403
    ok = client.post("/api/auth/change-password", headers=csrf(client),
                     json={"current_password": PW, "new_password": "a brand new passphrase"})
    assert ok.status_code == 200
    assert other.get("/api/auth/me").status_code == 401  # the other device was signed out
    assert client.get("/api/auth/me").status_code == 200  # this one got a fresh session
    assert app.state.auth.store.count_sessions() == 1
    client.cookies.clear()
    assert client.post("/api/auth/login", json={"email": GOOD["email"], "password": PW}).status_code == 401
    assert client.post("/api/auth/login", json={"email": GOOD["email"], "password": "a brand new passphrase"}).status_code == 200
    other.__exit__(None, None, None)


def test_change_password_applies_the_password_rules(client):
    register(client)
    r = client.post("/api/auth/change-password", headers=csrf(client),
                    json={"current_password": PW, "new_password": "ada@example.com"})
    assert r.status_code == 422


# ---- login: same answer for unknown email and wrong password, and throttling -------------------------------------- #


def test_unknown_email_and_wrong_password_look_the_same(client):
    register(client)
    client.cookies.clear()
    a = client.post("/api/auth/login", json={"email": "nobody@example.com", "password": PW})
    b = client.post("/api/auth/login", json={"email": GOOD["email"], "password": "wrong password!"})
    assert (a.status_code, a.json()) == (b.status_code, b.json()) == (401, {"detail": "Incorrect email or password."})


def test_login_runs_a_hash_even_when_the_email_is_unknown(monkeypatch, client):
    calls = []
    real = passwords.verify_password
    monkeypatch.setattr(passwords, "verify_password", lambda h, p: calls.append(1) or real(h, p))
    client.post("/api/auth/login", json={"email": "nobody@example.com", "password": PW})
    assert calls, "an unknown email must still cost a password verification, so timing does not give the email away"


def test_lockout_after_repeated_failures_blocks_even_the_right_password(clock, client):
    register(client)
    client.cookies.clear()
    for _ in range(5):
        assert client.post("/api/auth/login", json={"email": GOOD["email"], "password": "wrong password!"}).status_code == 401
    r = client.post("/api/auth/login", json={"email": GOOD["email"], "password": PW})
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0
    clock.advance(minutes=6)
    assert client.post("/api/auth/login", json={"email": GOOD["email"], "password": PW}).status_code == 200


def test_lockout_applies_to_unknown_emails_too(client):
    for _ in range(5):
        client.post("/api/auth/login", json={"email": "ghost@example.com", "password": "whatever 123"})
    assert client.post("/api/auth/login", json={"email": "ghost@example.com", "password": "whatever 123"}).status_code == 429


def test_limiter_ip_limit_and_sliding_window(clock):
    lim = LoginLimiter(clock, max_per_email=3, max_per_ip=4)
    for i in range(4):
        lim.failed(f"user{i}@x.co", "1.2.3.4")
    assert lim.retry_after("another@x.co", "1.2.3.4") > 0  # the IP is locked, whatever the email
    assert lim.retry_after("another@x.co", "5.6.7.8") == 0
    lim2 = LoginLimiter(clock, max_per_email=3)
    lim2.failed("a@x.co", "ip"); lim2.failed("a@x.co", "ip")
    clock.advance(minutes=16)  # the window slid past the first two
    lim2.failed("a@x.co", "ip")
    assert lim2.retry_after("a@x.co", "ip") == 0


def test_a_good_login_clears_the_email_failures(clock):
    lim = LoginLimiter(clock, max_per_email=3)
    lim.failed("a@x.co", "ip"); lim.failed("a@x.co", "ip")
    lim.succeeded("a@x.co", "ip")
    lim.failed("a@x.co", "ip")
    assert lim.retry_after("a@x.co", "ip") == 0


# ---- CSRF and origin --------------------------------------------------------------------------------------------- #


def test_state_changing_requests_need_the_csrf_token(client):
    register(client)
    assert client.post("/api/auth/logout").status_code == 403  # cookie alone is not enough
    assert client.post("/api/auth/logout", headers={"X-CSRF-Token": "guess"}).status_code == 403
    assert client.get("/api/auth/me").status_code == 200  # still signed in: nothing happened
    assert client.post("/api/auth/logout", headers=csrf(client)).status_code == 204


def test_the_csrf_token_of_another_session_does_not_work(app, client):
    register(client, email="a@example.com")
    other = TestClient(app)
    other.__enter__()
    register(other, email="b@example.com")
    stolen = csrf(other)
    assert client.post("/api/auth/logout", headers=stolen).status_code == 403
    other.__exit__(None, None, None)


def test_cross_site_origin_is_refused_even_with_a_valid_token(client):
    register(client)
    h = csrf(client)
    assert client.post("/api/auth/logout", headers={**h, "Origin": "https://evil.example"}).status_code == 403
    assert client.post("/api/auth/logout", headers={**h, "Origin": "http://localhost:5173"}).status_code == 204


def test_login_attempt_through_a_hostile_origin_is_not_a_csrf_hole(client):
    # login itself is public, but the cookie it sets is SameSite=Lax and every state change needs the token
    register(client)
    assert re.search(r"samesite=lax", client.post("/api/auth/login", json={"email": GOOD["email"], "password": PW}).headers["set-cookie"].lower())
