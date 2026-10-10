"""The guard in front of every non-public route.

`require_user` (aliased `current_user`) is the only way a route learns who is calling. It checks, in order:
1. a session cookie that maps to a live session and an enabled user (else 401);
2. for anything that changes state: an Origin that is ours (when the browser sends one), and the session's CSRF
   token in the `X-CSRF-Token` header (else 403).

CSRF design: synchronizer token. Each session has a random token that the server returns in the body of
login/register/me. The page keeps it in memory and sends it as a header. A malicious site can make the browser
send our cookie, but cannot read our responses (CORS) so it cannot learn the token, and it cannot set a custom
header cross-site without a CORS pre-flight that we only grant to our own origins. SameSite=Lax on the cookie and
the Origin check are a second layer. We chose this over a double-submit cookie because the token lives on the
server, so it cannot be forged by anything that can merely write a cookie.
"""

from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket

from app.auth.service import Authenticated
from app.auth.tokens import same
from app.identity import Actor

SESSION_COOKIE = "tradedesk_session"
CSRF_HEADER = "x-csrf-token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "testserver"})


def cookie_secure(request: Request | WebSocket) -> bool:
    """Secure unless the app is being served from this machine (settings.cookie_secure overrides)."""
    configured = request.app.state.settings.cookie_secure
    if configured is not None:
        return configured
    return (request.url.hostname or "") not in LOCAL_HOSTS


def origin_allowed(request: Request | WebSocket) -> bool:
    """A request that carries an Origin must come from one of ours. Requests without one (not a browser) pass here
    and are still held to the session and the CSRF token."""
    origin = request.headers.get("origin")
    if origin is None:
        return True
    if origin in request.app.state.settings.allowed_origins:
        return True
    host = request.headers.get("host", "")
    parts = urlsplit(origin)
    return bool(host) and parts.netloc == host and parts.scheme in ("http", "https")


async def require_user(request: Request) -> Actor:
    auth: Authenticated | None = request.app.state.auth.authenticate(request.cookies.get(SESSION_COOKIE))
    if auth is None:
        raise HTTPException(401, "Sign in to continue.")
    if request.method not in SAFE_METHODS:
        if not origin_allowed(request):
            raise HTTPException(403, "Cross-site request refused.")
        sent = request.headers.get(CSRF_HEADER, "")
        if not sent or not same(sent, auth.session.csrf_token):
            raise HTTPException(403, "CSRF token missing or wrong. Reload the page.")
    request.state.auth = auth
    request.state.actor = auth.actor
    return auth.actor


current_user = require_user  # the name other code (Co-Captain) should use
CurrentUser = Annotated[Actor, Depends(require_user)]


async def current_auth(request: Request, _: CurrentUser) -> Authenticated:
    """The caller's session as well as the caller (logout and change-password need the session)."""
    return request.state.auth


def protected_router(**kwargs) -> APIRouter:
    """An APIRouter whose every route needs a signed-in user. Use this instead of APIRouter()."""
    return APIRouter(dependencies=[Depends(require_user)], **kwargs)


def authenticate_websocket(websocket: WebSocket) -> Actor | None:
    """For /ws: the cookie must belong to a live session and the Origin must be ours. Returns None to refuse."""
    if not origin_allowed(websocket):
        return None
    auth = websocket.app.state.auth.authenticate(websocket.cookies.get(SESSION_COOKIE))
    return auth.actor if auth else None
