"""Co-Captain end to end: past a limit the trader set, an order needs the trader AND their Co-Captain, on the exact same
card, before anything is sent. Out of the zone nothing changes. Two approvals never override a hard stop, and no
shortcut (a stranger, the trader themselves, a stale approval, a revoked link, a race, an LLM tool) gets one through.

Two people are played with the X-Actor header (DEV_ACTORS, demo mode only): "local" is the trader, "ravi" the Co-Captain."""

import asyncio
import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.broker.mock import MockBroker
from app.config import Settings
from app.llm.tools import build_tools
from app.main import create_app
from app.orders.approval import ApprovalError
from app.risk.presets import preset
from app.schemas import AuditKind

T0 = datetime(2026, 10, 10, 5, 0, tzinfo=timezone.utc)
RAVI = {"X-Actor": "ravi"}
EVE = {"X-Actor": "eve"}


class Env:
    def __init__(self, client, broker, clock):
        self.client, self.broker, self.clock = client, broker, clock

    def order(self, symbol="ITC", qty=1, side="BUY"):
        r = self.client.post("/api/orders/preview", json=dict(action="PLACE", instrument_ref=symbol, side=side,
                                                              quantity=qty, order_type="MARKET")).json()
        assert r["cards"][0]["type"] == "pending_order", r
        return r["cards"][0]["pending"]

    def approve(self, p, **kw):
        return self.client.post(f"/api/approvals/{p['id']}/approve", json={"order_hash": p["order_hash"]}, **kw)

    def co(self, p, verb, headers=RAVI, order_hash=None):
        body = {"order_hash": order_hash or p["order_hash"]} if verb == "approve" else None
        return self.client.post(f"/api/cocaptain/cards/{p['id']}/{verb}", json=body, headers=headers)

    def state(self, p):
        return self.client.app.state.pending.get(p["id"]).state.value

    def sent(self):
        return len(self.broker._orders)

    def limit(self, **over):
        profile = preset("balanced").model_copy(update=over)
        assert self.client.put("/api/profile", json=profile.model_dump(mode="json")).status_code == 200

    def pair(self):
        assert self.client.post("/api/cocaptain/invite", json={"reviewer": "Ravi"}).status_code == 200
        assert self.client.post("/api/cocaptain/accept", headers=RAVI).status_code == 200

    def past_limit(self):
        """One order already placed today and a limit of one a day: every further order is past the trader's own limit."""
        first = self.order()
        assert self.approve(first).status_code == 200
        self.limit(max_orders_per_day=1)


def make_env(**settings):
    clock = [T0]
    broker = MockBroker(clock=lambda: clock[0])
    s = Settings(demo_mode=True, dev_actors=True, ticker_interval=None, reconcile_interval=None,
                 timeout_reconcile_delay=0, account_push_interval=3600, **settings)
    return Env(None, broker, clock), s


@pytest.fixture
def env():
    e, s = make_env()
    with TestClient(create_app(s, broker=e.broker, clock=lambda: e.clock[0])) as c:
        e.client = c
        yield e


# ---- outside the zone nothing changes ------------------------------------------------------------------------ #


def test_with_a_co_captain_but_inside_the_limit_one_approval_sends(env):
    env.pair()
    p = env.order()
    assert env.approve(p).status_code == 200 and env.sent() == 1


def test_without_a_co_captain_past_your_own_limit_is_a_warning_as_before(env):
    env.past_limit()
    p = env.order()
    assert any("You set" in w for w in p["warnings"])
    assert env.approve(p).status_code == 200 and env.sent() == 2  # Co-Captain is opt-in


def test_an_operator_can_make_no_co_captain_a_pause(env):
    e, s = make_env(cocaptain_block_without_reviewer=True)
    with TestClient(create_app(s, broker=e.broker, clock=lambda: e.clock[0])) as c:
        e.client = c
        first = e.order()
        assert e.approve(first).status_code == 200
        e.limit(max_orders_per_day=1)
        p = e.order()
        r = e.approve(p)
        assert r.status_code == 409 and r.json()["code"] == "BLOCKED" and "no Co-Captain" in r.json()["message"]
        assert e.sent() == 1


# ---- the two-person flow ------------------------------------------------------------------------------------- #


def test_past_your_limit_the_order_waits_for_the_co_captain_and_sends_only_when_both_approved(env):
    env.pair()
    env.past_limit()
    p = env.order()
    r = env.approve(p)
    assert r.status_code == 409 and r.json()["code"] == "AWAITING_CO_CAPTAIN" and "ravi" in r.json()["message"]
    assert env.state(p) == "AWAITING_CO_APPROVAL" and env.sent() == 1  # only the first, earlier order
    waiting = env.client.app.state.pending.get(p["id"])
    assert waiting.co_captain == "ravi" and any("You set 1 order a day" in x for x in waiting.co_reasons)
    assert p["id"] in [x["id"] for x in env.client.get("/api/pending").json()["orders"]]  # still on the trader's desk

    again = env.approve(p)  # the trader clicking again does not skip the second person
    assert again.status_code == 409 and again.json()["code"] == "AWAITING_CO_CAPTAIN" and env.sent() == 1

    inbox = env.client.get("/api/cocaptain/inbox", headers=RAVI).json()
    assert [x["id"] for x in inbox] == [p["id"]] and env.client.get("/api/cocaptain/inbox").json() == []

    done = env.co(p, "approve")
    assert done.status_code == 200, done.text
    assert env.state(p) == "SENT" and env.sent() == 2
    assert env.co(p, "approve").status_code == 409 and env.sent() == 2  # a repeat click sends nothing more


def test_a_decline_ends_it_and_nothing_is_sent(env):
    env.pair()
    env.past_limit()
    p = env.order()
    env.approve(p)
    r = env.co(p, "decline")
    assert r.status_code == 200 and r.json()["state"] == "REJECTED" and env.sent() == 1


def test_every_step_is_in_the_audit_trail(env):
    env.pair()
    env.past_limit()
    p = env.order()
    env.approve(p)
    env.co(p, "approve")
    rows = env.client.app.state.audit.list(500, kind=AuditKind.COCAPTAIN)
    text = " | ".join(r.summary for r in rows)
    for want in ("Invited ravi", "ravi accepted as Co-Captain", "waiting for ravi", "Co-Captain ravi approved"):
        assert want in text, text
    assert {r.data.get("by") for r in rows} >= {"local", "ravi"}  # who did each step is recorded


# ---- who may be the second person ---------------------------------------------------------------------------- #


def test_the_trader_a_stranger_and_an_unaccepted_invitee_cannot_approve(env):
    env.client.post("/api/cocaptain/invite", json={"reviewer": "ravi"})  # invited, not accepted
    env.past_limit()
    p = env.order()
    assert env.approve(p).status_code == 200  # no ACTIVE Co-Captain yet: ordinary approval (opt-in)

    e2, s = make_env()
    with TestClient(create_app(s, broker=e2.broker, clock=lambda: e2.clock[0])) as c:
        e2.client = c
        e2.pair()
        e2.past_limit()
        q = e2.order()
        e2.approve(q)
        assert e2.co(q, "approve", headers={}).status_code == 404  # the trader cannot be their own second approver
        assert e2.co(q, "approve", headers=EVE).status_code == 404  # nor can a stranger
        assert e2.co(q, "decline", headers=EVE).status_code == 404
        assert e2.client.get("/api/cocaptain/inbox", headers=EVE).json() == []
        assert e2.state(q) == "AWAITING_CO_APPROVAL" and e2.sent() == 1


def test_you_cannot_be_your_own_co_captain_or_have_two(env):
    assert env.client.post("/api/cocaptain/invite", json={"reviewer": "local"}).status_code == 409
    assert env.client.post("/api/cocaptain/invite", json={"reviewer": "ravi"}).status_code == 200
    assert env.client.post("/api/cocaptain/invite", json={"reviewer": "sam"}).status_code == 409
    assert env.client.post("/api/cocaptain/accept").status_code == 409  # nothing was waiting for the trader


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
    assert env.client.app.state.cocaptain.store.decision(fresh["id"], "CO_CAPTAIN") is None  # nothing carried over


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


def test_revoking_the_co_captain_ends_their_say(env):
    env.pair()
    env.past_limit()
    p = env.order()
    env.approve(p)
    assert env.client.post("/api/cocaptain/revoke").status_code == 200
    assert env.co(p, "approve").status_code == 404 and env.sent() == 1
    assert env.client.get("/api/cocaptain/inbox", headers=RAVI).json() == []


def test_an_approval_from_an_earlier_pairing_does_not_count_after_a_re_invite(env):
    env.pair()
    env.past_limit()
    p = env.order()
    env.approve(p)
    svc = env.client.app.state.cocaptain
    first_hash = p["order_hash"]
    svc.store.record(p["id"], "CO_CAPTAIN", "ravi", first_hash, "zone-v1", "APPROVE", env.clock[0])  # given under pairing 1
    assert env.client.post("/api/cocaptain/revoke").status_code == 200
    env.clock[0] += timedelta(seconds=1)
    env.pair()  # the same person, a new pairing
    assert not svc.co_decision_counts(p["id"], first_hash, "zone-v1")
    assert env.approve(p).json()["code"] == "AWAITING_CO_CAPTAIN" and env.sent() == 1


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


# ---- races --------------------------------------------------------------------------------------------------- #


def test_two_simultaneous_co_approvals_send_once(env):
    env.pair()
    env.past_limit()
    p = env.order()
    env.approve(p)
    approvals = env.client.app.state.approvals

    async def both():
        return await asyncio.gather(approvals.co_approve(p["id"], p["order_hash"], "ravi"),
                                    approvals.co_approve(p["id"], p["order_hash"], "ravi"), return_exceptions=True)

    results = env.client.portal.call(both)
    assert sum(not isinstance(r, Exception) for r in results) == 1
    assert [type(r) for r in results if isinstance(r, Exception)] == [ApprovalError]
    assert env.sent() == 2  # the earlier order and this one, once
    assert env.state(p) == "SENT"  # and the losing click did not overwrite the card's real outcome


def test_an_approval_racing_a_revoke_sends_nothing_if_the_link_is_gone(env):
    env.pair()
    env.past_limit()
    p = env.order()
    env.approve(p)
    svc = env.client.app.state.cocaptain
    real_gate = svc.gate

    async def revoke_during_the_check(**kw):
        result = await real_gate(**kw)
        svc.store.revoke("local", env.clock[0])  # the trader ends it while the check is running
        return result

    svc.gate = revoke_during_the_check
    assert env.co(p, "approve").status_code == 404
    assert env.sent() == 1 and env.state(p) == "AWAITING_CO_APPROVAL"


# ---- it survives a restart ----------------------------------------------------------------------------------- #


def test_the_pairing_and_a_waiting_card_survive_a_restart(tmp_path):
    db = f"sqlite:///{tmp_path / 'td.db'}"
    clock = [T0]
    broker = MockBroker(clock=lambda: clock[0])
    s = Settings(demo_mode=True, dev_actors=True, database_url=db, ticker_interval=None, reconcile_interval=None,
                 timeout_reconcile_delay=0, account_push_interval=3600)
    e = Env(None, broker, clock)
    with TestClient(create_app(s, broker=broker, clock=lambda: clock[0])) as c:
        e.client = c
        e.pair()
        e.past_limit()
        p = e.order()
        e.approve(p)
    broker2 = MockBroker(clock=lambda: clock[0])
    e2 = Env(None, broker2, clock)
    with TestClient(create_app(s, broker=broker2, clock=lambda: clock[0])) as c2:
        e2.client = c2
        assert c2.get("/api/cocaptain/status").json()["as_trader"] == {"reviewer": "ravi", "status": "ACTIVE"}
        assert e2.state(p) == "AWAITING_CO_APPROVAL"
        assert [x["id"] for x in c2.get("/api/cocaptain/inbox", headers=RAVI).json()] == [p["id"]]


def test_a_co_captain_approval_given_while_out_of_zone_counts_only_for_that_exact_card(env):
    env.pair()
    env.past_limit()
    p = env.order()
    env.approve(p)  # waiting
    env.limit(max_orders_per_day=99)  # inside again: the reviewer's click is recorded but sends nothing
    assert env.co(p, "approve").status_code == 409 and env.sent() == 1
    env.limit(max_orders_per_day=1)  # past the limit again
    svc = env.client.app.state.cocaptain
    svc.store.record(p["id"], "CO_CAPTAIN", "ravi", "f" * 64, "zone-v1", "APPROVE", env.clock[0])  # (ignored: already decided)
    assert svc.co_decision_counts(p["id"], p["order_hash"], "zone-v1")  # the original click is for this exact card
    assert not svc.co_decision_counts(p["id"], "f" * 64, "zone-v1")  # not for a different one
    assert not svc.co_decision_counts(p["id"], p["order_hash"], "zone-v2")  # nor under a different rule version
    assert env.approve(p).status_code == 200 and env.sent() == 2  # the trader's click now completes both approvals


def test_the_trader_is_never_their_own_reviewer_even_if_a_link_said_so(env):
    svc = env.client.app.state.cocaptain
    svc.store.invite("local", "local", env.clock[0])  # impossible through the API; the check must not rely on that
    svc.store.accept("local", env.clock[0])
    assert not svc.may_review("local")


# ---- plans, the model, and plain safety ---------------------------------------------------------------------- #


def test_a_plan_is_not_approved_at_all_while_past_the_limit_with_a_co_captain(env):
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
        env.client.get("/api/cocaptain/status")
        env.client.get("/api/cocaptain/inbox", headers=RAVI)
        env.client.get("/api/pending")
    assert env.state(p) == "AWAITING_CO_APPROVAL" and env.sent() == 1


def test_the_second_actor_only_exists_in_demo_dev_mode():
    clock = [T0]
    broker = MockBroker(clock=lambda: clock[0])
    s = Settings(demo_mode=False, dev_actors=True, ticker_interval=None, reconcile_interval=None,
                 timeout_reconcile_delay=0, account_push_interval=3600)
    with TestClient(create_app(s, broker=broker, clock=lambda: clock[0])) as c:
        assert c.get("/api/cocaptain/status", headers=RAVI).json()["me"] == "local"  # the header is ignored
