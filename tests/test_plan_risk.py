"""Plan steps go through the trader's own limits, like single cards, and a plan's steps count as orders."""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.broker.mock import MockBroker
from app.config import Settings
from app.main import create_app
from app.risk.guard import RiskVerdict

NOW = datetime(2026, 10, 8, 5, 0, tzinfo=timezone.utc)
PLAN = {"legs": [{"instrument": "infosys", "side": "SELL", "fraction_of_holding": 0.5},
                 {"instrument": "itc", "side": "BUY", "proceeds_of_leg": 0}]}


class FakeGuard:
    def __init__(self, preview=None, approve=None):
        self.preview, self.approve = preview or {}, approve or {}  # step index -> verdict
        self.calls: list[tuple[str, str, int]] = []

    async def check(self, pending, stage, *, extra_orders=0):
        self.calls.append((stage, pending.instrument.symbol, extra_orders))
        return (self.preview if stage == "preview" else self.approve).get(extra_orders, RiskVerdict())


@pytest.fixture
def env():
    broker = MockBroker(clock=lambda: NOW)
    settings = Settings(ticker_interval=None, reconcile_interval=None, account_push_interval=3600, external_sync_interval=None)
    with TestClient(create_app(settings, broker=broker, clock=lambda: NOW)) as c:
        yield c, broker


def use(c, guard):
    c.app.state.plans._risk = guard


def test_each_step_is_checked_counting_the_steps_before_it(env):
    c, _ = env
    guard = FakeGuard(preview={1: RiskVerdict(warnings=("You set 2 orders a day; this would be order #3.",))})
    use(c, guard)
    plan = c.post("/api/plans/preview", json=PLAN).json()["cards"][0]["plan"]
    assert guard.calls == [("preview", "INFY", 0), ("preview", "ITC", 1)]
    assert "order #3" not in " ".join(plan["legs"][0]["order"]["warnings"])
    assert plan["legs"][1]["order"]["warnings"][0] == "You set 2 orders a day; this would be order #3."


def test_a_hard_limit_on_any_step_makes_no_plan(env):
    c, _ = env
    use(c, FakeGuard(preview={1: RiskVerdict(block="You switched on a hard limit of 2 orders a day.")}))
    reply = c.post("/api/plans/preview", json=PLAN).json()
    assert reply["text"] == "Step 2: You switched on a hard limit of 2 orders a day."
    assert reply["cards"][0]["type"] == "notice"


def test_a_hard_limit_crossed_before_approval_runs_nothing(env):
    c, broker = env
    plan = c.post("/api/plans/preview", json=PLAN).json()["cards"][0]["plan"]
    use(c, FakeGuard(approve={0: RiskVerdict(block="You switched on a hard limit of 2 orders a day.")}))
    r = c.post(f"/api/plans/{plan['id']}/approve", json={"plan_hash": plan["plan_hash"]})
    assert r.status_code == 409 and r.json()["code"] == "BLOCKED"
    assert "Step 1: You switched on a hard limit" in r.json()["message"]
    assert broker._orders == {}


def test_warnings_do_not_change_what_is_approved(env):
    c, _ = env
    use(c, FakeGuard())
    plain = c.post("/api/plans/preview", json=PLAN).json()["cards"][0]["plan"]
    use(c, FakeGuard(preview={0: RiskVerdict(warnings=("a warning",))}))
    warned = c.post("/api/plans/preview", json=PLAN).json()["cards"][0]["plan"]
    strip = lambda p: [(leg["order"]["side"], leg["order"]["quantity"], leg["order"]["instrument"]["symbol"]) for leg in p["legs"]]
    assert strip(plain) == strip(warned)


def test_the_real_guard_counts_plan_steps_toward_orders_a_day(env):
    c, _ = env
    base = c.get("/api/profile/presets").json()[0]["profile"] if c.get("/api/profile/presets").status_code == 200 else None
    profile = dict(base or {}, max_orders_per_day=1, hard_order_limit=True)
    assert c.put("/api/profile", json=profile).status_code == 200
    reply = c.post("/api/plans/preview", json=PLAN).json()
    assert reply["cards"][0]["type"] == "notice" and reply["text"].startswith("Step 2:")
