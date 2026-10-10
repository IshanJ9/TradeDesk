"""The Co-Captain's side of the approval: the inbox, and Approve / Decline on a card sent to them.

Only ever the person a trader invited and who accepted; the trader themselves, a stranger and a revoked reviewer all
get the same 404 as a card that doesn't exist. A decision is a deliberate POST from the app: nothing here changes
state on a GET, and incoming websocket text never decides anything.
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import Field

from app.api_models import ApprovalConflict, ExecutionResult
from app.cocaptain.actors import Actor, current_actor
from app.orders.approval import ApprovalError, ApprovalNotFound
from app.schemas import Model, PendingOrder, PendingState

router = APIRouter(prefix="/api/cocaptain")


class ReviewRequest(Model):
    order_hash: str = Field(min_length=64, max_length=64)


def _enabled(request: Request):
    if not request.app.state.settings.cocaptain_enabled:
        raise HTTPException(404, "Co-Captain is not enabled")
    return request.app.state.cocaptain


def _conflict(err: ApprovalError) -> JSONResponse:
    body = ApprovalConflict(code=err.code, message=err.message, pending=err.pending)
    return JSONResponse(status_code=409, content=body.model_dump(mode="json"))


@router.get("/inbox", response_model=list[PendingOrder])
async def inbox(request: Request, actor: Actor = Depends(current_actor)):
    """Cards waiting for THIS person as a Co-Captain: nothing for anyone who isn't one."""
    gate = _enabled(request)
    return [p for p in request.app.state.pending.all()
            if p.state is PendingState.AWAITING_CO_APPROVAL and gate.is_reviewer_of(actor, p)]


@router.post("/cards/{pending_id}/approve", response_model=ExecutionResult,
             responses={404: {"description": "Not a card you may review"}, 409: {"model": ApprovalConflict}})
async def approve(pending_id: str, body: ReviewRequest, request: Request, actor: Actor = Depends(current_actor)):
    _enabled(request)
    try:
        return await request.app.state.approvals.co_approve(pending_id, body.order_hash, actor)
    except ApprovalNotFound:
        raise HTTPException(404, "unknown approval id")
    except ApprovalError as err:
        return _conflict(err)


@router.post("/cards/{pending_id}/decline", response_model=PendingOrder,
             responses={404: {"description": "Not a card you may review"}, 409: {"model": ApprovalConflict}})
async def decline(pending_id: str, request: Request, actor: Actor = Depends(current_actor)):
    _enabled(request)
    try:
        return await request.app.state.approvals.co_decline(pending_id, actor)
    except ApprovalNotFound:
        raise HTTPException(404, "unknown approval id")
    except ApprovalError as err:
        return _conflict(err)
