"""Risk-owned daily snapshots and separate synthetic history, without changing ActivityStore."""

from datetime import datetime, timedelta, timezone

from app.db import Database
from app.risk.models import DisciplineDay, TodayFacts


class ReportStore:
    def __init__(self, db: Database):
        self._db = db
        db.execute("CREATE TABLE IF NOT EXISTS risk_days (day TEXT NOT NULL, demo INTEGER NOT NULL, "
                   "data TEXT NOT NULL, PRIMARY KEY(day, demo))")
        db.execute("CREATE TABLE IF NOT EXISTS risk_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS risk_observations_v2 (source TEXT NOT NULL, day TEXT NOT NULL, bucket INTEGER NOT NULL, "
                   "at TEXT NOT NULL, risk REAL, portfolio INTEGER NOT NULL, pnl INTEGER NOT NULL, "
                   "PRIMARY KEY(source,day,bucket))")

    def observe(self, facts: TodayFacts, score: int | None, now: datetime, *, source: str = "unknown") -> dict:
        """First successful observation per five-minute UTC bucket; refresh frequency adds no weight."""
        if now.tzinfo is None:
            raise ValueError("Observation time must be timezone-aware")
        day = facts.day.isoformat()
        bucket = int(now.timestamp()) // 300
        self._db.execute("INSERT OR IGNORE INTO risk_observations_v2(source,day,bucket,at,risk,portfolio,pnl) "
                         "VALUES (?,?,?,?,?,?,?)", (source,day,bucket,now.astimezone(timezone.utc).isoformat(),
                                                  score,facts.portfolio_value,facts.pnl_after_charges))
        self._db.execute("DELETE FROM risk_observations_v2 WHERE day < ?", ((facts.day-timedelta(days=365)).isoformat(),))
        rows = self._db.query("SELECT * FROM risk_observations_v2 WHERE source=? AND day=? ORDER BY bucket", (source,day))
        first = rows[0]
        risks = [r["risk"] for r in rows if r["risk"] is not None]
        elapsed = (now-datetime.fromisoformat(first["at"])).total_seconds()
        return dict(average_risk_score=sum(risks)/len(risks) if risks else None, risk_samples=len(risks),
                    first_observed_at=datetime.fromisoformat(first["at"]), last_observed_at=now,
                    opening_portfolio_paise=first["portfolio"], opening_pnl_paise=first["pnl"],
                    observed_return_pct=(100*(facts.pnl_after_charges-first["pnl"])/first["portfolio"]
                                         if first["portfolio"]>0 and elapsed>=300 else None),
                    reentries=facts.reentries, cooling_off_breaches=facts.cooling_off_breaches,
                    intraday_share_pct=facts.intraday_share_pct, consecutive_losses=facts.consecutive_losses)

    def covered(self, start: datetime, end: datetime, *, source: str = "zerotwoone") -> bool:
        """Require an observation in every five-minute slot of a historical pace window."""
        buckets = set(range(int(start.timestamp())//300, int(end.timestamp())//300+1))
        found = {r["bucket"] for r in self._db.query(
            "SELECT bucket FROM risk_observations_v2 WHERE source=? AND bucket>=? AND bucket<=?", (source,min(buckets),max(buckets)))}
        return buckets <= found

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
