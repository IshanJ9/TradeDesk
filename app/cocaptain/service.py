"""Co-Captain: a second person who must also approve an order, but only when the trader is past limits they set.

This module decides WHETHER a second approval is needed and who may give it. It never sends anything: sending stays
in app/orders/approval.py, behind the same checks as every other order. Two approvals never override a hard stop, a
lock, a price band or the broker; they are one more condition, not a way round the others.

Identity is a plain string for now (`Actor.id`). Until real accounts exist (app/auth, being built separately) the
only actor is the trader, "local"; a second actor can be selected by a header only in demo/dev mode (see api.py).
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from app.audit import AuditLog
from app.cocaptain.store import CoCaptainStore, Link
from app.risk.guard import RiskGuard
from app.schemas import AuditKind

OWNER = "local"  # the one trader until accounts exist

NO_REVIEWER_BLOCK = (
    "You are past a limit you set for yourself and have no Co-Captain, so this order is paused. "
    "Add a Co-Captain in Settings, or wait until you are back inside your limit."
)


@dataclass(frozen=True)
class Actor:
    id: str
    name: str


@dataclass(frozen=True)
class Gate:
    required: bool  # the order also needs the Co-Captain's approval
    blocked: str | None = None  # in the zone with no Co-Captain, and the operator requires one
    reviewer_id: str | None = None
    reasons: tuple[str, ...] = ()
    policy_version: str = ""


class CoCaptainError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class CoCaptainService:
    def __init__(self, store: CoCaptainStore, risk: RiskGuard, audit: AuditLog, clock: Callable[[], datetime],
                 block_without_reviewer: bool = False):
        self.store, self._risk, self._audit, self._clock = store, risk, audit, clock
        self._block_without = block_without_reviewer

    # ---- the question every approval asks ------------------------------------------------------- #

    async def gate(self, *, orders: int, value: int, owner: str = OWNER) -> Gate:
        zone = await self._risk.zone(orders=orders, value=value)
        if not zone.in_zone:
            return Gate(required=False, policy_version=zone.policy_version)
        link = self.store.active_link(owner)
        if link is None:  # Co-Captain is opt-in: without one, the trader's own limits stay warnings
            return Gate(required=False, blocked=NO_REVIEWER_BLOCK if self._block_without else None,
                        reasons=zone.reasons, policy_version=zone.policy_version)
        return Gate(required=True, reviewer_id=link.reviewer_id, reasons=zone.reasons, policy_version=zone.policy_version)

    def reviewer_of(self, owner: str = OWNER) -> str | None:
        link = self.store.active_link(owner)
        return link.reviewer_id if link else None

    def may_review(self, actor_id: str, owner: str = OWNER) -> bool:
        """Only the trader's ACTIVE Co-Captain, and never the trader themselves."""
        link = self.store.active_link(owner)
        return link is not None and link.reviewer_id == actor_id and actor_id != owner

    def co_decision_counts(self, card_id: str, order_hash: str, policy_version: str, owner: str = OWNER) -> bool:
        """The Co-Captain's APPROVE still stands: same exact card, same rule version, same person, link still active."""
        row = self.store.decision(card_id, "CO_CAPTAIN")
        link = self.store.active_link(owner)
        return bool(row and link and row["decision"] == "APPROVE" and row["approver_id"] == link.reviewer_id
                    and row["order_hash"] == order_hash and row["policy_version"] == policy_version
                    and row["decided_at"] >= (link.accepted_at or ""))  # given under THIS pairing, not an earlier one

    # ---- the pairing ---------------------------------------------------------------------------- #

    def status(self, actor: Actor) -> dict:
        mine = self.store.open_link(actor.id)
        invite = self.store.invitation_for(actor.id)
        return {
            "me": actor.id,
            "as_trader": None if mine is None else {"reviewer": mine.reviewer_id, "status": mine.status},
            "invitation_from": invite.owner_id if invite else None,
            "reviewing": [link.owner_id for link in self.store.reviewing(actor.id)],
            "blocks_without_reviewer": self._block_without,
        }

    def invite(self, actor: Actor, reviewer_id: str) -> Link:
        reviewer = reviewer_id.strip().lower()
        if not reviewer:
            raise CoCaptainError("Say who your Co-Captain is.")
        if reviewer == actor.id.lower():
            raise CoCaptainError("Your Co-Captain has to be someone else.")
        if self.store.open_link(actor.id):
            raise CoCaptainError("You already have a Co-Captain or an open invitation. Remove it first.")
        link = self.store.invite(actor.id, reviewer, self._clock())
        self._audit.record(AuditKind.COCAPTAIN, "user", f"Invited {reviewer} as Co-Captain", data={"by": actor.id, "reviewer": reviewer})
        return link

    def accept(self, actor: Actor) -> Link:
        invite = self.store.invitation_for(actor.id)
        if invite is None:
            raise CoCaptainError("There is no invitation waiting for you.")
        self.store.accept(invite.owner_id, self._clock())
        self._audit.record(AuditKind.COCAPTAIN, "user", f"{actor.id} accepted as Co-Captain for {invite.owner_id}", data={"by": actor.id, "owner": invite.owner_id})
        return self.store.active_link(invite.owner_id)  # type: ignore[return-value]

    def revoke(self, actor: Actor) -> bool:
        """Either side can end it, at once. Approvals the old Co-Captain gave stop counting (see co_decision_counts)."""
        done = self.store.revoke(actor.id, self._clock())
        for link in self.store.reviewing(actor.id) + ([i] if (i := self.store.invitation_for(actor.id)) else []):
            done = self.store.revoke(link.owner_id, self._clock()) or done
        if done:
            self._audit.record(AuditKind.COCAPTAIN, "user", "Co-Captain link ended", data={"by": actor.id})
        return done
