"""Durable invitations. A new link ID is a new approval generation."""
from datetime import datetime
from typing import Literal
from uuid import uuid4

from fastapi import HTTPException

from app.cocaptain.actors import Actor, ActorDirectory
from app.cocaptain.events import ReviewHub
from app.schemas import AuditKind, Model


class Link(Model):
    id: str
    owner_id: str
    reviewer_id: str
    status: Literal["INVITED", "ACTIVE", "REVOKED"]
    created_at: datetime
    accepted_at: datetime | None = None
    revoked_at: datetime | None = None


class Pairing:
    def __init__(self, db, directory: ActorDirectory, audit, hub: ReviewHub, clock):
        self.db, self.directory, self.audit, self.hub, self.clock = db, directory, audit, hub, clock
        self.on_revoke = None
        db.execute("""CREATE TABLE IF NOT EXISTS cocaptain_links (
            id TEXT UNIQUE NOT NULL, owner_id TEXT PRIMARY KEY, reviewer_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('INVITED','ACTIVE','REVOKED')),
            created_at TEXT NOT NULL, accepted_at TEXT, revoked_at TEXT,
            CHECK(owner_id <> reviewer_id))""")

    def get(self, owner_id: str) -> Link | None:
        rows = self.db.query("SELECT * FROM cocaptain_links WHERE owner_id=?", (owner_id,))
        return Link(**dict(rows[0])) if rows else None

    def for_actor(self, actor: Actor) -> list[Link]:
        return [Link(**dict(row)) for row in self.db.query(
            "SELECT * FROM cocaptain_links WHERE owner_id=? OR reviewer_id=?", (actor.id, actor.id))]

    def _write(self, link: Link, actor: Actor, action: str):
        self.db.execute("INSERT OR REPLACE INTO cocaptain_links VALUES (?,?,?,?,?,?,?)", (
            link.id, link.owner_id, link.reviewer_id, link.status, link.created_at.isoformat(),
            link.accepted_at.isoformat() if link.accepted_at else None,
            link.revoked_at.isoformat() if link.revoked_at else None))
        self.audit.record(AuditKind.COCAPTAIN, "user", action, subject_id=link.id,
                          data={"actor_id": actor.id, "action": action,
                                "owner_id": link.owner_id, "reviewer_id": link.reviewer_id})
        for actor_id in (link.owner_id, link.reviewer_id):
            self.hub.publish(actor_id, action)
        return link

    def invite(self, actor: Actor, email: str) -> Link:
        reviewer = self.directory.by_email(email)
        if reviewer is None:
            raise HTTPException(404, "No account found for that email")
        if reviewer.id == actor.id:
            raise HTTPException(403, "Your Co-Captain must be a different person")
        previous = self.get(actor.id)
        if previous and previous.status != "REVOKED":
            if previous.reviewer_id == reviewer.id:
                return previous
            raise HTTPException(409, "Revoke the existing invitation or link before inviting someone else")
        return self._write(Link(id=uuid4().hex, owner_id=actor.id, reviewer_id=reviewer.id,
                                status="INVITED", created_at=self.clock()), actor, "invited")

    def accept(self, actor: Actor, owner_id: str, link_id: str) -> Link:
        link = self.get(owner_id)
        if not link or link.id != link_id:
            raise HTTPException(404, "Invitation not found")
        if actor.id == link.owner_id or actor.id != link.reviewer_id:
            raise HTTPException(403, "Only the invited Co-Captain can accept")
        if link.status == "REVOKED":
            raise HTTPException(409, "This invitation was revoked")
        if link.status == "ACTIVE":
            return link
        return self._write(link.model_copy(update={"status": "ACTIVE", "accepted_at": self.clock()}), actor, "accepted")

    def revoke(self, actor: Actor, owner_id: str, link_id: str) -> Link:
        link = self.get(owner_id)
        if not link or link.id != link_id:
            raise HTTPException(404, "Link not found")
        if actor.id not in (link.owner_id, link.reviewer_id):
            raise HTTPException(403, "Only either person in this link can revoke it")
        if link.status == "REVOKED":
            return link
        link = self._write(link.model_copy(update={"status": "REVOKED", "revoked_at": self.clock()}), actor, "revoked")
        if self.on_revoke:
            self.on_revoke(link, actor)
        return link
