"""Durable settings for one trader, using tables owned by the risk-goals feature."""

from app.db import Database
from app.risk.models import Goal, RiskProfile


class ProfileStore:
    def __init__(self, db: Database):
        self._db = db
        for table in ("risk_profile", "risk_goal"):
            db.execute(f"CREATE TABLE IF NOT EXISTS {table} (id INTEGER PRIMARY KEY CHECK(id = 1), data TEXT NOT NULL)")

    def get_profile(self) -> RiskProfile | None:
        rows = self._db.query("SELECT data FROM risk_profile WHERE id = 1")
        return RiskProfile.model_validate_json(rows[0]["data"]) if rows else None

    def save_profile(self, profile: RiskProfile) -> RiskProfile:
        self._db.execute(
            "INSERT INTO risk_profile (id, data) VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET data = excluded.data",
            (profile.model_dump_json(),),
        )
        return profile

    def get_goal(self) -> Goal | None:
        rows = self._db.query("SELECT data FROM risk_goal WHERE id = 1")
        return Goal.model_validate_json(rows[0]["data"]) if rows else None

    def save_goal(self, goal: Goal) -> Goal:
        self._db.execute(
            "INSERT INTO risk_goal (id, data) VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET data = excluded.data",
            (goal.model_dump_json(),),
        )
        return goal

    def delete_goal(self) -> None:
        self._db.execute("DELETE FROM risk_goal WHERE id = 1")
