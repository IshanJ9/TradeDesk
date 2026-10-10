"""The Co-Captain gate: decides WHETHER an order or plan needs a second person, and keeps the review bookkeeping.

Built on the foundation in this package (zone.py, pairing.py, store.py, actors.py). It never sends anything: sending
stays in app/orders/approval.py, behind the same checks as every other order, plus one more fence right before the
broker call. Two approvals never override a hard stop, a lock, a price band or the broker.

The feature is off unless COCAPTAIN_ENABLED is set; off, nothing in the order path changes.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from app.audit import AuditLog
from app.cocaptain.actors import Actor
from app.cocaptain.pairing import Pairing
from app.cocaptain.store import Review, ReviewError, ReviewStore
from app.cocaptain.zone import ZoneDecision
from app.config import Settings
from app.risk.guard import RiskGuard
from app.schemas import OrderAction, PendingOrder, Plan

NO_REVIEWER_BLOCK = (
    "You are past a limit you set for yourself and have no Co-Captain, so this order is paused. "
    "Add a Co-Captain in Settings, or wait until you are back inside your limit."
)


@dataclass(frozen=True)
class Assessment:
    required: bool  # needs the Co-Captain's approval as well as the trader's
    blocked: str | None = None  # in the zone with no active Co-Captain: nothing may go
    reviewer_id: str | None = None
    zone: ZoneDecision | None = None

    @property
    def reasons(self) -> list[str]:
        return list(self.zone.reasons) if self.zone else []


class CoCaptainGate:
    def __init__(self, settings: Settings, pairing: Pairing, reviews: ReviewStore, risk: RiskGuard, audit: AuditLog,
                 clock: Callable[[], datetime], owner: Actor):
        """One gate per trader's desk: `owner` is that trader, `risk` and `audit` are theirs. The pairing and review
        store are shared (they are the only things two people have in common)."""
        self._settings, self.pairing, self.reviews, self._risk, self._audit, self._clock = settings, pairing, reviews, risk, audit, clock
        self.owner = owner

    # ---- identity ------------------------------------------------------------------------------- #

    @property
    def enabled(self) -> bool:
        return self._settings.cocaptain_enabled

    @property
    def owner_id(self) -> str:
        return self.owner.id

    @property
    def account_id(self) -> str:
        return self.owner.id  # a desk's account changes only by replacing the desk, which rejects its waiting cards

    def name_of(self, user_id: str | None) -> str | None:
        """What to call a person on screen: their name, or their email."""
        if user_id is None:
            return None
        person = self.pairing.directory.by_id(user_id)
        return person.label if person else user_id

    def bindings(self, p: PendingOrder | Plan) -> dict:
        """What a review is bound to: the exact card (or plan), its account, and its expiry."""
        fingerprint = p.plan_hash if isinstance(p, Plan) else p.order_hash
        return dict(owner_id=self.owner_id, account_id=self.account_id, order_hash=fingerprint, expires_at=p.expires_at)

    # ---- the question every approval asks -------------------------------------------------------- #

    async def assess(self, proposed: PendingOrder | Plan) -> Assessment:
        """Is this order (or plan) past a limit the trader set? If so, does it have a Co-Captain to go to?"""
        if not self.enabled or (isinstance(proposed, PendingOrder) and proposed.action is not OrderAction.PLACE):
            return Assessment(required=False)  # changing or cancelling an existing order is never a new order
        zone = await self._risk.zone(proposed)
        if not zone.in_zone:
            return Assessment(required=False, zone=zone)
        link = self.pairing.get(self.owner_id)
        if link is None or link.status != "ACTIVE":
            return Assessment(required=False, blocked=NO_REVIEWER_BLOCK, zone=zone)
        return Assessment(required=True, reviewer_id=link.reviewer_id, zone=zone)

    # ---- reviews -------------------------------------------------------------------------------- #

    def open_review(self, p: PendingOrder | Plan) -> Review:
        b = self.bindings(p)
        return self.reviews.open(card_id=p.id, kind="PLAN" if isinstance(p, Plan) else "ORDER", **b)

    def is_reviewer_of(self, actor: Actor, p: PendingOrder | Plan) -> bool:
        review = self.reviews.get(p.id)
        return bool(review and review.status == "OPEN" and actor.id == review.reviewer_id and actor.id != review.owner_id)

    def link_holds(self, card_id: str) -> bool:
        """The review is still open and the pairing it was made under is still active. Unlike `ready` this ignores
        the card's expiry, so a plan that is already running is only stopped by the pairing ending, never by the clock."""
        review = self.reviews.get(card_id)
        if review is None or review.status != "OPEN":
            return False
        try:
            self.reviews._active_link(review)
        except ReviewError:
            return False
        return True

    def ready(self, p: PendingOrder | Plan) -> bool:
        """Both people have approved this exact card under an active link. Necessary, never sufficient to send."""
        try:
            return self.reviews.ready(p.id, **self.bindings(p))
        except ReviewError:
            return False

    def close(self, card_id: str, reason: str) -> None:
        """The card is no longer going anywhere (declined, expired, voided, re-quoted): its review stops counting."""
        if self.enabled:
            self.reviews.invalidate(card_id, reason)
