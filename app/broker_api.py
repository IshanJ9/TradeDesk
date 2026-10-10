"""Link, unlink and reconnect the signed-in user's own 021 account.

The login is checked by really logging in (once: 021 allows one session per account, and the session we open is the one
the desk then uses), then stored encrypted (app/vault.py). Nothing here returns, logs or audits the username or the
password; the API only says whether an account is linked and the last four characters of its client id."""

import asyncio
import logging
from typing import Literal

from fastapi import HTTPException, Request
from pydantic import Field

from app.broker.adapter_errors import AuthFailed
from app.broker.base import BrokerError, BrokerTimeout
from app.broker.kind import AccountKind
from app.broker_links import START_TIMEOUT, UccTaken, normalise_ucc
from app.desk import desk_router, ws_of
from app.schemas import Model
from app.vault import VaultCorrupt, VaultUnavailable
from app.workspace import Workspace, WorkspaceUnavailable

log = logging.getLogger("tradedesk.broker_api")
router = desk_router(prefix="/api/broker", tags=["broker"])


class BrokerStatus(Model):
    kind: AccountKind  # mock = a simulated account with no real money; 021 = a real 021 account
    status: Literal["mock", "connected", "needs_reconnect"]
    ucc_hint: str | None  # "…1234": the last characters of the 021 client id, never the id or the password
    server_account: bool  # True: this desk uses the 021 login in the server's .env; it is managed there, not here
    can_link: bool
    link_unavailable_reason: str | None


class LinkRequest(Model):
    username: str = Field(min_length=1, max_length=40, description="Your 021 client id (UCC)")
    password: str = Field(min_length=1, max_length=256)


def _status(request: Request, ws: Workspace) -> BrokerStatus:
    state = request.app.state
    row = state.links.get(ws.user_id)
    server = state.uses_server_account(ws.user)
    reason = None
    if server:
        reason = "This desk uses the 021 login set on the server."
    elif not state.vault.available:
        reason = "Linking isn't set up on this server (TRADEDESK_SECRET_KEY is missing or invalid)."
    hint = f"…{row.ucc_last4}" if row else (f"…{ws.settings.zerotwoone_username[-4:]}" if server and ws.settings.zerotwoone_username else None)
    return BrokerStatus(kind=ws.account_kind, status=ws.broker_status, ucc_hint=hint, server_account=server,
                        can_link=reason is None, link_unavailable_reason=reason)


def _guard(request: Request, ws: Workspace) -> None:
    state = request.app.state
    if state.uses_server_account(ws.user):
        raise HTTPException(409, "This desk uses the 021 login set on the server; it can't be changed here.")
    blocker = ws.switch_blocker()
    if blocker:
        raise HTTPException(409, blocker)


async def _close(adapter) -> None:
    try:
        await adapter.close()
    except Exception:
        pass


@router.get("", response_model=BrokerStatus)
async def broker_status(request: Request):
    return _status(request, ws_of(request))


@router.post("/link", response_model=BrokerStatus)
async def link(body: LinkRequest, request: Request):
    state = request.app.state
    ws = ws_of(request)
    _guard(request, ws)
    if not state.vault.available:
        raise HTTPException(503, "Linking isn't set up on this server (TRADEDESK_SECRET_KEY is missing or invalid).")
    ip = request.client.host if request.client else "unknown"
    wait = state.link_limiter.retry_after(ws.user_id, ip)
    if wait:
        raise HTTPException(429, "Too many attempts. Try again in a few minutes.", headers={"Retry-After": str(wait)})
    ucc = normalise_ucc(body.username)
    taken = state.links.taken_by_other(ucc, ws.user_id) or (
        state.uses_server_account_ucc(ucc))
    if taken:
        raise HTTPException(409, "That 021 account is already in use by another TradeDesk user.")
    adapter = state.zerotwoone_factory(ucc, body.password)
    try:
        await asyncio.wait_for(adapter.start(), START_TIMEOUT)
    except AuthFailed:
        state.link_limiter.failed(ws.user_id, ip)
        await _close(adapter)
        raise HTTPException(422, "021 did not accept those details. Nothing was saved.") from None
    except (BrokerTimeout, asyncio.TimeoutError):
        await _close(adapter)
        raise HTTPException(503, "Couldn't reach 021 to check those details. Nothing was saved.") from None
    except Exception:
        await _close(adapter)
        log.warning("linking a 021 account failed: unexpected error")
        raise HTTPException(502, "Something went wrong while checking those details. Nothing was saved.") from None
    try:
        state.links.save(ws.user_id, ucc, body.password, state.clock())
    except UccTaken:
        await _close(adapter)
        raise HTTPException(409, "That 021 account is already in use by another TradeDesk user.") from None
    except VaultUnavailable:
        await _close(adapter)
        raise HTTPException(503, "Linking isn't set up on this server (TRADEDESK_SECRET_KEY is missing or invalid).") from None
    state.link_limiter.succeeded(ws.user_id, ip)
    ws.note_account_change(f"Linked a 021 account (client id ends …{ucc[-4:]})")
    try:
        new = await state.workspaces.replace(ws.user, adapter, started=True)
    except WorkspaceUnavailable:
        state.links.delete(ws.user_id)
        await _close(adapter)
        raise HTTPException(503, "Your desk couldn't start on that account. Nothing was kept.") from None
    return _status(request, new)


@router.delete("/link", response_model=BrokerStatus)
async def unlink(request: Request):
    state = request.app.state
    ws = ws_of(request)
    _guard(request, ws)
    if state.links.get(ws.user_id) is None:
        raise HTTPException(409, "No 021 account is linked.")
    ws.note_account_change("Unlinked the 021 account; back on the simulated account")
    state.links.delete(ws.user_id)  # the saved login is gone before the desk is rebuilt
    new = await state.workspaces.replace(ws.user)
    return _status(request, new)


@router.post("/reconnect", response_model=BrokerStatus)
async def reconnect(request: Request):
    state = request.app.state
    ws = ws_of(request)
    if ws.broker_status != "needs_reconnect":
        raise HTTPException(409, "Nothing to reconnect.")
    wait = state.link_limiter.retry_after(ws.user_id, request.client.host if request.client else "unknown")
    if wait:
        raise HTTPException(429, "Too many attempts. Try again in a few minutes.", headers={"Retry-After": str(wait)})
    broker = ws.broker
    try:
        if hasattr(broker, "relogin"):  # 021 revoked our token: log in again with the saved login
            await asyncio.wait_for(broker.relogin(), START_TIMEOUT)
            try:
                await asyncio.wait_for(broker.get_funds(), START_TIMEOUT)  # a login that is revoked again at once is not "connected"
            except BrokerError:
                pass  # the flag below says whether 021 revoked it again
            if ws.broker_status == "needs_reconnect":
                raise HTTPException(503, "021 still shows this login as in use somewhere else. Close other copies of the app and try again.")
            ws.note_account_change("Reconnected the 021 account")
            return _status(request, ws)
        new = await state.workspaces.replace(ws.user)  # the session never opened: try opening it again
    except AuthFailed:
        state.link_limiter.failed(ws.user_id, request.client.host if request.client else "unknown")
        raise HTTPException(422, "021 did not accept the saved login. Unlink the account and link it again.") from None
    except (BrokerError, asyncio.TimeoutError):
        raise HTTPException(503, "Couldn't reach 021. Try again in a moment.") from None
    except (VaultUnavailable, VaultCorrupt):
        raise HTTPException(409, "The saved login can't be read (the server key changed). Unlink and link the account again.") from None
    new_status = _status(request, new)
    if new_status.status == "needs_reconnect":
        if state.linked.failure.get(ws.user_id) == "refused":
            raise HTTPException(422, "021 did not accept the saved login. Unlink the account and link it again.")
        raise HTTPException(503, "Couldn't reach 021. Try again in a moment.")
    new.note_account_change("Reconnected the 021 account")
    return new_status
