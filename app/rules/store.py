"""Durable storage for standing instructions.

The one rule that matters: `mark_fired` is a conditional UPDATE (ACTIVE -> FIRED) and reports
whether *this call* made the change. Whoever gets True fires the rule; everyone else gets False.
That is what makes "fires exactly once" hold across duplicate ticks, retries and restarts.
"""

from __future__ import annotations

from datetime import datetime

from app.db import Database
from app.schemas import Rule, RuleStatus


def _dump(rule: Rule) -> str:
    # trigger_price is computed from the other fields, so it is not stored
    return rule.model_dump_json(exclude={"condition": {"trigger_price"}})


def _load(row) -> Rule:
    rule = Rule.model_validate_json(row["data"])
    fired_at = datetime.fromisoformat(row["fired_at"]) if row["fired_at"] else None
    return rule.model_copy(update={"status": RuleStatus(row["status"]), "fired_at": fired_at})


class RuleStore:
    def __init__(self, db: Database, *, user_id: str):
        self._db = db
        self._user_id = user_id  # every query below filters by it, so one trader's rules are invisible to another
        self._active: dict[str, list[Rule]] | None = None  # cache for the per-tick lookup

    def _invalidate(self) -> None:
        self._active = None

    def add(self, rule: Rule) -> Rule:
        self._db.execute(
            "INSERT INTO rules (id, user_id, kind, status, instrument_key, created_at, fired_at, delivered, data)"
            " VALUES (?,?,?,?,?,?,?,0,?)",
            (
                rule.id,
                self._user_id,
                rule.kind.value,
                rule.status.value,
                rule.condition.instrument_key,
                rule.created_at.isoformat(),
                rule.fired_at.isoformat() if rule.fired_at else None,
                _dump(rule),
            ),
        )
        self._invalidate()
        return rule

    def get(self, rule_id: str) -> Rule | None:
        rows = self._db.query("SELECT * FROM rules WHERE id = ? AND user_id = ?", (rule_id, self._user_id))
        return _load(rows[0]) if rows else None

    def list(self, status: RuleStatus | None = None, limit: int = 200) -> list[Rule]:
        """Newest first."""
        sql, params = "SELECT * FROM rules WHERE user_id = ?", (self._user_id,)
        if status is not None:
            sql, params = sql + " AND status = ?", params + (status.value,)
        rows = self._db.query(sql + " ORDER BY created_at DESC, rowid DESC LIMIT ?", params + (limit,))
        return [_load(r) for r in rows]

    def active_count(self) -> int:
        return self._db.query("SELECT COUNT(*) AS n FROM rules WHERE status = 'ACTIVE' AND user_id = ?", (self._user_id,))[0]["n"]

    def active_for(self, instrument_key: str) -> list[Rule]:
        if self._active is None:
            grouped: dict[str, list[Rule]] = {}
            for rule in self.list(RuleStatus.ACTIVE, limit=10_000):
                grouped.setdefault(rule.condition.instrument_key, []).append(rule)
            self._active = grouped
        return list(self._active.get(instrument_key, []))

    def mark_fired(self, rule_id: str, now: datetime) -> Rule | None:
        """ACTIVE -> FIRED. Returns the rule only to the one caller whose update took effect."""
        cur = self._db.execute(
            "UPDATE rules SET status = 'FIRED', fired_at = ? WHERE id = ? AND status = 'ACTIVE' AND user_id = ?",
            (now.isoformat(), rule_id, self._user_id),
        )
        self._invalidate()
        return self.get(rule_id) if cur.rowcount == 1 else None

    def cancel(self, rule_id: str) -> Rule | None:
        """ACTIVE -> CANCELLED. Returns None if the rule is unknown or no longer active."""
        cur = self._db.execute(
            "UPDATE rules SET status = 'CANCELLED' WHERE id = ? AND status = 'ACTIVE' AND user_id = ?", (rule_id, self._user_id)
        )
        self._invalidate()
        return self.get(rule_id) if cur.rowcount == 1 else None

    def mark_delivered(self, rule_id: str) -> None:
        self._db.execute("UPDATE rules SET delivered = 1 WHERE id = ? AND user_id = ?", (rule_id, self._user_id))

    def undelivered_fired(self) -> list[Rule]:
        rows = self._db.query("SELECT * FROM rules WHERE status = 'FIRED' AND delivered = 0 AND user_id = ? ORDER BY fired_at", (self._user_id,))
        return [_load(r) for r in rows]
