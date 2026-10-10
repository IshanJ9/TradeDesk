"""The Co-Captain's side of the approval: the inbox, and Approve / Decline on a card sent to them.

The caller is whoever is signed in. A review names its OWNER (the trader the card belongs to); only the person that
trader invited, who accepted and whose link is still active, may see or decide it. Everyone else (the trader
themselves, a stranger, a former Co-Captain) gets the same 404 as a card that does not exist. This is the one place a
user touches another user's desk, and it reaches only that review's card, through the owner's own approval service.
A decision is a deliberate POST: nothing here changes state on a GET.
"""

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import Field

from app.api_models import ApprovalConflict, ExecutionResult
from app.auth.deps import CurrentUser, protected_router
from app.orders.approval import ApprovalError, ApprovalNotFound
from app.plans.service import PlanApprovalError, PlanNotFound
from app.schemas import Model, PendingOrder, PendingState, Plan, PlanReport, PlanState

router = protected_router(prefix="/api/cocaptain")


class ReviewRequest(Model):
    order_hash: str = Field(min_length=64, max_length=64)


class PlanReviewRequest(Model):
    plan_hash: str = Field(min_length=64, max_length=64)


def _enabled(request: Request) -> None:
    if not request.app.state.settings.cocaptain_enabled:
        raise HTTPException(404, "Co-Captain is not enabled")


async def _owners_desk(request: Request, card_id: str, actor):
    """The workspace of the trader this card belongs to, if (and only if) `actor` is their Co-Captain for it."""
    _enabled(request)
    review = request.app.state.cocaptain_reviews.get(card_id)
    if review is None or review.status != "OPEN" or review.reviewer_id != actor.id or review.owner_id == actor.id:
        raise HTTPException(404, "unknown approval id")
    ws = request.app.state.workspaces.peek(review.owner_id)  # their desk is running whenever they have cards waiting
    if ws is None:
        raise HTTPException(404, "unknown approval id")
    return ws


def _conflict(err: ApprovalError) -> JSONResponse:
    body = ApprovalConflict(code=err.code, message=err.message, pending=err.pending)
    return JSONResponse(status_code=409, content=body.model_dump(mode="json"))


@router.get("/inbox", response_model=list[PendingOrder])
async def inbox(request: Request, actor: CurrentUser):
    """Cards waiting for THIS person as a Co-Captain: nothing for anyone who isn't one."""
    _enabled(request)
    found: list[PendingOrder] = []
    for link in request.app.state.cocaptain_pairing.for_actor(actor):
        if link.reviewer_id != actor.id or link.status != "ACTIVE":
            continue
        ws = request.app.state.workspaces.peek(link.owner_id)
        if ws is None:
            continue
        found += [p for p in ws.pending.all()
                  if p.state is PendingState.AWAITING_CO_APPROVAL and ws.cocaptain.is_reviewer_of(actor, p)]
    return sorted(found, key=lambda p: p.created_at)


@router.post("/cards/{pending_id}/approve", response_model=ExecutionResult,
             responses={404: {"description": "Not a card you may review"}, 409: {"model": ApprovalConflict}})
async def approve(pending_id: str, body: ReviewRequest, request: Request, actor: CurrentUser):
    ws = await _owners_desk(request, pending_id, actor)
    try:
        return await ws.approvals.co_approve(pending_id, body.order_hash, actor)
    except ApprovalNotFound:
        raise HTTPException(404, "unknown approval id")
    except ApprovalError as err:
        return _conflict(err)


@router.post("/cards/{pending_id}/decline", response_model=PendingOrder,
             responses={404: {"description": "Not a card you may review"}, 409: {"model": ApprovalConflict}})
async def decline(pending_id: str, request: Request, actor: CurrentUser):
    ws = await _owners_desk(request, pending_id, actor)
    try:
        return await ws.approvals.co_decline(pending_id, actor)
    except ApprovalNotFound:
        raise HTTPException(404, "unknown approval id")
    except ApprovalError as err:
        return _conflict(err)


# ---- whole plans: the same review, bound to the plan's hash ------------------------------------------------------- #


def _plan_conflict(err: PlanApprovalError) -> JSONResponse:
    body = ApprovalConflict(code=err.code, message=err.message, plan=err.plan)
    return JSONResponse(status_code=409, content=body.model_dump(mode="json"))


@router.get("/plan-inbox", response_model=list[Plan])
async def plan_inbox(request: Request, actor: CurrentUser):
    """Plans waiting for THIS person as a Co-Captain."""
    _enabled(request)
    found: list[Plan] = []
    for link in request.app.state.cocaptain_pairing.for_actor(actor):
        if link.reviewer_id != actor.id or link.status != "ACTIVE":
            continue
        ws = request.app.state.workspaces.peek(link.owner_id)
        if ws is None:
            continue
        found += [p for p in ws.plan_store.all()
                  if p.state is PlanState.AWAITING_CO_APPROVAL and ws.cocaptain.is_reviewer_of(actor, p)]
    return sorted(found, key=lambda p: p.created_at)


@router.post("/plans/{plan_id}/approve", response_model=PlanReport,
             responses={404: {"description": "Not a plan you may review"}, 409: {"model": ApprovalConflict}})
async def approve_plan(plan_id: str, body: PlanReviewRequest, request: Request, actor: CurrentUser):
    ws = await _owners_desk(request, plan_id, actor)
    try:
        return await ws.plans.co_approve(plan_id, body.plan_hash, actor)
    except PlanNotFound:
        raise HTTPException(404, "unknown plan id")
    except PlanApprovalError as err:
        return _plan_conflict(err)


@router.post("/plans/{plan_id}/decline", response_model=Plan,
             responses={404: {"description": "Not a plan you may review"}, 409: {"model": ApprovalConflict}})
async def decline_plan(plan_id: str, request: Request, actor: CurrentUser):
    ws = await _owners_desk(request, plan_id, actor)
    try:
        return await ws.plans.co_decline(plan_id, actor)
    except PlanNotFound:
        raise HTTPException(404, "unknown plan id")
    except PlanApprovalError as err:
        return _plan_conflict(err)
