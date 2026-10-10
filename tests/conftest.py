"""Shared test setup.

1. Cheap password hashing for every test (speed only; production uses the real cost).
2. Most tests are about the desk, not about signing in, so the test client they import (`fastapi.testclient.TestClient`)
   signs in as one fixed user on its first request. Tests about accounts and isolation use the raw Starlette client
   (`from starlette.testclient import TestClient`) or `Client.as_user(...)`, and sign in explicitly.
3. Older tests reach into `app.state.<broker|pending|rules|...>`; those things now belong to a user's workspace. For
   those tests only, `app.state.<name>` resolves to the workspace of the one user who has signed in. This shim lives
   here, in the tests, on purpose: application code must never use it (tests/test_route_coverage.py greps for that).
"""

import asyncio
from http.cookies import SimpleCookie

import fastapi.testclient
import httpx
import pytest
import starlette.testclient
from starlette.datastructures import State

from app.auth import passwords

TEST_EMAIL = "desk-user@example.com"
TEST_PASSWORD = "test-password-123"
PER_USER = frozenset({
    "broker", "hub", "pending", "audit", "builder", "executor", "history", "risk", "profile_store", "discipline", "cards",
    "rule_store", "rules", "plan_store", "plans", "rule_engine", "copilot", "approvals", "order_publisher",
})


@pytest.fixture(autouse=True, scope="session")
def _cheap_password_hashing():
    passwords.configure(time_cost=1, memory_cost=8, parallelism=1)


def _session_cookie(response) -> tuple[str, str]:
    """(name, value) of the session cookie. Read from the header and re-set without Secure, so a client that talks
    plain http to a made-up host name still sends it (the Secure rule itself is tested in test_auth.py)."""
    jar = SimpleCookie()
    for header in response.headers.get_list("set-cookie"):
        jar.load(header)
    morsel = jar["tradedesk_session"]
    return morsel.key, morsel.value


class SignedInClient(starlette.testclient.TestClient):
    """The Starlette client, signed in as `email` on first use (registering the account if it does not exist yet)."""

    sign_in_as = TEST_EMAIL

    def __init__(self, *args, email: str | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._email = email or self.sign_in_as
        self._signed_in = False

    def sign_in(self) -> None:
        if self._signed_in:
            return
        self._signed_in = True  # set first: the calls below go through request() too
        body = {"email": self._email, "password": TEST_PASSWORD}
        r = super().request("POST", "/api/auth/register", json={**body, "accepts_no_advice": True})
        if r.status_code == 409:
            r = super().request("POST", "/api/auth/login", json=body)
        assert r.status_code in (200, 201), r.text
        name, value = _session_cookie(r)
        self.cookies.clear()
        self.cookies.set(name, value)
        self.headers["X-CSRF-Token"] = r.json()["csrf_token"]
        super().request("GET", "/api/pending")  # makes sure this user's desk exists

    def request(self, method, url, **kwargs):  # noqa: D102
        if not self._signed_in:
            self.sign_in()
        return super().request(method, url, **kwargs)

    def websocket_connect(self, url, *args, **kwargs):  # noqa: D102
        if not self._signed_in:
            self.sign_in()
        return super().websocket_connect(url, *args, **kwargs)

    @classmethod
    def as_user(cls, app, email: str, **kwargs) -> "SignedInClient":
        return cls(app, email=email, **kwargs)


fastapi.testclient.TestClient = SignedInClient  # test modules import it from here


class SignedInAsyncClient(httpx.AsyncClient):
    """httpx.AsyncClient talking straight to our app (ASGITransport) is signed in on first use. Clients for anything
    else (a fake 021, a mocked Groq) are left alone. Pass sign_in=False to stay anonymous."""

    def __init__(self, *args, sign_in: bool = True, **kwargs):
        transport = kwargs.get("transport")
        app = getattr(transport, "app", None)
        self._our_app = app if (sign_in and isinstance(transport, httpx.ASGITransport) and
                                "workspaces" in getattr(getattr(app, "state", None), "_state", {})) else None
        self._signed_in = False
        self._sign_in_lock = None
        super().__init__(*args, **kwargs)

    async def _sign_in(self) -> None:
        if self._sign_in_lock is None:
            self._sign_in_lock = asyncio.Lock()
        async with self._sign_in_lock:  # concurrent first requests must all wait for the one sign-in
            if self._signed_in:
                return
            body = {"email": TEST_EMAIL, "password": TEST_PASSWORD}
            r = await super().request("POST", "/api/auth/register", json={**body, "accepts_no_advice": True})
            if r.status_code == 409:
                r = await super().request("POST", "/api/auth/login", json=body)
            assert r.status_code in (200, 201), r.text
            name, value = _session_cookie(r)
            self.cookies.clear()
            self.cookies.set(name, value)
            self.headers["X-CSRF-Token"] = r.json()["csrf_token"]
            self._signed_in = True

    async def request(self, method, url, **kwargs):  # noqa: D102
        if self._our_app is not None and not self._signed_in:
            await self._sign_in()
        return await super().request(method, url, **kwargs)


httpx.AsyncClient = SignedInAsyncClient


_original_getattr = State.__getattr__


def _default_workspace(state: dict):
    """The test user's desk, made (but not started) if nobody has signed in yet: older tests build the app and poke at
    its stores before, or without, ever making a request."""
    from app.auth.service import actor_of

    auth, registry = state["auth"], state["workspaces"]
    user = auth.store.user_by_email(TEST_EMAIL)
    if user is None:
        user = auth.store.create_user(TEST_EMAIL, passwords.hash_password(TEST_PASSWORD), "", state["clock"]())
        auth._on_user_created(user, auth.store.count_users() == 1)
    return registry.build(actor_of(user))


def _state_getattr(self, key):
    try:
        return _original_getattr(self, key)
    except AttributeError:
        if key in PER_USER and "workspaces" in self._state:
            all_ws = self._state["workspaces"].all()
            if not all_ws:
                all_ws = [_default_workspace(self._state)]
            if len(all_ws) == 1:
                return getattr(all_ws[0], key)
            raise AttributeError(f"app.state.{key}: {len(all_ws)} workspaces exist; use app.state.workspaces.peek(user_id)")
        raise


State.__getattr__ = _state_getattr
