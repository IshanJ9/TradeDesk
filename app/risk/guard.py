"""Hook for the trader's own risk limits (the overtrading guard).

Every order card passes through `CardService.propose` (stage "preview") and every Approve click through
`ApprovalService._recheck` (stage "approve"), and both ask the guard. Plans ask it for every step, in both
`PlanService.propose` and `PlanService._recheck`, passing `extra_orders`: how many orders the same plan places
before that step, so a 3-step plan counts as 3 orders. A guard can only:
  * add warnings to a card (shown to the trader, not part of the order hash), or
  * block, with a message, for a limit the trader switched on themselves.
It never sends, changes or cancels anything, and it never gives advice.

`NoRiskGuard` is the do-nothing default. The real one lives in app/risk/ and is wired in app/main.py.
"""

from dataclasses import dataclass
from typing import Literal, Protocol

from app.schemas import PendingOrder

Stage = Literal["preview", "approve"]


@dataclass(frozen=True)
class RiskVerdict:
    warnings: tuple[str, ...] = ()
    block: str | None = None  # a message for the trader; set only by a limit they switched on


class RiskGuard(Protocol):
    async def check(self, pending: PendingOrder, stage: Stage, *, extra_orders: int = 0) -> RiskVerdict: ...

    async def zone(self, proposed): ...  # app.cocaptain.zone.ZoneDecision for an order or a plan


class NoRiskGuard:
    async def check(self, pending: PendingOrder, stage: Stage, *, extra_orders: int = 0) -> RiskVerdict:
        return RiskVerdict()

    async def zone(self, proposed):
        from app.cocaptain.zone import ZoneDecision, proposal_totals  # no limits are set, so none can be crossed

        orders, value = proposal_totals(proposed)
        return ZoneDecision(in_zone=False, configured=False, reasons=[], proposed_orders=orders, proposed_value_paise=value)
