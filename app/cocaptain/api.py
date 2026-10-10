import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import Field

from app.cocaptain.actors import Actor, current_actor
from app.cocaptain.pairing import Link
from app.schemas import Model

router = APIRouter(prefix="/api/cocaptain")


def feature(request: Request):
    if not request.app.state.settings.cocaptain_enabled:
        raise HTTPException(404, "Co-Captain is not enabled")
    return request.app.state.cocaptain_pairing


class Invite(Model):
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^\s@]+@[^\s@]+$")


class LinkAction(Model):
    owner_id: str
    link_id: str


class PairingStatus(Model):
    actor: Actor
    account_owner_id: str
    links: list[Link]
    people: list[Actor]
    limits_configured: bool
    dev_actors: bool


@router.get("/config")
async def config(request: Request):
    s = request.app.state.settings
    return {"enabled": s.cocaptain_enabled, "dev_actors": s.cocaptain_dev_actors and s.demo_mode}


@router.get("/settings", response_model=PairingStatus)
async def settings(request: Request, actor: Actor = Depends(current_actor)):
    pairing = feature(request)
    links = pairing.for_actor(actor)
    ids = {actor.id, *(person for link in links for person in (link.owner_id, link.reviewer_id))}
    profile = request.app.state.profile_store.get_profile()
    return PairingStatus(actor=actor, account_owner_id=request.app.state.settings.cocaptain_account_owner_id,
                         links=links, people=[person for id in sorted(ids) if (person := pairing.directory.by_id(id))],
                         limits_configured=profile is not None, dev_actors=request.app.state.settings.cocaptain_dev_actors)


@router.post("/invite", response_model=Link)
async def invite(body: Invite, request: Request, actor: Actor = Depends(current_actor)):
    pairing = feature(request)
    if actor.id != request.app.state.settings.cocaptain_account_owner_id:
        raise HTTPException(403, "Only the trader can invite a reviewer for this account")
    return pairing.invite(actor, body.email)


@router.post("/accept", response_model=Link)
async def accept(body: LinkAction, request: Request, actor: Actor = Depends(current_actor)):
    return feature(request).accept(actor, body.owner_id, body.link_id)


@router.post("/revoke", response_model=Link)
async def revoke(body: LinkAction, request: Request, actor: Actor = Depends(current_actor)):
    return feature(request).revoke(actor, body.owner_id, body.link_id)


@router.websocket("/ws")
async def events(websocket: WebSocket, actor: Actor = Depends(current_actor)):
    if not websocket.app.state.settings.cocaptain_enabled:
        await websocket.close(code=1008)
        return
    protocol = "tradedesk-cocaptain" if "tradedesk-cocaptain" in websocket.scope.get("subprotocols", []) else None
    await websocket.accept(subprotocol=protocol)
    hub = websocket.app.state.cocaptain_hub
    queue = hub.subscribe(actor.id)
    receive = asyncio.create_task(websocket.receive_text())
    getter = None
    try:
        await websocket.send_json({"type": "cocaptain_connected"})
        while True:
            getter = asyncio.create_task(queue.get())
            done, _ = await asyncio.wait({getter, receive}, return_when=asyncio.FIRST_COMPLETED)
            if receive in done:
                # Incoming websocket text never records a decision.
                receive.result()
                receive = asyncio.create_task(websocket.receive_text())
            if getter in done:
                event = getter.result()
                if event is None:
                    await websocket.close(code=1013)
                    break
                await websocket.send_json(event)
            else:
                getter.cancel()
                await asyncio.gather(getter, return_exceptions=True)
    except WebSocketDisconnect:
        pass
    finally:
        hub.unsubscribe(actor.id, queue)
        for task in (getter, receive):
            if task:
                task.cancel()
        await asyncio.gather(*(t for t in (getter, receive) if t), return_exceptions=True)
