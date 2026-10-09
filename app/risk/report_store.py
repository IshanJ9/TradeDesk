"""Risk-owned daily snapshots and separate synthetic history, without changing ActivityStore."""

from app.db import Database
from app.risk.models import DisciplineDay


class ReportStore:
    def __init__(self, db: Database):
        self._db = db
        db.execute("CREATE TABLE IF NOT EXISTS risk_days (day TEXT NOT NULL, demo INTEGER NOT NULL, "
                   "data TEXT NOT NULL, PRIMARY KEY(day, demo))")
        db.execute("CREATE TABLE IF NOT EXISTS risk_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")

    def save_day(self, day: DisciplineDay) -> None:
        self._db.execute("INSERT INTO risk_days(day, demo, data) VALUES (?, ?, ?) "
                         "ON CONFLICT(day, demo) DO UPDATE SET data=excluded.data",
                         (day.day.isoformat(), int(day.demo), day.model_dump_json()))

    def days(self, limit: int = 3650) -> list[DisciplineDay]:
        return [DisciplineDay.model_validate_json(row["data"]) for row in self._db.query(
            "SELECT data FROM risk_days ORDER BY day DESC, demo ASC LIMIT ?", (limit,))]

    def seed_attempted(self) -> bool:
        return bool(self._db.query("SELECT value FROM risk_meta WHERE key='demo_seed_attempted'"))

    def mark_seed_attempted(self) -> None:
        self._db.execute("INSERT OR REPLACE INTO risk_meta(key,value) VALUES ('demo_seed_attempted','1')")

    def clear_demo(self) -> None:
        self._db.execute("DELETE FROM risk_days WHERE demo=1")
        self.mark_seed_attempted()  # A subsequent refresh/restart must not silently seed again.
