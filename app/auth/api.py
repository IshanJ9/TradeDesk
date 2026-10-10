"""/api/auth: register, log in, log out, who am I, change password."""

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from app.auth.deps import SESSION_COOKIE, cookie_secure, current_auth, protected_router
from app.auth.models import ChangePasswordRequest, LoginRequest, Me, RegisterRequest, Session
from app.auth.service import AuthError, Authenticated, Issued

public = APIRouter(prefix="/api/auth", tags=["auth"])
private = protected_router(prefix="/api/auth", tags=["auth"])


def _session(issued: Issued, created_at=None) -> Session:
    a = issued.actor
    return Session(user=Me(id=a.id, email=a.email, display_name=a.display_name, created_at=created_at), csrf_token=issued.csrf_token)


def _set_cookie(request: Request, response: Response, issued: Issued) -> None:
    max_age = int((issued.expires_at - request.app.state.clock()).total_seconds())
    response.set_cookie(SESSION_COOKIE, issued.token, max_age=max_age, httponly=True, samesite="lax",
                        secure=cookie_secure(request), path="/")


def _fail(err: AuthError) -> HTTPException:
    headers = {"Retry-After": str(err.retry_after)} if err.retry_after else None
    return HTTPException(err.status, err.message, headers=headers)


def _ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@public.post("/register", response_model=Session, status_code=201)
async def register(body: RegisterRequest, request: Request, response: Response):
    try:
        issued = await request.app.state.auth.register(body.email, body.password, body.display_name, body.accepts_no_advice)
    except AuthError as err:
        raise _fail(err) from None
    _set_cookie(request, response, issued)
    return _session(issued)


@public.post("/login", response_model=Session)
async def login(body: LoginRequest, request: Request, response: Response):
    try:
        issued = await request.app.state.auth.login(body.email, body.password, _ip(request))
    except AuthError as err:
        raise _fail(err) from None
    _set_cookie(request, response, issued)
    return _session(issued)


@private.post("/logout", status_code=204)
async def logout(request: Request, response: Response):
    request.app.state.auth.logout(request.cookies.get(SESSION_COOKIE))
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.status_code = 204
    return response


@private.get("/me", response_model=Session)
async def me(auth: Authenticated = Depends(current_auth)):
    a = auth.actor
    return Session(user=Me(id=a.id, email=a.email, display_name=a.display_name), csrf_token=auth.session.csrf_token)


@private.post("/change-password", response_model=Session)
async def change_password(body: ChangePasswordRequest, request: Request, response: Response,
                          auth: Authenticated = Depends(current_auth)):
    try:
        issued = await request.app.state.auth.change_password(auth, body.current_password, body.new_password)
    except AuthError as err:
        raise _fail(err) from None
    _set_cookie(request, response, issued)
    return _session(issued)
