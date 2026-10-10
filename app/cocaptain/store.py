"""Who a trader's Co-Captain is, and who has approved which card. Both are saved, so a restart loses nothing."""

from dataclasses import dataclass
from datetime import datetime

from app.db import Database

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cocaptain_links (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id    TEXT NOT NULL,
    reviewer_id TEXT NOT NULL,
    status      TEXT NOT NULL,           -- INVITED | ACTIVE | REVOKED
    created_at  TEXT NOT NULL,
    accepted_at TEXT,
    revoked_at  TEXT
);
-- one open link (invited or active) per trader
CREATE UNIQUE INDEX IF NOT EXISTS cocaptain_one_open ON cocaptain_links(owner_id) WHERE status IN ('INVITED', 'ACTIVE');
-- one decision per role per card, bound to the card's exact hash and the rule version it was given under
CREATE TABLE IF NOT EXISTS cocaptain_approvals (
    card_id        TEXT NOT NULL,
    role           TEXT NOT NULL,        -- TRADER | CO_CAPTAIN
    approver_id    TEXT NOT NULL,
    order_hash     TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    decision       TEXT NOT NULL,        -- APPROVE | DECLINE
    decided_at     TEXT NOT NULL,
    UNIQUE (card_id, role)
);
"""


@dataclass(frozen=True)
class Link:
    owner_id: str
    reviewer_id: str
    status: str
    created_at: str
    accepted_at: str | None = None
    revoked_at: str | None = None


class CoCaptainStore:
    def __init__(self, db: Database):
        self._db = db
        db.conn.executescript(_SCHEMA)

    # ---- links ---------------------------------------------------------------------------------- #

    @staticmethod
    def _link(row) -> Link:
        return Link(row["owner_id"], row["reviewer_id"], row["status"], row["created_at"], row["accepted_at"], row["revoked_at"])

    def open_link(self, owner_id: str) -> Link | None:
        rows = self._db.query("SELECT * FROM cocaptain_links WHERE owner_id=? AND status IN ('INVITED','ACTIVE')", (owner_id,))
        return self._link(rows[0]) if rows else None

    def active_link(self, owner_id: str) -> Link | None:
        link = self.open_link(owner_id)
        return link if link and link.status == "ACTIVE" else None

    def invitation_for(self, reviewer_id: str) -> Link | None:
        rows = self._db.query("SELECT * FROM cocaptain_links WHERE reviewer_id=? AND status='INVITED' ORDER BY id DESC", (reviewer_id,))
        return self._link(rows[0]) if rows else None

    def reviewing(self, reviewer_id: str) -> list[Link]:
        rows = self._db.query("SELECT * FROM cocaptain_links WHERE reviewer_id=? AND status='ACTIVE'", (reviewer_id,))
        return [self._link(r) for r in rows]

    def invite(self, owner_id: str, reviewer_id: str, now: datetime) -> Link:
        self._db.execute("INSERT INTO cocaptain_links (owner_id, reviewer_id, status, created_at) VALUES (?, ?, 'INVITED', ?)",
                         (owner_id, reviewer_id, now.isoformat()))
        return self.open_link(owner_id)  # type: ignore[return-value]

    def accept(self, owner_id: str, now: datetime) -> None:
        self._db.execute("UPDATE cocaptain_links SET status='ACTIVE', accepted_at=? WHERE owner_id=? AND status='INVITED'",
                         (now.isoformat(), owner_id))

    def revoke(self, owner_id: str, now: datetime) -> bool:
        cur = self._db.execute("UPDATE cocaptain_links SET status='REVOKED', revoked_at=? WHERE owner_id=? AND status IN ('INVITED','ACTIVE')",
                               (now.isoformat(), owner_id))
        return cur.rowcount > 0

    # ---- approvals ------------------------------------------------------------------------------ #

    def record(self, card_id: str, role: str, approver_id: str, order_hash: str, policy_version: str, decision: str,
               now: datetime) -> bool:
        """True if saved; False if this role already decided on this card (a repeat click changes nothing)."""
        cur = self._db.execute(
            "INSERT OR IGNORE INTO cocaptain_approvals (card_id, role, approver_id, order_hash, policy_version, decision, decided_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)", (card_id, role, approver_id, order_hash, policy_version, decision, now.isoformat()))
        return cur.rowcount > 0

    def decision(self, card_id: str, role: str) -> dict | None:
        rows = self._db.query("SELECT * FROM cocaptain_approvals WHERE card_id=? AND role=?", (card_id, role))
        return dict(rows[0]) if rows else None

    def forget_co_captain(self, card_id: str) -> None:
        """The reviewer's decision no longer counts (the link was revoked, or the card changed)."""
        self._db.execute("DELETE FROM cocaptain_approvals WHERE card_id=? AND role='CO_CAPTAIN'", (card_id,))
