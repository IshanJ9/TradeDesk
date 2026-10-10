"""Co-Captain end to end, between real accounts: past a limit the trader set, an order needs the trader AND their
Co-Captain, on the exact same card, before anything is sent. Out of the zone, and with the feature off, nothing
changes. Two approvals never override a hard stop, and no shortcut (a stranger, the trader themselves, a stale
approval, a revoked pairing, a race, an LLM tool) gets one through.

Three signed-in users share one server, each with their own desk: "a" is the trader, "b" the Co-Captain, "c" a stranger.
The pairing and review store have their own tests (test_cocaptain_pairing.py, test_cocaptain_store.py)."""

import asyncio
import re
from datetime import datetime, timedelta, timezone

import pytest
from conftest import SignedInClient

from app.broker.mock import MockBroker
from app.config import Settings
from app.llm.tools import build_tools
from app.main import create_app
from app.orders.approval import ApprovalNotFound
from app.risk.presets import preset
from app.schemas import AuditKind

T0 = datetime(2026, 10, 10, 5, 0, tzinfo=timezone.utc)
ORDER = dict(action="PLACE", side="BUY", order_type="MARKET")


def settings(**over):
    base = dict(cocaptain_enabled=True, ticker_interval=None, reconcile_interval=None, external_sync_interval=None,
                timeout_reconcile_delay=0, account_push_interval=3600)
    return Settings(**(base | over))


class Env:
    """Three signed-in users on one app. `a` is the trader (and the server's owner, so they get the server's broker)."""

    def __init__(self, app, clock, a, b, c):
        self.app, self.clock, self.a, self.b, self.c = app, clock, a, b, c
        self.ids = {k: v.get("/api/auth/me").json()["user"]["id"] for k, v in (("a", a), ("b", b), ("c", c))}

    def ws(self, who="a"):
        return self.app.state.workspaces.peek(self.ids[who])

    def person(self, who):
        return self.ws(who).user

    def order(self, symbol="ITC", qty=1, client=None):
        r = (client or self.a).post("/api/orders/preview", json=dict(ORDER, instrument_ref=symbol, quantity=qty)).json()
        assert r["cards"][0]["type"] == "pending_order", r
        return r["cards"][0]["pending"]

    def approve(self, p, client=None):
        return (client or self.a).post(f"/api/approvals/{p['id']}/approve", json={"order_hash": p["order_hash"]})

    def co(self, p, verb, who="b", order_hash=None):
        client = getattr(self, who)
        body = {"order_hash": order_hash or p["order_hash"]} if verb == "approve" else None
        return client.post(f"/api/cocaptain/cards/{p['id']}/{verb}", json=body)

    def inbox(self, who="b"):
        return getattr(self, who).get("/api/cocaptain/inbox").json()

    def state(self, p):
        return self.ws("a").pending.get(p["id"]).state.value

    def sent(self):
        return len(self.ws("a").broker._orders)

    def limit(self, **over):
        profile = preset("balanced").model_copy(update=over)
        assert self.a.put("/api/profile", json=profile.model_dump(mode="json")).status_code == 200

    def link(self):
        return self.a.get("/api/cocaptain/settings").json()["links"][0]

    def pair(self):
        link = self.a.post("/api/cocaptain/invite", json={"email": "b@example.com"}).json()
        done = self.b.post("/api/cocaptain/accept", json={"owner_id": self.ids["a"], "link_id": link["id"]})
        assert done.status_code == 200 and done.json()["status"] == "ACTIVE"
        return link

    def revoke(self, who="a"):
        link = self.link()
        return getattr(self, who).post("/api/cocaptain/revoke", json={"owner_id": self.ids["a"], "link_id": link["id"]})

    def past_limit(self):
        """One order already placed today and a limit of one a day: every further order is past the trader's own limit."""
        first = self.order()
        assert self.approve(first).status_code == 200
        self.limit(max_orders_per_day=1)


def build(**over):
    clock = [T0]
    app = create_app(settings(**over), broker=MockBroker(clock=lambda: clock[0]), clock=lambda: clock[0])
    return app, clock


def sign_in_all(app, clock, a):
    b, c = SignedInClient(app, email="b@example.com"), SignedInClient(app, email="c@example.com")
    b.portal = c.portal = a.portal  # one event loop for every desk, as in a real server
    a.sign_in()
    b.sign_in()
    c.sign_in()
    return Env(app, clock, a, b, c)


@pytest.fixture
def env():
    app, clock = build()
    with SignedInClient(app, email="a@example.com") as a:
        yield sign_in_all(app, clock, a)


# ---- off, or inside your limit: nothing changes -------------------------------------------------------------- #


def test_with_the_feature_off_past_your_own_limit_is_a_warning_as_before():
    app, clock = build(cocaptain_enabled=False)
    with SignedInClient(app, email="a@example.com") as a:
        e = sign_in_all(app, clock, a)
        e.past_limit()
        p = e.order()
        assert any("You set" in w for w in p["warnings"]) and p["co_captain"] is None
        assert e.approve(p).status_code == 200 and e.sent() == 2
        assert a.get("/api/cocaptain/config").json() == {"enabled": False}
        assert a.get("/api/cocaptain/settings").status_code == 404


def test_with_a_co_captain_but_inside_the_limit_one_approval_sends(env):
    env.pair()
    p = env.order()
    assert p["co_captain"] is None and env.approve(p).status_code == 200 and env.sent() == 1


def test_past_your_limit_with_no_co_captain_the_order_is_paused_not_just_warned(env):
    env.past_limit()
    r = env.a.post("/api/orders/preview", json=dict(ORDER, instrument_ref="ITC", quantity=1)).json()
    assert r["cards"][0]["level"] == "blocked" and "no Co-Captain" in r["text"] and env.sent() == 1


def test_a_card_made_before_the_limit_was_crossed_is_still_paused_at_the_click(env):
    p = env.order()  # no profile yet, so no zone
    first = env.order("TCS")
    assert env.approve(first).status_code == 200
    env.limit(max_orders_per_day=1)
    r = env.approve(p)
    assert r.status_code == 409 and r.json()["code"] == "BLOCKED" and "no Co-Captain" in r.json()["message"]
    assert env.sent() == 1


def test_an_invitation_not_yet_accepted_is_not_a_co_captain(env):
    link = env.a.post("/api/cocaptain/invite", json={"email": "b@example.com"}).json()
    env.past_limit()
    r = env.a.post("/api/orders/preview", json=dict(ORDER, instrument_ref="ITC", quantity=1)).json()
    assert r["cards"][0]["level"] == "blocked" and link["status"] == "INVITED"
    assert env.inbox("b") == []


# ---- the two-person flow ------------------------------------------------------------------------------------- #


def test_past_your_limit_the_order_waits_for_the_co_captain_and_sends_only_when_both_approved(env):
    env.pair()
    env.past_limit()
    p = env.order()
    assert p["co_captain"] == env.ids["b"] and any("You set 1 order" in x for x in p["co_reasons"])
    assert p["co_captain_name"] == "b"  # shown by name (the part of the email before the @), never by internal id
    r = env.approve(p)
    assert r.status_code == 409 and r.json()["code"] == "AWAITING_CO_CAPTAIN"
    assert "Co-Captain, b," in r.json()["message"] and env.ids["b"] not in r.json()["message"]
    assert env.state(p) == "AWAITING_CO_APPROVAL" and env.sent() == 1  # only the first, earlier order
    assert p["id"] in [x["id"] for x in env.a.get("/api/pending").json()["orders"]]  # still on the trader's desk

    again = env.approve(p)  # the trader clicking again does not skip the second person
    assert again.status_code == 409 and again.json()["code"] == "AWAITING_CO_CAPTAIN" and env.sent() == 1

    assert [x["id"] for x in env.inbox("b")] == [p["id"]] and env.inbox("c") == []
    done = env.co(p, "approve")
    assert done.status_code == 200, done.text
    assert env.state(p) == "SENT" and env.sent() == 2
    assert env.co(p, "approve").status_code in (404, 409) and env.sent() == 2  # a repeat click sends nothing more


def test_the_co_captain_sees_the_card_and_nothing_else_of_the_traders(env):
    env.pair()
    env.past_limit()
    p = env.order()
    env.approve(p)
    assert [x["id"] for x in env.inbox("b")] == [p["id"]]
    assert env.b.get("/api/pending").json() == {"orders": [], "plans": []}  # their own desk is their own
    assert env.b.get("/api/orders").json() == [] and env.b.get("/api/profile").json() is None
    assert all(p["id"] not in str(env.b.get(f"/api/{route}").json()) for route in ("audit", "rules", "orders", "pending"))


def test_a_decline_ends_it_and_nothing_is_sent(env):
    env.pair()
    env.past_limit()
    p = env.order()
    env.approve(p)
    r = env.co(p, "decline")
    assert r.status_code == 200 and r.json()["state"] == "REJECTED" and env.sent() == 1
    assert env.approve(p).status_code == 409 and env.sent() == 1


def test_the_trader_can_cancel_a_waiting_card(env):
    env.pair()
    env.past_limit()
    p = env.order()
    env.approve(p)
    assert env.a.post(f"/api/approvals/{p['id']}/reject").json()["state"] == "REJECTED"
    assert env.co(p, "approve").status_code == 404 and env.sent() == 1  # the review is closed with the card


def test_every_step_is_in_the_traders_audit_trail_with_who_did_it(env):
    env.pair()
    env.past_limit()
    p = env.order()
    env.approve(p)
    env.co(p, "approve")
    rows = env.ws("a").audit.list(500, kind=AuditKind.COCAPTAIN)
    actions = {(r.data.get("actor_id"), r.data.get("action")) for r in rows}
    a, b = env.ids["a"], env.ids["b"]
    assert {(a, "invited"), (b, "accepted"), (a, "trader_approve"), (b, "co_captain_approve")} <= actions
    assert env.ws("b").audit.list(500, kind=AuditKind.COCAPTAIN) == []  # it is the trader's record, not the reviewer's
    assert env.ws("c").audit.list(500, kind=AuditKind.COCAPTAIN) == []


# ---- who may be the second person ---------------------------------------------------------------------------- #


def test_the_trader_a_stranger_and_nobody_signed_in_cannot_approve(env):
    env.pair()
    env.past_limit()
    p = env.order()
    env.approve(p)
    assert env.co(p, "approve", who="a").status_code == 404  # the trader cannot be their own second approver
    assert env.co(p, "approve", who="c").status_code == 404  # nor can a stranger
    assert env.co(p, "decline", who="c").status_code == 404
    from starlette.testclient import TestClient as Anonymous
    anon = Anonymous(env.app)  # not entered as a context: that would run the app's start-up and shut-down again
    assert anon.post(f"/api/cocaptain/cards/{p['id']}/approve", json={"order_hash": p["order_hash"]}).status_code == 401
    assert env.inbox("c") == [] and env.inbox("a") == []
    assert env.state(p) == "AWAITING_CO_APPROVAL" and env.sent() == 1


def test_the_approval_service_itself_refuses_anyone_but_the_co_captain(env):
    """The route also checks this; here the service is called directly, so each layer is proven on its own."""
    env.pair()
    env.past_limit()
    p = env.order()
    env.approve(p)
    approvals = env.ws("a").approvals
    for who in ("a", "c"):  # the trader themselves, and a stranger
        with pytest.raises(ApprovalNotFound):
            env.a.portal.call(lambda who=who: approvals.co_approve(p["id"], p["order_hash"], env.person(who)))
        with pytest.raises(ApprovalNotFound):
            env.a.portal.call(lambda who=who: approvals.co_decline(p["id"], env.person(who)))
    assert env.state(p) == "AWAITING_CO_APPROVAL" and env.sent() == 1


def test_a_co_captain_cannot_approve_a_card_of_someone_who_did_not_invite_them(env):
    env.pair()  # b is a's Co-Captain, not c's
    c_card = env.order(client=env.c)
    assert env.co(c_card, "approve", who="b").status_code == 404


# ---- the same exact card, the same rule ---------------------------------------------------------------------- #


def test_a_wrong_hash_voids_the_card_and_sends_nothing(env):
    env.pair()
    env.past_limit()
    p = env.order()
    env.approve(p)
    r = env.co(p, "approve", order_hash="0" * 64)
    assert r.status_code == 409 and r.json()["code"] == "HASH_MISMATCH"
    assert env.state(p) == "VOID" and env.sent() == 1


def test_an_expired_card_is_dead_for_both(env):
    env.pair()
    env.past_limit()
    p = env.order()
    env.approve(p)
    env.clock[0] = T0 + timedelta(seconds=300)
    r = env.co(p, "approve")
    assert r.status_code == 409 and r.json()["code"] == "EXPIRED" and env.state(p) == "EXPIRED" and env.sent() == 1


def test_a_changed_price_makes_a_new_card_that_needs_fresh_approvals(env):
    env.pair()
    env.past_limit()
    p = env.order()
    env.approve(p)
    broker = env.ws("a").broker
    broker._prices["NSE:ITC"] = int(broker._prices["NSE:ITC"] * 1.05)  # the price moves past the drift limit
    r = env.co(p, "approve")
    assert r.status_code == 409 and r.json()["code"] == "REQUOTE_REQUIRED" and env.sent() == 1
    fresh = r.json()["pending"]
    assert fresh["id"] != p["id"] and fresh["order_hash"] != p["order_hash"]
    reviews = env.app.state.cocaptain_reviews
    assert reviews.decisions(fresh["id"]) == []  # nothing carried over
    assert reviews.get(p["id"]).status == "INVALIDATED"


# ---- things that change while it waits ----------------------------------------------------------------------- #


def test_leaving_the_zone_while_waiting_means_the_trader_must_click_again(env):
    env.pair()
    env.past_limit()
    p = env.order()
    env.approve(p)
    env.limit(max_orders_per_day=99)  # back inside the limit
    r = env.co(p, "approve")
    assert r.status_code == 409 and "approve it again themselves" in r.json()["message"]
    assert env.sent() == 1 and env.state(p) == "AWAITING_CO_APPROVAL"
    assert env.approve(p).status_code == 200 and env.sent() == 2  # the trader's fresh click sends it


def test_ending_the_pairing_cancels_what_was_waiting_and_nothing_is_sent(env):
    env.pair()
    env.past_limit()
    p = env.order()
    env.approve(p)
    assert env.revoke().json()["status"] == "REVOKED"
    assert env.state(p) == "VOID" and env.co(p, "approve").status_code == 404 and env.sent() == 1
    assert env.inbox("b") == [] and env.approve(p).status_code == 409 and env.sent() == 1


def test_either_side_can_end_it(env):
    env.pair()
    assert env.revoke(who="b").json()["status"] == "REVOKED"


def test_a_hard_stop_still_wins_after_both_have_approved(env):
    env.pair()
    env.past_limit()
    p = env.order()
    env.approve(p)  # waiting
    env.limit(max_orders_per_day=1, hard_order_limit=True)  # the trader's own hard stop comes on
    r = env.co(p, "approve")
    assert r.status_code == 409 and r.json()["code"] == "BLOCKED" and "hard limit" in r.json()["message"]
    assert env.state(p) == "VOID" and env.sent() == 1


def test_an_anchor_lock_still_wins_after_both_have_approved(env):
    env.pair()
    env.past_limit()
    p = env.order()
    env.approve(p)
    broker = env.ws("a").broker
    broker.locks = broker.locks.model_copy(update={"anchor_active": True})
    r = env.co(p, "approve")
    assert r.status_code == 409 and env.state(p) == "VOID" and env.sent() == 1


# ---- races and the last look before the broker --------------------------------------------------------------- #


def test_two_simultaneous_co_approvals_send_once(env):
    env.pair()
    env.past_limit()
    p = env.order()
    env.approve(p)
    approvals, reviewer = env.ws("a").approvals, env.person("b")

    async def both():
        return await asyncio.gather(approvals.co_approve(p["id"], p["order_hash"], reviewer),
                                    approvals.co_approve(p["id"], p["order_hash"], reviewer), return_exceptions=True)

    results = env.a.portal.call(both)
    assert sum(not isinstance(r, Exception) for r in results) == 1
    assert env.sent() == 2 and env.state(p) == "SENT"  # and the losing click did not overwrite the real outcome


def test_a_revoke_during_the_checks_sends_nothing(env):
    env.pair()
    env.past_limit()
    p = env.order()
    env.approve(p)
    gate = env.ws("a").cocaptain
    real = gate.assess

    async def revoke_during_the_check(proposed):
        result = await real(proposed)
        gate.pairing.revoke(gate.owner, gate.owner_id, gate.pairing.get(gate.owner_id).id)  # the trader ends it mid-check
        return result

    gate.assess = revoke_during_the_check
    assert env.co(p, "approve").status_code in (404, 409)
    assert env.sent() == 1 and env.state(p) == "VOID"


def test_a_revoke_between_the_last_checks_and_the_broker_call_is_caught_by_the_final_fence(env):
    env.pair()
    env.past_limit()
    p = env.order()
    env.approve(p)
    approvals, gate = env.ws("a").approvals, env.ws("a").cocaptain
    real = approvals._recheck

    async def end_it_after_the_rechecks(card):
        await real(card)  # every broker check passes ...
        gate.reviews.invalidate(card.id, "ended just now")  # ... then the review stops counting before the broker is called

    approvals._recheck = end_it_after_the_rechecks
    r = env.co(p, "approve")
    assert r.status_code == 409 and "no longer holds" in r.json()["message"]
    assert env.sent() == 1 and env.state(p) == "VOID"


# ---- it survives a restart ----------------------------------------------------------------------------------- #


def test_the_pairing_and_a_waiting_card_survive_a_restart(tmp_path):
    db = f"sqlite:///{tmp_path / 'td.db'}"
    clock = [T0]
    broker = MockBroker(clock=lambda: clock[0])
    app1 = create_app(settings(database_url=db), broker=broker, clock=lambda: clock[0])
    with SignedInClient(app1, email="a@example.com") as a:
        e = sign_in_all(app1, clock, a)
        e.pair()
        e.past_limit()
        p = e.order()
        e.approve(p)
    app2 = create_app(settings(database_url=db), broker=broker, clock=lambda: clock[0])  # same server, same broker account
    with SignedInClient(app2, email="a@example.com") as a2:
        e2 = sign_in_all(app2, clock, a2)
        assert e2.link()["status"] == "ACTIVE" and e2.state(p) == "AWAITING_CO_APPROVAL"
        assert [x["id"] for x in e2.inbox("b")] == [p["id"]]
        assert e2.co(p, "approve").status_code == 200 and e2.sent() == 2


# ---- plans, the model, and plain safety ---------------------------------------------------------------------- #


PLAN = dict(legs=[dict(instrument="infosys", side="SELL", fraction_of_holding=0.5),
                  dict(instrument="itc", side="BUY", proceeds_of_leg=0)])


def plan_card(env):
    return env.a.post("/api/plans/preview", json=PLAN).json()["cards"][0]["plan"]


def plan_state(env, plan):
    return env.ws("a").plan_store.get(plan["id"]).state.value


def plan_approve(env, plan, who="a"):
    if who == "a":
        return env.a.post(f"/api/plans/{plan['id']}/approve", json={"plan_hash": plan["plan_hash"]})
    return getattr(env, who).post(f"/api/cocaptain/plans/{plan['id']}/approve", json={"plan_hash": plan["plan_hash"]})


def plan_inbox(env, who="b"):
    return getattr(env, who).get("/api/cocaptain/plan-inbox").json()


def finish(env, plan):
    env.a.portal.call(lambda: env.ws("a").plans.join(plan["id"]))
    return env.ws("a").plan_store.report(plan["id"])


def test_a_whole_plan_past_the_limit_waits_for_the_co_captain_and_runs_only_when_both_approved(env):
    env.pair()
    env.past_limit()
    plan = plan_card(env)
    r = plan_approve(env, plan)
    assert r.status_code == 409 and r.json()["code"] == "AWAITING_CO_CAPTAIN" and "same plan" in r.json()["message"]
    assert plan_state(env, plan) == "AWAITING_CO_APPROVAL" and env.sent() == 1  # nothing of the plan was sent
    shown = env.ws("a").plan_store.get(plan["id"])
    assert shown.co_captain == env.ids["b"] and shown.co_captain_name == "b" and shown.co_reasons
    assert plan["id"] in [x["id"] for x in env.a.get("/api/pending").json()["plans"]]  # still on the trader's desk

    again = plan_approve(env, plan)  # the trader clicking again does not skip the second person
    assert again.status_code == 409 and env.sent() == 1

    assert [x["id"] for x in plan_inbox(env)] == [plan["id"]] and plan_inbox(env, "c") == []
    done = plan_approve(env, plan, who="b")
    assert done.status_code == 200, done.text
    report = finish(env, plan)
    assert report.state.value == "COMPLETED" and all(leg.status.value == "FILLED" for leg in report.legs)
    assert env.sent() == 3  # the earlier order, then both steps of the plan


def test_a_plan_decline_ends_it_and_the_trader_can_cancel_a_waiting_plan(env):
    env.pair()
    env.past_limit()
    plan = plan_card(env)
    plan_approve(env, plan)
    r = env.b.post(f"/api/cocaptain/plans/{plan['id']}/decline")
    assert r.status_code == 200 and r.json()["state"] == "REJECTED" and env.sent() == 1
    second = plan_card(env)
    plan_approve(env, second)
    assert env.a.post(f"/api/plans/{second['id']}/reject").json()["state"] == "REJECTED"
    assert plan_approve(env, second, who="b").status_code == 404 and env.sent() == 1


def test_a_plan_review_is_for_that_plan_only_and_only_for_the_co_captain(env):
    env.pair()
    env.past_limit()
    plan = plan_card(env)
    plan_approve(env, plan)
    for who in ("a", "c"):  # the trader themselves, and a stranger
        assert getattr(env, who).post(f"/api/cocaptain/plans/{plan['id']}/approve", json={"plan_hash": plan["plan_hash"]}).status_code == 404
        assert getattr(env, who).post(f"/api/cocaptain/plans/{plan['id']}/decline").status_code == 404
    wrong = env.b.post(f"/api/cocaptain/plans/{plan['id']}/approve", json={"plan_hash": "0" * 64})
    assert wrong.status_code == 409 and wrong.json()["code"] == "HASH_MISMATCH"
    assert plan_state(env, plan) == "VOID" and env.sent() == 1


def test_leaving_the_zone_while_a_plan_waits_means_the_trader_must_click_again(env):
    env.pair()
    env.past_limit()
    plan = plan_card(env)
    plan_approve(env, plan)
    env.limit(max_orders_per_day=99)  # back inside the limit
    r = plan_approve(env, plan, who="b")
    assert r.status_code == 409 and "approve it again themselves" in r.json()["message"] and env.sent() == 1
    assert plan_approve(env, plan).status_code == 200
    assert finish(env, plan).state.value == "COMPLETED" and env.sent() == 3


def test_ending_the_pairing_cancels_a_waiting_plan(env):
    env.pair()
    env.past_limit()
    plan = plan_card(env)
    plan_approve(env, plan)
    assert env.revoke().json()["status"] == "REVOKED"
    assert plan_state(env, plan) == "VOID" and plan_approve(env, plan, who="b").status_code == 404 and env.sent() == 1
    assert plan_inbox(env) == []


def test_ending_the_pairing_after_a_plan_started_stops_the_steps_not_yet_sent(env):
    env.pair()
    env.past_limit()
    plan = plan_card(env)
    plan_approve(env, plan)
    plans, gate = env.ws("a").plans, env.ws("a").cocaptain
    real = plans._send_leg
    sent_legs = []

    async def end_it_after_the_first_step(leg, to_send):
        result = await real(leg, to_send)
        sent_legs.append(leg.index)
        if len(sent_legs) == 1:
            gate.pairing.revoke(gate.owner, gate.owner_id, gate.pairing.get(gate.owner_id).id)  # ended mid-plan
        return result

    plans._send_leg = end_it_after_the_first_step
    assert plan_approve(env, plan, who="b").status_code == 200
    report = finish(env, plan)
    assert sent_legs == [0] and env.sent() == 2  # the earlier order and step 1 only
    assert report.legs[1].status.value == "SKIPPED" and "Co-Captain" in report.legs[1].message


def test_a_plan_is_checked_one_last_time_right_before_it_starts(env):
    env.pair()
    env.past_limit()
    plan = plan_card(env)
    plan_approve(env, plan)
    plans, gate = env.ws("a").plans, env.ws("a").cocaptain
    real = plans._recheck

    async def end_it_after_the_rechecks(p):
        await real(p)
        gate.reviews.invalidate(p.id, "ended just now")

    plans._recheck = end_it_after_the_rechecks
    r = plan_approve(env, plan, who="b")
    assert r.status_code == 409 and "no longer holds" in r.json()["message"]
    assert env.sent() == 1 and plan_state(env, plan) == "VOID"


def test_a_hard_stop_still_wins_for_a_plan_after_both_have_approved(env):
    env.pair()
    env.past_limit()
    plan = plan_card(env)
    plan_approve(env, plan)
    env.limit(max_orders_per_day=1, hard_order_limit=True)
    r = plan_approve(env, plan, who="b")
    assert r.status_code == 409 and r.json()["code"] == "BLOCKED" and env.sent() == 1
    assert plan_state(env, plan) == "VOID"


# ---- the Co-Captain's desk hears about it at once ------------------------------------------------------------------ #


def next_cocaptain_event(ws, limit=12, wait=3.0):
    """The next co-captain event on this live feed. Fails after `wait` seconds of silence instead of hanging."""
    import concurrent.futures
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        for _ in range(limit):
            try:
                event = pool.submit(ws.receive_json).result(timeout=wait)
            except concurrent.futures.TimeoutError:
                raise AssertionError("no co-captain event arrived") from None
            if event["type"] == "cocaptain_update":
                return event
        raise AssertionError("no co-captain event arrived")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def test_the_co_captains_desk_is_told_at_once_over_its_own_live_feed(env):
    with env.b.websocket_connect("/ws") as ws_b:
        assert ws_b.receive_json()["type"] == "snapshot"
        link = env.a.post("/api/cocaptain/invite", json={"email": "b@example.com"}).json()
        assert next_cocaptain_event(ws_b)["action"] == "invited"  # the invitation arrives without polling
        env.b.post("/api/cocaptain/accept", json={"owner_id": env.ids["a"], "link_id": link["id"]})
        env.past_limit()
        p = env.order()
        env.approve(p)
        while True:
            e = next_cocaptain_event(ws_b)
            if e["action"] == "review_requested":
                assert e["card_id"] == p["id"]
                break
        env.co(p, "decline")
        plan = plan_card(env)
        plan_approve(env, plan)
        while True:
            e = next_cocaptain_event(ws_b)
            if e["action"] == "review_requested":
                assert e["card_id"] == plan["id"]  # a waiting plan is announced the same way
                break


def test_a_stranger_is_never_told_about_someone_elses_review(env):
    events = env.app.state.events
    queue = events.subscribe(env.ids["c"])  # c's own live feed, listened to for the whole exchange
    try:
        env.pair()
        env.past_limit()
        p = env.order()
        env.approve(p)
        plan = plan_card(env)
        plan_approve(env, plan)
        env.co(p, "decline")
        heard = []
        while not queue.empty():
            heard.append(queue.get_nowait())
        assert all(getattr(e, "type", None) != "cocaptain_update" for e in heard)
    finally:
        events.unsubscribe(env.ids["c"], queue)
    b_queue = events.subscribe(env.ids["b"])  # ...whereas the Co-Captain's feed does hear it
    try:
        env.revoke()
        assert any(getattr(e, "type", None) == "cocaptain_update" and e.action == "revoked" for e in
                   [b_queue.get_nowait() for _ in range(b_queue.qsize())])
    finally:
        events.unsubscribe(env.ids["b"], b_queue)


def test_no_tool_the_model_can_call_can_approve_decline_or_pair():
    names = set(build_tools())
    assert not [n for n in names if re.search(r"approv|declin|cocaptain|co_captain|invite|accept|revoke|send", n)]


def test_reading_never_changes_anything(env):
    env.pair()
    env.past_limit()
    p = env.order()
    env.approve(p)
    for _ in range(2):
        env.a.get("/api/cocaptain/settings")
        env.inbox("b")
        env.a.get("/api/pending")
    assert env.state(p) == "AWAITING_CO_APPROVAL" and env.sent() == 1
