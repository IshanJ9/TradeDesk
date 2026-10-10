"""Co-Captain end to end: past a limit the trader set, an order needs the trader AND their Co-Captain, on the exact same
card, before anything is sent. Out of the zone, and with the feature off, nothing changes. Two approvals never override
a hard stop, and no shortcut (a stranger, the trader themselves, a stale approval, a revoked link, a race, an LLM tool)
gets one through. Built on the pairing and review store in app/cocaptain (their own tests: test_cocaptain_*.py).

Three people are played with the x-tradedesk-actor header (demo mode only): "a" is the trader, "b" the Co-Captain and
"c" a stranger."""

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.broker.mock import MockBroker
from app.config import Settings
from app.llm.tools import build_tools
from app.main import create_app
from app.risk.presets import preset
from app.schemas import AuditKind

T0 = datetime(2026, 10, 10, 5, 0, tzinfo=timezone.utc)
USERS = json.dumps([dict(id=i, display_name=n, email=f"{i}@example.invalid") for i, n in [("a", "Trader"), ("b", "Ravi"), ("c", "Eve")]])
A, B, C = ({"x-tradedesk-actor": i} for i in "abc")


def settings(**over):
    base = dict(demo_mode=True, cocaptain_enabled=True, cocaptain_dev_actors=True, cocaptain_account_owner_id="a",
                cocaptain_dev_users=USERS, ticker_interval=None, reconcile_interval=None, external_sync_interval=None,
                timeout_reconcile_delay=0, account_push_interval=3600)
    return Settings(**(base | over))


class Env:
    def __init__(self, client, broker, clock):
        self.client, self.broker, self.clock = client, broker, clock

    def order(self, symbol="ITC", qty=1):
        r = self.client.post("/api/orders/preview", json=dict(action="PLACE", instrument_ref=symbol, side="BUY",
                                                              quantity=qty, order_type="MARKET")).json()
        assert r["cards"][0]["type"] == "pending_order", r
        return r["cards"][0]["pending"]

    def approve(self, p):
        return self.client.post(f"/api/approvals/{p['id']}/approve", json={"order_hash": p["order_hash"]})

    def co(self, p, verb, who=B, order_hash=None):
        body = {"order_hash": order_hash or p["order_hash"]} if verb == "approve" else None
        return self.client.post(f"/api/cocaptain/cards/{p['id']}/{verb}", json=body, headers=who)

    def inbox(self, who=B):
        return self.client.get("/api/cocaptain/inbox", headers=who).json()

    def state(self, p):
        return self.client.app.state.pending.get(p["id"]).state.value

    def sent(self):
        return len(self.broker._orders)

    def limit(self, **over):
        profile = preset("balanced").model_copy(update=over)
        assert self.client.put("/api/profile", json=profile.model_dump(mode="json")).status_code == 200

    def link(self):
        return self.client.get("/api/cocaptain/settings").json()["links"][0]

    def pair(self):
        link = self.client.post("/api/cocaptain/invite", json={"email": "b@example.invalid"}).json()
        done = self.client.post("/api/cocaptain/accept", headers=B, json={"owner_id": "a", "link_id": link["id"]})
        assert done.status_code == 200 and done.json()["status"] == "ACTIVE"
        return link

    def revoke(self, who=A):
        link = self.link()
        return self.client.post("/api/cocaptain/revoke", headers=who, json={"owner_id": "a", "link_id": link["id"]})

    def past_limit(self):
        """One order already placed today and a limit of one a day: every further order is past the trader's own limit."""
        first = self.order()
        assert self.approve(first).status_code == 200
        self.limit(max_orders_per_day=1)


def make_env(**over):
    clock = [T0]
    return Env(None, MockBroker(clock=lambda: clock[0]), clock), settings(**over)


def open_app(e, s):
    return TestClient(create_app(s, broker=e.broker, clock=lambda: e.clock[0]), headers=A)


@pytest.fixture
def env():
    e, s = make_env()
    with open_app(e, s) as c:
        e.client = c
        yield e


# ---- off, or inside your limit: nothing changes -------------------------------------------------------------- #


def test_with_the_feature_off_past_your_own_limit_is_a_warning_as_before():
    clock = [T0]
    broker = MockBroker(clock=lambda: clock[0])
    e = Env(None, broker, clock)
    with TestClient(create_app(Settings(ticker_interval=None, reconcile_interval=None, timeout_reconcile_delay=0,
                                        account_push_interval=3600), broker=broker, clock=lambda: clock[0])) as c:
        e.client = c
        e.past_limit()
        p = e.order()
        assert any("You set" in w for w in p["warnings"]) and p["co_captain"] is None
        assert e.approve(p).status_code == 200 and e.sent() == 2
        assert c.get("/api/cocaptain/config").json() == {"enabled": False, "dev_actors": False}


def test_with_a_co_captain_but_inside_the_limit_one_approval_sends(env):
    env.pair()
    p = env.order()
    assert p["co_captain"] is None and env.approve(p).status_code == 200 and env.sent() == 1


def test_past_your_limit_with_no_co_captain_the_order_is_paused_not_just_warned(env):
    env.past_limit()
    r = env.client.post("/api/orders/preview", json=dict(action="PLACE", instrument_ref="ITC", side="BUY", quantity=1,
                                                         order_type="MARKET")).json()
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
    link = env.client.post("/api/cocaptain/invite", json={"email": "b@example.invalid"}).json()
    env.past_limit()
    r = env.client.post("/api/orders/preview", json=dict(action="PLACE", instrument_ref="ITC", side="BUY", quantity=1,
                                                         order_type="MARKET")).json()
    assert r["cards"][0]["level"] == "blocked" and link["status"] == "INVITED"
    assert env.inbox(B) == []


# ---- the two-person flow ------------------------------------------------------------------------------------- #


def test_past_your_limit_the_order_waits_for_the_co_captain_and_sends_only_when_both_approved(env):
    env.pair()
    env.past_limit()
    p = env.order()
    assert p["co_captain"] == "b" and any("You set 1 orders a day" in x or "You set 1 order" in x for x in p["co_reasons"])
    r = env.approve(p)
    assert r.status_code == 409 and r.json()["code"] == "AWAITING_CO_CAPTAIN" and "b" in r.json()["message"]
    assert env.state(p) == "AWAITING_CO_APPROVAL" and env.sent() == 1  # only the first, earlier order
    assert p["id"] in [x["id"] for x in env.client.get("/api/pending").json()["orders"]]  # still on the trader's desk

    again = env.approve(p)  # the trader clicking again does not skip the second person
    assert again.status_code == 409 and again.json()["code"] == "AWAITING_CO_CAPTAIN" and env.sent() == 1

    assert [x["id"] for x in env.inbox(B)] == [p["id"]] and env.inbox(C) == []
    done = env.co(p, "approve")
    assert done.status_code == 200, done.text
    assert env.state(p) == "SENT" and env.sent() == 2
    assert env.co(p, "approve").status_code in (404, 409) and env.sent() == 2  # a repeat click sends nothing more


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
    assert env.client.post(f"/api/approvals/{p['id']}/reject").json()["state"] == "REJECTED"
    assert env.co(p, "approve").status_code == 404 and env.sent() == 1  # the review is closed with the card


def test_every_step_is_in_the_audit_trail_with_who_did_it(env):
    env.pair()
    env.past_limit()
    p = env.order()
    env.approve(p)
    env.co(p, "approve")
    rows = env.client.app.state.audit.list(500, kind=AuditKind.COCAPTAIN)
    actions = {(r.data.get("actor_id"), r.data.get("action")) for r in rows}
    assert {("a", "invited"), ("b", "accepted"), ("a", "trader_approve"), ("b", "co_captain_approve")} <= actions


# ---- who may be the second person ---------------------------------------------------------------------------- #


def test_the_trader_a_stranger_and_nobody_at_all_cannot_approve(env):
    env.pair()
    env.past_limit()
    p = env.order()
    env.approve(p)
    assert env.co(p, "approve", who=A).status_code == 404  # the trader cannot be their own second approver
    assert env.co(p, "approve", who=C).status_code == 404  # nor can a stranger
    assert env.co(p, "decline", who=C).status_code == 404
    assert env.client.post(f"/api/cocaptain/cards/{p['id']}/approve", json={"order_hash": p["order_hash"]},
                           headers={}).status_code in (401, 404)
    assert env.inbox(C) == [] and env.inbox(A) == []
    assert env.state(p) == "AWAITING_CO_APPROVAL" and env.sent() == 1


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
    env.broker._prices["NSE:ITC"] = int(env.broker._prices["NSE:ITC"] * 1.05)  # the price moves past the drift limit
    r = env.co(p, "approve")
    assert r.status_code == 409 and r.json()["code"] == "REQUOTE_REQUIRED" and env.sent() == 1
    fresh = r.json()["pending"]
    assert fresh["id"] != p["id"] and fresh["order_hash"] != p["order_hash"]
    reviews = env.client.app.state.cocaptain.reviews
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
    assert env.inbox(B) == [] and env.approve(p).status_code == 409 and env.sent() == 1


def test_either_side_can_end_it(env):
    env.pair()
    assert env.revoke(who=B).json()["status"] == "REVOKED"


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
    env.broker.locks = env.broker.locks.model_copy(update={"anchor_active": True})
    r = env.co(p, "approve")
    assert r.status_code == 409 and env.state(p) == "VOID" and env.sent() == 1


# ---- races and the last look before the broker --------------------------------------------------------------- #


def test_two_simultaneous_co_approvals_send_once(env):
    env.pair()
    env.past_limit()
    p = env.order()
    env.approve(p)
    approvals = env.client.app.state.approvals
    reviewer = env.client.app.state.cocaptain_directory.by_id("b")

    async def both():
        return await asyncio.gather(approvals.co_approve(p["id"], p["order_hash"], reviewer),
                                    approvals.co_approve(p["id"], p["order_hash"], reviewer), return_exceptions=True)

    results = env.client.portal.call(both)
    assert sum(not isinstance(r, Exception) for r in results) == 1
    assert env.sent() == 2 and env.state(p) == "SENT"  # and the losing click did not overwrite the real outcome


def test_a_revoke_during_the_checks_sends_nothing(env):
    env.pair()
    env.past_limit()
    p = env.order()
    env.approve(p)
    gate = env.client.app.state.cocaptain
    real = gate.assess

    async def revoke_during_the_check(proposed):
        result = await real(proposed)
        link = gate.pairing.get("a")
        gate.pairing.revoke(gate.owner, "a", link.id)  # the trader ends it while the check is running
        return result

    gate.assess = revoke_during_the_check
    assert env.co(p, "approve").status_code in (404, 409)
    assert env.sent() == 1 and env.state(p) == "VOID"


def test_a_revoke_between_the_last_checks_and_the_broker_call_is_caught_by_the_final_fence(env):
    env.pair()
    env.past_limit()
    p = env.order()
    env.approve(p)
    approvals = env.client.app.state.approvals
    gate = env.client.app.state.cocaptain
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
    e, s = make_env(database_url=db)
    with open_app(e, s) as c:
        e.client = c
        e.pair()
        e.past_limit()
        p = e.order()
        e.approve(p)
    e2 = Env(None, e.broker, e.clock)  # the same broker account: today's earlier order is still there
    with open_app(e2, s) as c2:
        e2.client = c2
        assert e2.link()["status"] == "ACTIVE" and e2.state(p) == "AWAITING_CO_APPROVAL"
        assert [x["id"] for x in e2.inbox(B)] == [p["id"]]
        assert e2.co(p, "approve").status_code == 200 and e2.sent() == 2


# ---- plans, the model, and plain safety ---------------------------------------------------------------------- #


def test_a_plan_past_the_limit_is_not_approved_at_all(env):
    env.pair()
    env.past_limit()
    plan = env.client.post("/api/plans/preview", json=dict(legs=[
        dict(instrument="infosys", side="SELL", fraction_of_holding=0.5),
        dict(instrument="itc", side="BUY", proceeds_of_leg=0)])).json()["cards"][0]["plan"]
    r = env.client.post(f"/api/plans/{plan['id']}/approve", json={"plan_hash": plan["plan_hash"]})
    assert r.status_code == 409 and "Co-Captain" in r.json()["message"] and env.sent() == 1


def test_no_tool_the_model_can_call_can_approve_decline_or_pair():
    names = set(build_tools())
    assert not [n for n in names if re.search(r"approv|declin|cocaptain|co_captain|invite|accept|revoke|send", n)]


def test_reading_never_changes_anything(env):
    env.pair()
    env.past_limit()
    p = env.order()
    env.approve(p)
    for _ in range(2):
        env.client.get("/api/cocaptain/settings")
        env.inbox(B)
        env.client.get("/api/pending")
    assert env.state(p) == "AWAITING_CO_APPROVAL" and env.sent() == 1


def test_the_owner_desk_feed_accepts_the_dev_actor_subprotocol_and_a_reviewer_is_kept_out(env):
    from starlette.websockets import WebSocketDisconnect
    bare = TestClient(env.client.app)  # no default actor header: who is calling travels only in the subprotocol pair
    with bare.websocket_connect("/ws", subprotocols=["tradedesk-cocaptain", "a"]) as ws:
        assert ws.receive_json()["type"] == "snapshot"  # the trader's live feed works with the actor in the subprotocol
    for who in ("b", "c"):
        with pytest.raises(WebSocketDisconnect):
            with bare.websocket_connect("/ws", subprotocols=["tradedesk-cocaptain", who]) as ws:
                ws.receive_json()  # a Co-Captain or stranger never gets the trader's account feed
