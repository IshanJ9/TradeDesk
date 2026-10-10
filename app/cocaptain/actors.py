"""One identity dependency for HTTP and websocket, replaceable by real login.

This module creates no users, sessions or passwords. The dev directory contains
only explicitly configured test identities and is never an authentication system.
"""

import inspect
import json
from typing import Protocol

from fastapi import Depends, HTTPException
from starlette.requests import HTTPConnection
from pydantic import Field

from app.schemas import Model


class Actor(Model):
    id: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_.-]+$")
    display_name: str = Field(min_length=1, max_length=100)


class ActorDirectory(Protocol):
    def by_email(self, email: str) -> Actor | None: ...
    def by_id(self, actor_id: str) -> Actor | None: ...


class DevDirectory:
    def __init__(self, raw: str):
        try:
            rows = json.loads(raw)
            if not isinstance(rows, list):
                raise ValueError()
            self._actors = {row["id"]: Actor(id=row["id"], display_name=row["display_name"]) for row in rows}
            self._emails = {row["email"].strip().casefold(): row["id"] for row in rows}
            if len(self._actors) != len(rows) or len(self._emails) != len(rows):
                raise ValueError()
            if any("@" not in email or "\n" in email or "\r" in email for email in self._emails):
                raise ValueError()
        except (ValueError, KeyError, TypeError, AttributeError):
            raise ValueError("Invalid Co-Captain dev directory configuration") from None

    def by_id(self, actor_id: str) -> Actor | None:
        return self._actors.get(actor_id)

    def by_email(self, email: str) -> Actor | None:
        return self.by_id(self._emails.get(email.strip().casefold(), ""))


async def current_actor(connection: HTTPConnection) -> Actor:
    """Akash's session adapter belongs here; all Co-Captain routes depend on it."""
    settings = connection.app.state.settings
    if not settings.cocaptain_enabled:
        # Existing single-user endpoints retain their pre-integration behavior.
        # Co-Captain endpoints refuse access when the feature is disabled.
        return Actor(id="legacy", display_name="Trader")
    resolver = getattr(connection.app.state, "actor_resolver", None)
    if resolver is not None:
        actor = resolver(connection)
        actor = await actor if inspect.isawaitable(actor) else actor
        if not isinstance(actor, Actor):
            raise HTTPException(401, "Sign in to continue")
        return actor
    if not (settings.cocaptain_dev_actors and settings.demo_mode):
        raise HTTPException(503, "Co-Captain requires the real login adapter")
    actor_id = connection.headers.get("x-tradedesk-actor", "")
    if connection.scope["type"] == "websocket" and not actor_id:
        # Browser WebSocket cannot set arbitrary headers; dev IDs travel in the
        # Sec-WebSocket-Protocol header under the exact same dev-only guard.
        protocols = connection.scope.get("subprotocols", [])
        if len(protocols) == 2 and protocols[0] == "tradedesk-cocaptain":
            actor_id = protocols[1]
    actor = connection.app.state.cocaptain_directory.by_id(actor_id)
    if actor is None:
        raise HTTPException(401, "Choose a configured dev actor")
    return actor


async def require_account_owner(connection: HTTPConnection, actor: Actor = Depends(current_actor)) -> Actor:
    if connection.app.state.settings.cocaptain_enabled:
        if actor.id != connection.app.state.settings.cocaptain_account_owner_id:
            raise HTTPException(403, "Only the trader can access this account; use your Co-Captain inbox")
    return actor
