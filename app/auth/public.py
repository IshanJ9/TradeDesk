"""The ONLY list of routes that work without a signed-in user.

Every other route must depend on `app.auth.deps.require_user` (use `protected_router`). tests/test_route_coverage.py
walks the real route table and fails if a route is neither listed here nor protected, so adding a route and
forgetting its guard is caught by the test suite, not by a user.
"""

PUBLIC_HTTP: frozenset[tuple[str, str]] = frozenset({
    ("GET", "/api/health"),
    ("POST", "/api/auth/register"),
    ("POST", "/api/auth/login"),
    # FastAPI's generated API documentation: it describes the routes, it contains no account data.
    ("GET", "/openapi.json"),
    ("GET", "/docs"),
    ("GET", "/docs/oauth2-redirect"),
    ("GET", "/redoc"),
})

PUBLIC_WEBSOCKETS: frozenset[str] = frozenset()  # /ws needs a session too
