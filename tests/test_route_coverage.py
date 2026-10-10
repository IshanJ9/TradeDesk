"""Every route is either listed as public (app/auth/public.py) or refuses an anonymous caller. Adding a route and
forgetting its guard fails this test. The test walks the app's real route table and then CALLS each route without a
session, so it checks what happens, not how the guard was written. The WebSocket guards itself (it must refuse before
accepting) and is checked the same way."""

import re
from pathlib import Path

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.auth.public import PUBLIC_HTTP, PUBLIC_WEBSOCKETS
from app.config import Settings
from app.main import create_app


def make_app():
    return create_app(Settings(ticker_interval=None, reconcile_interval=None, external_sync_interval=None))


def iter_routes(routes, prefix=""):
    """Every concrete route, however deeply routers were included (FastAPI wraps included routers)."""
    for route in routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:
            yield from iter_routes(inner.routes, prefix + route.include_context.prefix)
        elif hasattr(route, "routes"):
            yield from iter_routes(route.routes, prefix + getattr(route, "path", ""))
        else:
            yield prefix + route.path if not route.path.startswith(prefix) else route.path, route


def http_operations(app) -> list[tuple[str, str]]:
    ops = []
    for path, route in iter_routes(app.routes):
        if getattr(route, "methods", None) is not None:
            ops += [(m, path) for m in sorted(route.methods) if m not in ("HEAD", "OPTIONS")]
    return ops


def websocket_paths(app) -> set[str]:
    return {path for path, route in iter_routes(app.routes) if getattr(route, "methods", "x") is None or type(route).__name__ in
            ("APIWebSocketRoute", "WebSocketRoute")}


def test_the_route_walk_finds_the_routes_it_should():
    ops = set(http_operations(make_app()))
    assert ("POST", "/api/approvals/{pending_id}/approve") in ops and ("GET", "/api/auth/me") in ops and len(ops) > 30


def test_every_http_route_is_public_or_refuses_anonymous_callers():
    app = make_app()
    checked = 0
    with TestClient(app) as anonymous:
        for method, route_path in http_operations(app):
            if (method, route_path) in PUBLIC_HTTP:
                continue
            path = re.sub(r"\{[^}]+\}", "x", route_path)
            r = anonymous.request(method, path, json={} if method in ("POST", "PUT", "PATCH") else None)
            assert r.status_code == 401, f"{method} {route_path} answered {r.status_code} to an anonymous caller"
            checked += 1
    assert checked > 25


def test_a_state_changing_route_without_the_csrf_token_is_refused_even_when_signed_in():
    app = make_app()
    with TestClient(app) as c:
        c.post("/api/auth/register", json={"email": "csrf@example.com", "password": "a long enough password", "accepts_no_advice": True})
        for method, route_path in http_operations(app):
            if method in ("GET",) or (method, route_path) in PUBLIC_HTTP:
                continue
            path = re.sub(r"\{[^}]+\}", "x", route_path)
            r = c.request(method, path, json={})
            assert r.status_code == 403, f"{method} {route_path} answered {r.status_code} without a CSRF token"


def test_the_public_list_has_no_stale_entries_and_exposes_no_account_data():
    ops = set(http_operations(make_app())) | {(m, p) for p, r in iter_routes(make_app().routes) for m in getattr(r, "methods", ()) or ()}
    assert PUBLIC_HTTP <= ops, f"listed as public but no such route: {sorted(PUBLIC_HTTP - ops)}"
    for method, path in PUBLIC_HTTP:  # public means: auth (returns only what you sent), health, generated documentation
        assert path.startswith(("/api/auth/", "/api/health", "/openapi.json", "/docs", "/redoc")), (method, path)


def test_every_websocket_is_known_and_refuses_anonymous_clients():
    app = make_app()
    sockets = websocket_paths(app)
    assert sockets == {"/ws"} | set(PUBLIC_WEBSOCKETS), f"new websocket route {sockets}: guard it, then list it here"
    assert not PUBLIC_WEBSOCKETS
    with TestClient(app) as anonymous:
        for path in sockets:
            with pytest.raises(WebSocketDisconnect):
                with anonymous.websocket_connect(path):
                    pass


def test_application_code_never_reaches_a_users_stuff_through_app_state():
    """Per-user objects live in a Workspace chosen from the session. `app.state.<broker|pending|...>` would be a way
    to reach 'the' desk without asking whose it is (tests have a shim for that; the application must not)."""
    per_user = (r"broker|hub|pending|audit|builder|executor|history|risk|profile_store|discipline|cards|rule_store|rules|"
                r"plan_store|plans|rule_engine|copilot|approvals|order_publisher")
    pattern = re.compile(rf"\bapp\.state\.({per_user})\b|request\.app\.state\.({per_user})\b")
    offenders = [f"{p}:{n}: {line.strip()}" for p in Path("app").rglob("*.py")
                 for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1) if pattern.search(line)]
    assert not offenders, "\n".join(offenders)
