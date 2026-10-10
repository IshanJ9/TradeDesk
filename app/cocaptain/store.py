"""Durable review bindings and human decisions; this module never sends orders.

The service layer must re-evaluate trading constraints before using ready().
All writes are synchronous on the app's single SQLite/event-loop connection.
The unique role row makes repeated clicks idempotent; link IDs distinguish a
revoked pairing from a later invitation to the same person.
"""

import hmac
from datetime import datetime
from typing import Literal

from app.cocaptain.actors import Actor
from app.cocaptain.zone import POLICY_VERSION
from app.schemas import AuditKind, Model


class ReviewError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class Review(Model):
    card_id: str
    kind: Literal["ORDER", "PLAN"]
    owner_id: str
    account_id: str
    order_hash: str
    expires_at: datetime
    policy_version: str
    link_id: str
    reviewer_id: str
    status: Literal["OPEN", "DECLINED", "INVALIDATED"] = "OPEN"
    reason: str = ""


class Decision(Model):
    card_id: str
    approver_id: str
    role: Literal["TRADER", "CO_CAPTAIN"]
    order_hash: str
    policy_version: str
    decision: Literal["APPROVE", "DECLINE"]
    decided_at: datetime


class ReviewStore:
    def __init__(self, db, pairing, audit, clock):
        self.db, self.pairing, self.audit, self.clock = db, pairing, audit, clock
        db.conn.executescript("""
            CREATE TABLE IF NOT EXISTS cocaptain_reviews (
                card_id TEXT PRIMARY KEY, kind TEXT NOT NULL,
                owner_id TEXT NOT NULL, account_id TEXT NOT NULL,
                order_hash TEXT NOT NULL, expires_at TEXT NOT NULL,
                policy_version TEXT NOT NULL, link_id TEXT NOT NULL,
                reviewer_id TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('OPEN','DECLINED','INVALIDATED')),
                reason TEXT NOT NULL,
                CHECK(owner_id <> reviewer_id)
            );
            CREATE TABLE IF NOT EXISTS approvals (
                card_id TEXT NOT NULL, approver_id TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('TRADER','CO_CAPTAIN')),
                order_hash TEXT NOT NULL, policy_version TEXT NOT NULL,
                decision TEXT NOT NULL CHECK(decision IN ('APPROVE','DECLINE')),
                decided_at TEXT NOT NULL,
                UNIQUE(card_id, role)
            );
        """)

    def get(self, card_id: str) -> Review | None:
        rows = self.db.query("SELECT * FROM cocaptain_reviews WHERE card_id=?", (card_id,))
        return Review(**dict(rows[0])) if rows else None

    def decisions(self, card_id: str) -> list[Decision]:
        return [Decision(**dict(row)) for row in self.db.query(
            "SELECT * FROM approvals WHERE card_id=? ORDER BY role", (card_id,))]

    def _event(self, card_id: str, actor_id: str, action: str, **data):
        review = self.get(card_id)
        self.audit.record(AuditKind.COCAPTAIN, "user" if actor_id != "system" else "system",
                          action, subject_id=card_id,
                          data={"actor_id": actor_id, "action": action,
                                "owner_id": review.owner_id if review else None, **data})

    def _active_link(self, review: Review):
        link = self.pairing.get(review.owner_id)
        if (link is None or link.status != "ACTIVE" or link.id != review.link_id
                or link.reviewer_id != review.reviewer_id or link.reviewer_id == review.owner_id):
            raise ReviewError("LINK_REVOKED", "The Co-Captain link is not active. A fresh review is required.")
        return link

    def open(self, *, card_id: str, kind: Literal["ORDER", "PLAN"], owner_id: str,
             account_id: str, order_hash: str, expires_at: datetime) -> Review:
        """Bind a draft, not an approval. The caller supplies trusted ownership."""
        if expires_at <= self.clock():
            raise ReviewError("EXPIRED", "This card expired. Create a fresh card.")
        link = self.pairing.get(owner_id)
        if link is None or link.status != "ACTIVE" or link.reviewer_id == owner_id:
            raise ReviewError("CO_APPROVAL_REQUIRED", "No active Co-Captain is set. Add one in Settings, or wait.")
        review = Review(card_id=card_id, kind=kind, owner_id=owner_id, account_id=account_id,
                        order_hash=order_hash, expires_at=expires_at, policy_version=POLICY_VERSION,
                        link_id=link.id, reviewer_id=link.reviewer_id)
        existing = self.get(card_id)
        if existing:
            if existing != review:
                raise ReviewError("BINDING_CHANGED", "This review changed or was closed. Create a fresh card.")
            return existing
        self.db.execute("INSERT INTO cocaptain_reviews VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
            card_id, kind, owner_id, account_id, order_hash, expires_at.isoformat(),
            POLICY_VERSION, link.id, link.reviewer_id, "OPEN", ""))
        self._event(card_id, "system", "review_created", owner_id=owner_id,
                    reviewer_id=link.reviewer_id, policy_version=POLICY_VERSION, link_id=link.id)
        return review

    def validate(self, card_id: str, *, owner_id: str, account_id: str,
                 order_hash: str, expires_at: datetime, policy_version: str = POLICY_VERSION) -> Review:
        review = self.get(card_id)
        if review is None:
            raise ReviewError("NOT_FOUND", "Review not found.")
        if review.status != "OPEN":
            raise ReviewError("CLOSED", "This review is closed. Nothing is authorized.")
        if self.clock() >= review.expires_at:
            raise ReviewError("EXPIRED", "This card expired. Create a fresh card.")
        if (review.owner_id != owner_id or review.account_id != account_id
                or not hmac.compare_digest(review.order_hash, order_hash)
                or review.expires_at != expires_at or review.policy_version != policy_version
                or policy_version != POLICY_VERSION):
            raise ReviewError("BINDING_CHANGED", "The card does not match this review. Nothing is authorized.")
        self._active_link(review)
        return review

    def decide(self, card_id: str, actor: Actor, decision: Literal["APPROVE", "DECLINE"],
               *, owner_id: str, account_id: str, order_hash: str, expires_at: datetime) -> Decision:
        if decision not in ("APPROVE", "DECLINE"):
            raise ReviewError("INVALID_DECISION", "Use Approve or Decline.")
        # A caller cannot choose its own role or approve an arbitrary owner's card.
        review = self.get(card_id)
        if review is None:
            raise ReviewError("NOT_FOUND", "Review not found.")
        if actor.id == review.owner_id:
            role = "TRADER"
        elif actor.id == review.reviewer_id and actor.id != review.owner_id:
            role = "CO_CAPTAIN"
        else:
            raise ReviewError("FORBIDDEN", "Only the trader or their invited Co-Captain can decide.")
        # Decline duplicates are harmless, but still require a current link and binding.
        if review.status == "DECLINED":
            self._active_link(review)
            if (owner_id != review.owner_id or account_id != review.account_id
                    or not hmac.compare_digest(order_hash, review.order_hash)
                    or expires_at != review.expires_at or self.clock() >= review.expires_at):
                raise ReviewError("BINDING_CHANGED", "This click does not match the review.")
            previous = next((d for d in self.decisions(card_id) if d.role == role), None)
            if previous and previous.decision == decision:
                return previous
            raise ReviewError("CLOSED", "This review was declined.")
        self.validate(card_id, owner_id=owner_id, account_id=account_id,
                      order_hash=order_hash, expires_at=expires_at)
        previous = next((d for d in self.decisions(card_id) if d.role == role), None)
        if previous:
            if previous.decision != decision:
                raise ReviewError("ALREADY_DECIDED", "This decision was already recorded.")
            return previous
        result = Decision(card_id=card_id, approver_id=actor.id, role=role, order_hash=order_hash,
                          policy_version=POLICY_VERSION, decision=decision, decided_at=self.clock())
        # Atomic decision/status update; no await can interleave a revoke or another click.
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute("INSERT INTO approvals VALUES (?,?,?,?,?,?,?)", (
                card_id, actor.id, role, order_hash, POLICY_VERSION, decision, result.decided_at.isoformat()))
            if decision == "DECLINE":
                self.db.execute("UPDATE cocaptain_reviews SET status='DECLINED', reason=? WHERE card_id=?",
                                ("The card was declined. Nothing was sent.", card_id))
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        self._event(card_id, actor.id, f"{role.lower()}_{decision.lower()}",
                    order_hash=order_hash, policy_version=POLICY_VERSION, link_id=review.link_id)
        return result

    def ready(self, card_id: str, **bindings) -> bool:
        """Necessary but NOT sufficient for sending: trading checks remain required."""
        review = self.validate(card_id, **bindings)
        decisions = {d.role: d for d in self.decisions(card_id)}
        return all(
            role in decisions and decisions[role].decision == "APPROVE"
            and decisions[role].approver_id == expected
            and hmac.compare_digest(decisions[role].order_hash, review.order_hash)
            and decisions[role].policy_version == review.policy_version
            for role, expected in (("TRADER", review.owner_id), ("CO_CAPTAIN", review.reviewer_id))
        )

    def invalidate(self, card_id: str, reason: str, actor_id: str = "system") -> None:
        changed = self.db.execute(
            "UPDATE cocaptain_reviews SET status='INVALIDATED', reason=? WHERE card_id=? AND status='OPEN'",
            (reason, card_id)).rowcount
        if changed:
            self._event(card_id, actor_id, "review_invalidated", reason=reason)

    def revoke_link(self, link, actor: Actor):
        for row in self.db.query("SELECT card_id FROM cocaptain_reviews WHERE link_id=? AND status='OPEN'", (link.id,)):
            self.invalidate(row["card_id"], "The Co-Captain link was revoked. Create a fresh review card.", actor.id)
