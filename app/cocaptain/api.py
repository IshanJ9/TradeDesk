"""Co-Captain routes: pairing, the reviewer's inbox, and the reviewer's Approve / Decline.

The reviewer approves with a deliberate POST from the app, never from a link: nothing here changes state on a GET.
Who is calling comes from `current_actor`: the trader ("local") unless demo/dev mode is on and an X-Actor header
names someone else, which is how two people are tried before real accounts exist (app/config.py DEV_ACTORS).
"""

from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from app.api_models import ApprovalConflict, ExecutionResult
from app.cocaptain.service import OWNER, Actor, CoCaptainError
from app.orders.approval import ApprovalError, ApprovalNotFound
from app.schemas import Model, PendingOrder, PendingState
from pydantic import Field

router = APIRouter(prefix="/api/cocaptain", tags=["Co-Captain"])


def current_actor(request: Request) -> Actor:
    settings = request.app.state.settings
    named = request.headers.get("x-actor", "").strip().lower()
    if named and settings.dev_actors and settings.demo_mode:  # never in a real deployment
        return Actor(named, named)
    return Actor(OWNER, "You")


class InviteRequest(Model):
    reviewer: str = Field(min_length=1, max_length=120)


class ReviewRequest(Model):
    order_hash: str = Field(min_length=64, max_length=64)


class TraderLink(Model):
    reviewer: str
    status: Literal["INVITED", "ACTIVE"]


class CoCaptainStatus(Model):
    me: str
    as_trader: TraderLink | None = None
    invitation_from: str | None = None
    reviewing: list[str] = []
    blocks_without_reviewer: bool = False


def _service(request: Request):
    return request.app.state.cocaptain


def _conflict(err: ApprovalError) -> JSONResponse:
    body = ApprovalConflict(code=err.code, message=err.message, pending=err.pending)
    return JSONResponse(status_code=409, content=body.model_dump(mode="json"))


@router.get("/status", response_model=CoCaptainStatus)
async def status(request: Request):
    return _service(request).status(current_actor(request))


@router.post("/invite", response_model=CoCaptainStatus)
async def invite(body: InviteRequest, request: Request):
    actor = current_actor(request)
    try:
        _service(request).invite(actor, body.reviewer)
    except CoCaptainError as err:
        raise HTTPException(409, err.message)
    return _service(request).status(actor)


@router.post("/accept", response_model=CoCaptainStatus)
async def accept(request: Request):
    actor = current_actor(request)
    try:
        _service(request).accept(actor)
    except CoCaptainError as err:
        raise HTTPException(409, err.message)
    return _service(request).status(actor)


@router.post("/revoke", response_model=CoCaptainStatus)
async def revoke(request: Request):
    actor = current_actor(request)
    _service(request).revoke(actor)
    return _service(request).status(actor)


@router.get("/inbox", response_model=list[PendingOrder])
async def inbox(request: Request):
    """Cards waiting for THIS person as a Co-Captain: nothing for anyone who isn't one."""
    actor = current_actor(request)
    if not _service(request).may_review(actor.id):
        return []
    return [p for p in request.app.state.pending.all() if p.state is PendingState.AWAITING_CO_APPROVAL]


@router.post("/cards/{pending_id}/approve", response_model=ExecutionResult,
             responses={404: {"description": "Not a card you may review"}, 409: {"model": ApprovalConflict}})
async def approve(pending_id: str, body: ReviewRequest, request: Request):
    actor = current_actor(request)
    try:
        return await request.app.state.approvals.co_approve(pending_id, body.order_hash, actor.id)
    except ApprovalNotFound:
        raise HTTPException(404, "unknown approval id")
    except ApprovalError as err:
        return _conflict(err)


@router.post("/cards/{pending_id}/decline", response_model=PendingOrder,
             responses={404: {"description": "Not a card you may review"}, 409: {"model": ApprovalConflict}})
async def decline(pending_id: str, request: Request):
    actor = current_actor(request)
    try:
        return await request.app.state.approvals.co_decline(pending_id, actor.id)
    except ApprovalNotFound:
        raise HTTPException(404, "unknown approval id")
    except ApprovalError as err:
        return _conflict(err)
