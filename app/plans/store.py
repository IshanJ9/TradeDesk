"""Plans, their original requests (so a stale plan can be re-quoted) and their live reports.

With a database, every change is written through to SQLite and loaded back at startup, so a plan waiting for
approval survives a restart unchanged (same plan hash). A plan that was RUNNING when the app stopped cannot pick
up where it left off: `PlanService.recover` marks it halted at startup and says why. Steps that had already been
sent are safe (they are in the execution log and the broker's order book); steps not yet sent are never sent.
Without a database (tests) it is in memory only.
"""

from app.api_models import ProposePlanRequest
from app.db import Database
from app.schemas import Plan, PlanReport, PlanState

KEEP = 200  # plans loaded back at startup, newest first

class PlanStore:
    def __init__(self, db: Database | None = None, *, user_id: str) -> None:
        self._db = db
        self._user_id = user_id
        self._plans: dict[str, Plan] = {}
        self._requests: dict[str, ProposePlanRequest] = {}
        self._reports: dict[str, PlanReport] = {}
        if db is not None:
            for row in db.query("SELECT data FROM plans WHERE user_id = ? ORDER BY created_at DESC LIMIT ?", (user_id, KEEP)):
                plan = Plan.model_validate_json(row["data"])
                self._plans[plan.id] = plan
            for row in db.query("SELECT plan_id, data FROM plan_requests WHERE user_id = ?", (user_id,)):
                if row["plan_id"] in self._plans:
                    self._requests[row["plan_id"]] = ProposePlanRequest.model_validate_json(row["data"])
            for row in db.query("SELECT plan_id, data FROM plan_reports WHERE user_id = ?", (user_id,)):
                if row["plan_id"] in self._plans:
                    self._reports[row["plan_id"]] = PlanReport.model_validate_json(row["data"])

    def _write(self, sql: str, params: tuple) -> None:
        if self._db is not None:
            self._db.execute(sql, params)

    def put(self, plan: Plan) -> Plan:
        self._plans[plan.id] = plan
        self._write("INSERT INTO plans (id, user_id, created_at, data) VALUES (?, ?, ?, ?)"
                    " ON CONFLICT(id) DO UPDATE SET data = excluded.data WHERE user_id = excluded.user_id",
                    (plan.id, self._user_id, plan.created_at.isoformat(), plan.model_dump_json(round_trip=True)))
        return plan

    def get(self, plan_id: str) -> Plan | None:
        return self._plans.get(plan_id)

    def all(self) -> list[Plan]:
        return list(self._plans.values())

    def awaiting_approval(self) -> list[Plan]:
        """Plans the trader can still act on, oldest first."""
        return sorted((p for p in self._plans.values() if p.state in (PlanState.PENDING, PlanState.AWAITING_CO_APPROVAL)),
                      key=lambda p: p.created_at)

    def latest(self) -> Plan | None:
        return max(self._plans.values(), key=lambda p: p.created_at, default=None)

    def put_request(self, plan_id: str, request: ProposePlanRequest) -> None:
        self._requests[plan_id] = request
        self._write("INSERT INTO plan_requests (plan_id, user_id, data) VALUES (?, ?, ?)"
                    " ON CONFLICT(plan_id) DO UPDATE SET data = excluded.data WHERE user_id = excluded.user_id",
                    (plan_id, self._user_id, request.model_dump_json(round_trip=True)))

    def request(self, plan_id: str) -> ProposePlanRequest | None:
        return self._requests.get(plan_id)

    def put_report(self, report: PlanReport) -> PlanReport:
        self._reports[report.plan_id] = report
        self._write("INSERT INTO plan_reports (plan_id, user_id, data) VALUES (?, ?, ?)"
                    " ON CONFLICT(plan_id) DO UPDATE SET data = excluded.data WHERE user_id = excluded.user_id",
                    (report.plan_id, self._user_id, report.model_dump_json(round_trip=True)))
        return report

    def report(self, plan_id: str) -> PlanReport | None:
        return self._reports.get(plan_id)
