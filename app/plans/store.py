"""Plans, their original requests (so a stale plan can be re-quoted) and their live reports.

In memory, like pending order cards. A restart loses a plan that was waiting for approval or
still running; steps that had already been sent are safe (they are in the execution log and the
broker's order book), and steps that had not been sent are simply never sent.
"""

from app.api_models import ProposePlanRequest
from app.schemas import Plan, PlanReport, PlanState


class PlanStore:
    def __init__(self) -> None:
        self._plans: dict[str, Plan] = {}
        self._requests: dict[str, ProposePlanRequest] = {}
        self._reports: dict[str, PlanReport] = {}

    def put(self, plan: Plan) -> Plan:
        self._plans[plan.id] = plan
        return plan

    def get(self, plan_id: str) -> Plan | None:
        return self._plans.get(plan_id)

    def all(self) -> list[Plan]:
        return list(self._plans.values())

    def awaiting_approval(self) -> list[Plan]:
        """Plans the trader can still act on, oldest first."""
        return sorted((p for p in self._plans.values() if p.state is PlanState.PENDING), key=lambda p: p.created_at)

    def latest(self) -> Plan | None:
        return max(self._plans.values(), key=lambda p: p.created_at, default=None)

    def put_request(self, plan_id: str, request: ProposePlanRequest) -> None:
        self._requests[plan_id] = request

    def request(self, plan_id: str) -> ProposePlanRequest | None:
        return self._requests.get(plan_id)

    def put_report(self, report: PlanReport) -> PlanReport:
        self._reports[report.plan_id] = report
        return report

    def report(self, plan_id: str) -> PlanReport | None:
        return self._reports.get(plan_id)
