"""REST routes and the WebSocket endpoint.

Money rule: `POST /api/approvals/{id}/approve` is the only route that can lead to an order
being sent, and it does so only through the ApprovalService. Every other route builds cards,
reads data, or records decisions.
"""

import asyncio

from fastapi import APIRouter, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from app.account import build_account
from app.api_models import (
    AccountSnapshot,
    ApprovalConflict,
    ApproveRequest,
    ChatReply,
    ChatRequest,
    CreateRuleRequest,
    ExecutionResult,
    PendingList,
    PlanApproveRequest,
    ProposePlanRequest,
    ReconcileResult,
    SnapshotEvent,
    WsEvent,
)
from app.auth.deps import authenticate_websocket
from app.broker.base import BrokerTimeout
from app.desk import desk_router
from app.orders.approval import ApprovalError, ApprovalNotFound
from app.plans.service import PlanApprovalError, PlanNotFound
from app.rules.service import RuleNotActive, RuleNotFound
from app.workspace import WorkspaceUnavailable
from app.schemas import AuditEvent, AuditKind, Order, OrderIntent, PendingOrder, Plan, PlanReport, Rule, RuleStatus

rest = desk_router(prefix="/api")
ws_router = APIRouter()


def _pending_list(ws) -> PendingList:
    return PendingList(orders=ws.pending.awaiting_approval(), plans=ws.plan_store.awaiting_approval())


def _conflict(code: str, message: str, pending: PendingOrder | None = None, plan: Plan | None = None) -> JSONResponse:
    body = ApprovalConflict(code=code, message=message, pending=pending, plan=plan)
    return JSONResponse(status_code=409, content=body.model_dump(mode="json"))


# ---- reads --------------------------------------------------------------------- #


@rest.get("/account", response_model=AccountSnapshot)
async def get_account(request: Request):
    return await build_account(request.state.ws.broker)


@rest.get("/orders", response_model=list[Order])
async def get_orders(request: Request):
    return await request.state.ws.broker.get_orders()


@rest.get("/pending", response_model=PendingList)
async def get_pending(request: Request):
    return _pending_list(request.state.ws)


# ---- cards ---------------------------------------------------------------------- #


@rest.post("/orders/preview", response_model=ChatReply)
async def preview_order(intent: OrderIntent, request: Request):
    """Build an order card from a structured intent (the same object the LLM emits).

    Nothing is sent. The card has to be approved separately, and the hash on it is what the
    approve call must echo. Ambiguous or unknown instruments come back as a question.
    """
    return (await request.state.ws.cards.propose(intent)).reply


@rest.post(
    "/approvals/{pending_id}/approve",
    response_model=ExecutionResult,
    responses={
        404: {"description": "Unknown approval id"},
        409: {"model": ApprovalConflict, "description": "Nothing was sent: stale, expired, voided, blocked or re-quoted"},
    },
)
async def approve(pending_id: str, body: ApproveRequest, request: Request):
    try:
        return await request.state.ws.approvals.approve(pending_id, body.order_hash, body.acknowledgment)
    except ApprovalNotFound:
        raise HTTPException(404, "unknown approval id")
    except ApprovalError as err:
        return _conflict(err.code, err.message, err.pending)


@rest.post(
    "/approvals/{pending_id}/reject",
    response_model=PendingOrder,
    responses={404: {"description": "Unknown approval id"}, 409: {"model": ApprovalConflict}},
)
async def reject(pending_id: str, request: Request):
    try:
        return await request.state.ws.approvals.reject(pending_id)
    except ApprovalNotFound:
        raise HTTPException(404, "unknown approval id")
    except ApprovalError as err:
        return _conflict(err.code, err.message, err.pending)


# ---- plans: several orders, approved together ---------------------------------------- #


@rest.post("/plans/preview", response_model=ChatReply)
async def preview_plan(body: ProposePlanRequest, request: Request):
    """Build a plan card from structured steps (the same object the assistant uses). Nothing is sent."""
    return (await request.state.ws.plans.propose(body)).reply


@rest.post(
    "/plans/{plan_id}/approve",
    response_model=PlanReport,
    responses={
        404: {"description": "Unknown plan id"},
        409: {"model": ApprovalConflict, "description": "Nothing was run: stale, expired, voided, blocked or re-quoted"},
    },
)
async def approve_plan(plan_id: str, body: PlanApproveRequest, request: Request):
    """Approve a whole plan. The steps then run in order in the background, each through the same
    executor as a single order; follow progress on the WebSocket or with GET /plans/{id}/report."""
    try:
        return await request.state.ws.plans.approve(plan_id, body.plan_hash)
    except PlanNotFound:
        raise HTTPException(404, "unknown plan id")
    except PlanApprovalError as err:
        return _conflict(err.code, err.message, plan=err.plan)


@rest.post(
    "/plans/{plan_id}/reject",
    response_model=Plan,
    responses={404: {"description": "Unknown plan id"}, 409: {"model": ApprovalConflict}},
)
async def reject_plan(plan_id: str, request: Request):
    try:
        return await request.state.ws.plans.reject(plan_id)
    except PlanNotFound:
        raise HTTPException(404, "unknown plan id")
    except PlanApprovalError as err:
        return _conflict(err.code, err.message, plan=err.plan)


@rest.get("/plans/{plan_id}/report", response_model=PlanReport, responses={404: {"description": "Unknown plan id"}})
async def plan_report(plan_id: str, request: Request):
    """Where each step stands: filled, partly filled, rejected, not sent. Nothing is hidden."""
    try:
        return await request.state.ws.plans.report(plan_id)
    except PlanNotFound:
        raise HTTPException(404, "unknown plan id")


# ---- standing rules ----------------------------------------------------------------- #


@rest.get("/rules", response_model=list[Rule])
async def get_rules(request: Request, status: RuleStatus | None = None):
    """Newest first. A rule fires once; fired and cancelled rules stay listed."""
    return request.state.ws.rule_store.list(status)


@rest.post("/rules", response_model=ChatReply)
async def create_rule(body: CreateRuleRequest, request: Request):
    """Create a standing instruction from structured fields (the same object the assistant uses).

    A rule never sends an order: when it fires it prepares an approval card or an alert.
    """
    return (await request.state.ws.rules.create(body)).reply


@rest.delete("/rules/{rule_id}", response_model=Rule, responses={404: {"description": "Unknown rule"}, 409: {"description": "Rule is not active"}})
async def delete_rule(rule_id: str, request: Request):
    try:
        return await request.state.ws.rules.cancel(rule_id)
    except RuleNotFound:
        raise HTTPException(404, "unknown rule id")
    except RuleNotActive:
        raise HTTPException(409, "this rule has already fired or been cancelled")


# ---- audit and reconcile ---------------------------------------------------------- #


@rest.get("/audit", response_model=list[AuditEvent])
async def get_audit(request: Request, limit: int = Query(200, ge=1, le=1000), kind: AuditKind | None = None):
    """Newest first."""
    return request.state.ws.audit.list(limit=limit, kind=kind)


@rest.get("/audit/export", response_class=Response, responses={200: {"content": {"application/x-ndjson": {}}}})
async def export_audit(request: Request):
    """Downloadable session log: one JSON event per line, oldest first."""
    return Response(
        request.state.ws.audit.export_jsonl(),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": 'attachment; filename="tradedesk-audit.jsonl"'},
    )


@rest.post("/executions/reconcile", response_model=ReconcileResult)
async def reconcile(request: Request):
    """Re-check orders whose outcome was unknown (e.g. after a timeout). Never re-sends."""
    executor = request.state.ws.executor
    resolved = await executor.reconcile()
    return ReconcileResult(resolved=resolved, unresolved=len(executor.unresolved()))


# ---- chat (step 5) ------------------------------------------------------------------ #


@rest.post("/chat", response_model=ChatReply, responses={503: {"description": "Assistant or broker unavailable"}})
async def chat(body: ChatRequest, request: Request):
    """Ask the copilot. Order requests come back as cards; nothing is ever sent from here."""
    return await request.state.ws.copilot.handle(body.message, body.via_voice)


@rest.get("/ws-events", response_model=list[WsEvent], summary="Shape of messages on /ws (documentation only)")
async def ws_events_doc():
    """Never returns data. It exists so the WebSocket message types appear in the OpenAPI
    schema and the generated TypeScript types."""
    return []


# ---- WebSocket ------------------------------------------------------------------- #


@ws_router.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    # Browsers send the session cookie with the upgrade request. No live session (or a foreign Origin): refused
    # before the connection is accepted, so nothing is ever sent to an anonymous client.
    actor = authenticate_websocket(websocket)
    if actor is None:
        await websocket.close(code=1008)
        return
    try:
        ws = await websocket.app.state.workspaces.get(actor)
    except WorkspaceUnavailable:
        await websocket.close(code=1013)
        return
    await websocket.accept()
    hub, broker = ws.hub, ws.broker  # this user's stream: events for anyone else are never put on this queue
    queue = hub.subscribe()  # subscribe first so nothing published during the snapshot is lost
    getter = receiver = None
    try:
        seq0 = hub.seq
        try:
            snapshot = SnapshotEvent(
                seq=seq0,
                account=await build_account(broker),
                pending=_pending_list(ws),
                orders=await broker.get_orders(),
                rules=ws.rule_store.list(limit=100),
            )
        except BrokerTimeout:
            await websocket.close(code=1013)
            return
        await websocket.send_text(snapshot.model_dump_json())

        getter = asyncio.ensure_future(queue.get())
        receiver = asyncio.ensure_future(websocket.receive())  # only to notice a disconnect
        while True:
            done, _ = await asyncio.wait({getter, receiver}, return_when=asyncio.FIRST_COMPLETED)
            if receiver in done:
                if receiver.result()["type"] == "websocket.disconnect":
                    break
                receiver = asyncio.ensure_future(websocket.receive())  # clients send nothing we act on
            if getter in done:
                event = getter.result()
                getter = asyncio.ensure_future(queue.get())
                if event is None:  # we were too slow; reconnecting gives a fresh snapshot
                    await websocket.close(code=1013)
                    break
                if event.seq <= seq0:
                    continue
                await websocket.send_text(event.model_dump_json())
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        for task in (getter, receiver):
            if task is not None:
                task.cancel()
        hub.unsubscribe(queue)
