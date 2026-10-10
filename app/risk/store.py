"""Durable settings for one trader, using tables owned by the risk-goals feature."""

from datetime import datetime, timedelta

from app.db import Database
from app.risk.models import Goal, RiskProfile, TodayFacts


class ProfileStore:
    def __init__(self, db: Database, *, user_id: str):
        self._db = db
        self._user_id = user_id

    def get_profile(self) -> RiskProfile | None:
        rows = self._db.query("SELECT data FROM risk_profile WHERE user_id = ?", (self._user_id,))
        return RiskProfile.model_validate_json(rows[0]["data"]) if rows else None

    def save_profile(self, profile: RiskProfile) -> RiskProfile:
        if not profile.hard_cooling_off:
            self._db.execute("DELETE FROM risk_cooldown WHERE user_id = ?", (self._user_id,))
        self._db.execute(
            "INSERT INTO risk_profile (user_id, data) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET data = excluded.data",
            (self._user_id, profile.model_dump_json()),
        )
        return profile

    def cooldown_until(self, profile: RiskProfile, facts: TodayFacts, now: datetime) -> datetime | None:
        """Latch an observed opt-in pause, including across restart/day rollover.

        An existing pause is never shortened by a settings change. Switching the
        stop off clears it; subsequent observed qualifying losses can start one.
        """
        if not profile.hard_cooling_off:
            return None
        rows = self._db.query("SELECT until_at FROM risk_cooldown WHERE user_id = ?", (self._user_id,))
        until = datetime.fromisoformat(rows[0]["until_at"]) if rows else None
        if (facts.consecutive_losses >= profile.cooling_off_after_losses and
                facts.last_loss_at is not None and facts.last_loss_at <= now):
            candidate = facts.last_loss_at + timedelta(minutes=profile.cooling_off_minutes)
            if candidate > now and (until is None or candidate > until):
                until = candidate
                self._db.execute("INSERT INTO risk_cooldown (user_id, until_at) VALUES (?, ?) "
                                 "ON CONFLICT(user_id) DO UPDATE SET until_at = excluded.until_at", (self._user_id, until.isoformat()))
        if until is not None and until > now:
            return until
        self._db.execute("DELETE FROM risk_cooldown WHERE user_id = ?", (self._user_id,))
        return None

    def get_goal(self) -> Goal | None:
        rows = self._db.query("SELECT data FROM risk_goal WHERE user_id = ?", (self._user_id,))
        return Goal.model_validate_json(rows[0]["data"]) if rows else None

    def save_goal(self, goal: Goal) -> Goal:
        self._db.execute(
            "INSERT INTO risk_goal (user_id, data) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET data = excluded.data",
            (self._user_id, goal.model_dump_json()),
        )
        return goal

    def delete_goal(self) -> None:
        self._db.execute("DELETE FROM risk_goal WHERE user_id = ?", (self._user_id,))
