"""Pairing routes: who is whose Co-Captain. Every route needs a signed-in user (the same guard as the rest of the app),
and the person acting is always the session's user, never anything in the request.

Anyone with an account can invite one other account by email, and can be invited. The review routes (the Co-Captain's
inbox, Approve and Decline) are in review_api.py.
"""

from fastapi import HTTPException, Request
from pydantic import Field

from app.auth.deps import CurrentUser, protected_router
from app.cocaptain.pairing import Link
from app.schemas import Model

router = protected_router(prefix="/api/cocaptain")


def feature(request: Request):
    if not request.app.state.settings.cocaptain_enabled:
        raise HTTPException(404, "Co-Captain is not enabled")
    return request.app.state.cocaptain_pairing


class Invite(Model):
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^\s@]+@[^\s@]+$")


class LinkAction(Model):
    owner_id: str
    link_id: str


class Person(Model):
    id: str
    display_name: str


class PairingStatus(Model):
    actor: Person
    links: list[Link]
    people: list[Person]
    limits_configured: bool


class CoCaptainConfig(Model):
    enabled: bool


@router.get("/config", response_model=CoCaptainConfig)
async def config(request: Request, _: CurrentUser):
    return CoCaptainConfig(enabled=request.app.state.settings.cocaptain_enabled)


@router.get("/settings", response_model=PairingStatus)
async def settings(request: Request, actor: CurrentUser):
    pairing = feature(request)
    links = pairing.for_actor(actor)
    ids = {actor.id, *(person for link in links for person in (link.owner_id, link.reviewer_id))}
    people = [Person(id=p.id, display_name=p.label) for i in sorted(ids) if (p := pairing.directory.by_id(i))]
    ws = await request.app.state.workspaces.get(actor)  # this user's own desk: their own saved limits
    return PairingStatus(actor=Person(id=actor.id, display_name=actor.label), links=links, people=people,
                         limits_configured=ws.profile_store.get_profile() is not None)


@router.post("/invite", response_model=Link)
async def invite(body: Invite, request: Request, actor: CurrentUser):
    return feature(request).invite(actor, body.email)


@router.post("/accept", response_model=Link)
async def accept(body: LinkAction, request: Request, actor: CurrentUser):
    return feature(request).accept(actor, body.owner_id, body.link_id)


@router.post("/revoke", response_model=Link)
async def revoke(body: LinkAction, request: Request, actor: CurrentUser):
    return feature(request).revoke(actor, body.owner_id, body.link_id)
