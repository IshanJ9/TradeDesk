"""Standing instructions: creating them, firing them exactly once, surviving a restart, and the
guarantee that a rule can only ever prepare an alert or an approval card."""

import asyncio
import functools
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api_models import CreateRuleRequest
from app.broker.mock import MockBroker
from app.config import Settings
from app.llm.copilot import Copilot
from app.llm.tools import build_tools
from app.llm.types import LLMTurn, ToolCall
from app.main import create_app
from app.plans.service import PlanAssistant
from app.orders.approval import ApprovalError
from app.rules.engine import RuleEngine
from app.rules.service import RuleNotActive, RuleNotFound
from app.schemas import (
    AccountLocks,
    AuditKind,
    OrderType,
    PendingState,
    RuleBasis,
    RuleStatus,
    Tick,
    paise,
)

T0 = datetime(2026, 10, 8, 10, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now


class Env:
    def __init__(self, **settings):
        self.clock = Clock()
        self.broker = MockBroker(clock=self.clock)
        base = dict(ticker_interval=None, reconcile_interval=None, timeout_reconcile_delay=0)
        self.settings = Settings(**{**base, **settings})
        self.app = create_app(self.settings, broker=self.broker, clock=self.clock)
        self.s = self.app.state
        self.events = self.s.hub.subscribe()

    def drain(self):
        out = []
        while not self.events.empty():
            out.append(self.events.get_nowait())
        return out

    async def tick(self, key, rupees):
        """Move the price and hand the tick to the rule engine (what the live feed does)."""
        t = self.broker.set_price(key, paise(rupees))
        await self.s.rule_engine.on_tick(t)
        return t


@pytest.fixture
def env():
    return Env()


def spec(**over):
    base = dict(kind="TRIGGER_ORDER", instrument="tcs", comparator="BELOW", price_rupees=3800, side="BUY", quantity=5)
    base.update(over)
    return CreateRuleRequest(**base)


def alert(**over):
    base = dict(kind="ALERT", instrument="hdfc bank", comparator="BELOW", percent=3, basis="AVG_BUY")
    base.update(over)
    return CreateRuleRequest(**base)


# ============================================================================================ #
# Creating rules
# ============================================================================================ #


async def test_buy_below_a_price_is_saved_as_a_limit_order_at_the_trigger(env):
    out = await env.s.rules.create(spec())
    assert out.status == "rule_created"
    rule = out.rule
    assert rule.status is RuleStatus.ACTIVE and rule.condition.trigger_price == paise(3800)
    assert rule.order_template.order_type is OrderType.LIMIT and rule.order_template.limit_price == paise(3800)
    assert rule.description == (
        "When TCS falls below ₹3,800.00, I'll prepare an order to buy 5 shares of TCS (limit ₹3,800.00) "
        "and ask you to approve it. Nothing is sent without your approval."
    )
    assert out.reply.cards[0].type == "rule"
    assert env.s.rule_store.get(rule.id).description == rule.description  # it is in the database
    assert AuditKind.RULE_CREATED in [e.kind for e in env.s.audit.list()]
    assert env.broker._orders == {}


@pytest.mark.parametrize(
    "side, comparator, price, expected_type",
    [
        ("BUY", "BELOW", 3800, OrderType.LIMIT),  # buy the dip: a limit at the trigger can fill
        ("SELL", "ABOVE", 4000, OrderType.LIMIT),  # sell into strength
        ("SELL", "BELOW", 3800, OrderType.MARKET),  # stop-loss: must fill as the price runs away
        ("BUY", "ABOVE", 4000, OrderType.MARKET),  # breakout buy
    ],
)
async def test_default_order_type_follows_what_can_actually_fill(env, side, comparator, price, expected_type):
    # TCS is held (5 shares) so a SELL rule can be made
    out = await env.s.rules.create(spec(side=side, comparator=comparator, price_rupees=price))
    assert out.rule.order_template.order_type is expected_type


async def test_an_explicit_price_and_type_are_respected(env):
    out = await env.s.rules.create(spec(order_type="LIMIT", limit_price_rupees=3790))
    assert out.rule.order_template.limit_price == paise(3790)
    out = await env.s.rules.create(spec(order_type="MARKET"))
    assert out.rule.order_template.order_type is OrderType.MARKET and out.rule.order_template.limit_price is None


async def test_percent_from_my_buy_price_is_resolved_when_the_rule_is_made(env):
    out = await env.s.rules.create(alert())
    cond = out.rule.condition
    assert cond.basis is RuleBasis.AVG_BUY and cond.reference_price == paise(1600)
    assert cond.trigger_price == paise(1552) and cond.change_pct == -3
    assert "falls 3% from your average buy price (₹1,600.00)" in out.rule.description
    assert out.rule.description.startswith("I'll tell you when HDFCBANK")


async def test_one_share_reads_naturally(env):
    out = await env.s.rules.create(spec(quantity=1))
    assert "buy 1 share of TCS" in out.rule.description


async def test_other_bases(env):
    prev = await env.s.rules.create(alert(instrument="infosys", comparator="ABOVE", percent=2, basis="PREV_CLOSE"))
    assert prev.rule.condition.trigger_price == paise("1468.80")  # 1440 + 2%
    now = await env.s.rules.create(alert(instrument="infosys", percent=3, basis=None))
    assert now.rule.condition.basis is RuleBasis.AT_CREATION and now.rule.condition.trigger_price == round(paise(1448) * 0.97)


async def test_a_rule_that_is_already_true_is_refused(env):
    out = await env.s.rules.create(spec(price_rupees=4000))  # TCS is at 3,912: already below 4,000
    assert out.status == "blocked" and "already at ₹3,912.00" in out.message and "straight away" in out.message
    assert env.s.rule_store.list() == []
    out = await env.s.rules.create(alert(comparator="BELOW", percent=None, price_rupees=5000, basis=None))
    assert out.status == "blocked"


async def test_buy_price_rules_need_a_holding(env):
    out = await env.s.rules.create(alert(instrument="reliance"))  # RELIANCE is only an intraday position
    assert out.status == "blocked" and "don't hold RELIANCE" in out.message


async def test_ambiguous_unknown_and_unsupported_instruments(env):
    amb = await env.s.rules.create(spec(instrument="tata"))
    assert amb.status == "needs_clarification" and amb.reply.cards[0].type == "ambiguity"
    assert (await env.s.rules.create(spec(instrument="zzzz"))).status == "not_found"
    blocked = await env.s.rules.create(alert(instrument="nifty", percent=None, price_rupees=25000, comparator="ABOVE", basis=None))
    assert blocked.status == "blocked" and "only equity" in blocked.message
    assert env.s.rule_store.list() == []


async def test_order_size_limits_apply_when_the_rule_is_made(env):
    out = await env.s.rules.create(spec(quantity=200_000))
    assert out.status == "blocked" and "100,000 units" in out.message


async def test_there_is_a_cap_on_active_rules():
    env = Env(max_active_rules=2)
    assert (await env.s.rules.create(spec())).status == "rule_created"
    assert (await env.s.rules.create(spec(instrument="itc", price_rupees=400))).status == "rule_created"
    third = await env.s.rules.create(spec(instrument="zomato", price_rupees=200))
    assert third.status == "blocked" and "2 active rules" in third.message


@pytest.mark.parametrize(
    "bad",
    [
        dict(price_rupees=None),  # neither price nor percent
        dict(percent=3),  # both
        dict(basis="AVG_BUY"),  # basis without percent
        dict(quantity=None),  # trigger order with no size
        dict(amount_rupees=10000),  # both size kinds
        dict(side=None),
        dict(approved=True),  # not a field
    ],
)
def test_malformed_rule_requests_are_rejected(bad):
    with pytest.raises(ValidationError):
        spec(**bad)


async def test_a_fall_of_a_hundred_percent_is_not_a_price_to_watch(env):
    out = await env.s.rules.create(alert(percent=100, basis="PREV_CLOSE"))
    assert out.status == "blocked" and "100%" in out.message
    ok = await env.s.rules.create(alert(comparator="ABOVE", percent=100, basis="PREV_CLOSE"))  # a doubling is fine
    assert ok.status == "rule_created"


def test_an_alert_cannot_carry_an_order():
    with pytest.raises(ValidationError):
        alert(side="BUY", quantity=1)


async def test_cancelling_a_rule(env):
    rule = (await env.s.rules.create(spec())).rule
    cancelled = await env.s.rules.cancel(rule.id)
    assert cancelled.status is RuleStatus.CANCELLED
    with pytest.raises(RuleNotActive):
        await env.s.rules.cancel(rule.id)
    with pytest.raises(RuleNotFound):
        await env.s.rules.cancel("r-nope")
    await env.tick("NSE:TCS", 3700)  # a cancelled rule does not fire
    assert env.s.pending.all() == []


async def test_a_rule_cancelled_while_prices_are_streaming_stops_firing(env):
    """The engine caches active rules per instrument; cancelling must drop the rule from that cache."""
    rule = (await env.s.rules.create(spec())).rule
    await env.tick("NSE:TCS", 3900)  # a tick has already been seen, so the cache is warm
    await env.s.rules.cancel(rule.id)
    await env.tick("NSE:TCS", 3700)
    assert env.s.pending.all() == []


async def test_a_rule_created_while_prices_are_streaming_is_picked_up(env):
    await env.tick("NSE:TCS", 3900)  # the cache is warm and empty
    await env.s.rules.create(spec())
    await env.tick("NSE:TCS", 3799)
    assert len(env.s.pending.all()) == 1


# ============================================================================================ #
# Firing: exactly once, and only ever an alert or a card
# ============================================================================================ #


async def test_an_alert_fires_once_and_tells_the_trader(env):
    rule = (await env.s.rules.create(alert())).rule
    env.drain()
    await env.tick("NSE:HDFCBANK", 1600)
    assert env.s.rule_store.get(rule.id).status is RuleStatus.ACTIVE  # above the trigger: nothing yet
    await env.tick("NSE:HDFCBANK", 1550)
    fired = env.s.rule_store.get(rule.id)
    assert fired.status is RuleStatus.FIRED and fired.fired_at == T0
    events = env.drain()
    event = next(e for e in events if e.type == "rule_fired")
    assert event.message == "Alert: HDFCBANK is now ₹1,550.00, below your trigger of ₹1,552.00." and event.pending is None
    assert "rule_update" in [e.type for e in events]
    assert [e.kind for e in env.s.audit.list(kind=AuditKind.RULE_FIRED)] == [AuditKind.RULE_FIRED]
    assert env.s.db.query("SELECT delivered FROM rules")[0]["delivered"] == 1


async def test_a_trigger_prepares_a_card_and_sends_nothing(env):
    rule = (await env.s.rules.create(spec())).rule
    env.drain()
    await env.tick("NSE:TCS", 3799)
    [card] = env.s.pending.awaiting_approval()
    assert card.client_order_id == f"rule-{rule.id}" and card.rule_id == rule.id
    assert card.state is PendingState.PENDING and card.quantity == 5 and card.limit_price == paise(3800)
    assert card.expires_at - card.created_at == timedelta(seconds=900)  # the trader may be away
    assert env.broker._orders == {}  # the rule did not trade
    assert env.s.db.query("SELECT * FROM executions") == []
    event = next(e for e in env.drain() if e.type == "rule_fired")
    assert event.pending.id == card.id
    assert event.message.startswith("Your rule fired: TCS is now ₹3,799.00, below your trigger of ₹3,800.00.")
    assert "You are buying 5 shares of Tata Consultancy Services Ltd (NSE)" in event.message


async def test_the_same_tick_five_times_fires_once(env):
    await env.s.rules.create(spec())
    tick = env.broker.set_price("NSE:TCS", paise(3799))
    for _ in range(5):
        await env.s.rule_engine.on_tick(tick)
    assert len(env.s.pending.all()) == 1
    assert len(env.s.audit.list(kind=AuditKind.RULE_FIRED)) == 1


async def test_a_run_of_different_ticks_below_the_trigger_fires_once(env):
    await env.s.rules.create(spec())
    for price in (3799, 3790, 3785, 3770, 3760):
        await env.tick("NSE:TCS", price)
    assert len(env.s.pending.all()) == 1


async def test_a_throttled_feed_that_skips_the_crossing_still_fires_on_the_next_snapshot(env):
    """021's market socket sends a snapshot at most every 300 ms, not every tick. The price may cross the
    trigger between two snapshots; the rule must still fire (once) when a snapshot shows it below."""
    await env.s.rules.create(spec())
    snapshots = []
    for price in (3810, 3790, 3795, 3805, 3792):  # the engine only ever sees every other price
        snapshots.append(env.broker.set_price("NSE:TCS", paise(price)))
    for tick in (snapshots[0], snapshots[2], snapshots[4]):
        await env.s.rule_engine.on_tick(tick)
    assert len(env.s.pending.all()) == 1
    assert len(env.s.audit.list(kind=AuditKind.RULE_FIRED)) == 1


async def test_a_dip_that_recovers_between_two_snapshots_is_not_seen(env):
    """The honest limit of a 300 ms feed: a dip that comes and goes inside one interval is invisible,
    so nothing fires. (Written down here so the README can say it.)"""
    await env.s.rules.create(spec())
    before = env.broker.set_price("NSE:TCS", paise(3810))
    env.broker.set_price("NSE:TCS", paise(3790))  # the dip, never delivered
    after = env.broker.set_price("NSE:TCS", paise(3805))
    await env.s.rule_engine.on_tick(before)
    await env.s.rule_engine.on_tick(after)
    assert env.s.pending.all() == []


async def test_a_late_out_of_order_tick_is_ignored(env):
    await env.s.rules.create(spec())
    t1 = env.broker.set_price("NSE:TCS", paise(3900))
    t2 = env.broker.set_price("NSE:TCS", paise(3899))
    t3 = env.broker.set_price("NSE:TCS", paise(3798))  # this one would fire...
    await env.s.rule_engine.on_tick(t1)
    await env.s.rule_engine.on_tick(t2)
    stale = Tick(instrument_key="NSE:TCS", ltp=paise(3700), seq=t1.seq, ts=T0)  # ...but a stale one arrives
    await env.s.rule_engine.on_tick(stale)
    assert env.s.pending.all() == []
    await env.s.rule_engine.on_tick(t3)
    assert len(env.s.pending.all()) == 1


async def test_two_engines_racing_on_the_same_tick_fire_it_once(env):
    """No in-memory dedupe to lean on: the database decides who fires."""
    rule = (await env.s.rules.create(spec())).rule
    other = RuleEngine(env.s.rule_store, env.s.cards, env.broker.read_only(), env.s.audit, env.s.hub, env.settings, env.clock)
    tick = env.broker.set_price("NSE:TCS", paise(3799))
    await asyncio.gather(env.s.rule_engine.on_tick(tick), other.on_tick(tick))
    assert len(env.s.pending.all()) == 1
    assert env.s.rule_store.get(rule.id).status is RuleStatus.FIRED


async def test_firing_directly_twice_still_creates_one_card(env):
    rule = (await env.s.rules.create(spec())).rule
    await env.s.rule_engine._fire(rule, paise(3799), 1)
    await env.s.rule_engine._fire(rule, paise(3799), 2)
    assert len(env.s.pending.all()) == 1


async def test_a_rule_that_gapped_through_the_trigger_says_so_on_the_card(env):
    await env.s.rules.create(spec())
    await env.tick("NSE:TCS", 3650)  # 3.9% below the 3,800 trigger; the limit is 2%
    [card] = env.s.pending.awaiting_approval()
    assert any("already 3.9% below your trigger" in w for w in card.warnings)


async def test_a_rule_that_fires_into_a_lock_is_reported_not_forced(env):
    env.broker.locks = AccountLocks(anchor_active=True, anchor_message="Not today.")
    rule = (await env.s.rules.create(spec())).rule  # creating a rule is allowed under Anchor
    env.drain()
    await env.tick("NSE:TCS", 3799)
    assert env.s.pending.all() == []
    event = next(e for e in env.drain() if e.type == "rule_fired")
    assert event.pending is None and "couldn't prepare the order" in event.message and "Not today." in event.message
    assert env.s.rule_store.get(rule.id).status is RuleStatus.FIRED  # fired once; not retried


# ---- a fired rule's card goes through the same approval gate ---------------------------------- #


async def test_approving_a_rule_card_sends_the_order(env):
    await env.s.rules.create(spec())
    await env.tick("NSE:TCS", 3799)
    [card] = env.s.pending.awaiting_approval()
    result = await env.s.approvals.approve(card.id, card.order_hash)
    assert result.outcome == "SENT" and result.order.status.value == "FILLED"
    tcs = next(h for h in await env.broker.get_holdings() if h.instrument.symbol == "TCS")
    assert tcs.quantity == 10


async def test_a_second_card_for_the_same_rule_can_never_be_sent(env):
    """Even if a card is duplicated (a crash, a retry), the rule's fixed client order id stops a second buy."""
    rule = (await env.s.rules.create(spec())).rule
    await env.tick("NSE:TCS", 3799)
    first = env.s.pending.awaiting_approval()[0]
    await env.s.approvals.approve(first.id, first.order_hash)

    fired = env.s.rule_store.get(rule.id)
    await env.s.rule_engine._deliver(fired, paise(3799))  # a duplicate delivery makes a second card
    second = next(p for p in env.s.pending.awaiting_approval() if p.id != first.id)
    assert second.client_order_id == first.client_order_id
    with pytest.raises(ApprovalError) as exc:
        await env.s.approvals.approve(second.id, second.order_hash)
    assert exc.value.code == "NOT_PENDING"
    assert len(env.broker._orders) == 1
    tcs = next(h for h in await env.broker.get_holdings() if h.instrument.symbol == "TCS")
    assert tcs.quantity == 10  # bought once


async def test_a_rule_card_survives_for_fifteen_minutes_then_expires(env):
    await env.s.rules.create(spec())
    await env.tick("NSE:TCS", 3799)
    [card] = env.s.pending.awaiting_approval()
    env.clock.now = T0 + timedelta(minutes=14)
    result = await env.s.approvals.approve(card.id, card.order_hash)
    assert result.outcome == "SENT"

    env2 = Env()
    await env2.s.rules.create(spec())
    await env2.tick("NSE:TCS", 3799)
    [late] = env2.s.pending.awaiting_approval()
    env2.clock.now = T0 + timedelta(minutes=16)
    with pytest.raises(ApprovalError) as exc:
        await env2.s.approvals.approve(late.id, late.order_hash)
    assert exc.value.code == "EXPIRED"


async def test_a_price_move_requotes_a_rule_card_and_keeps_the_rules_order_id(env):
    rule = (await env.s.rules.create(spec())).rule
    await env.tick("NSE:TCS", 3799)
    [card] = env.s.pending.awaiting_approval()
    env.broker.set_price("NSE:TCS", paise(3860))  # +1.6% since the card
    with pytest.raises(ApprovalError) as exc:
        await env.s.approvals.approve(card.id, card.order_hash)
    fresh = exc.value.pending
    assert exc.value.code == "REQUOTE_REQUIRED"
    assert fresh.client_order_id == f"rule-{rule.id}" and fresh.rule_id == rule.id
    assert fresh.expires_at - fresh.created_at == timedelta(seconds=900)


# ============================================================================================ #
# Surviving a restart
# ============================================================================================ #


def file_env(tmp_path, clock=None):
    url = f"sqlite:///{(tmp_path / 'rules.db').as_posix()}"
    e = Env(database_url=url)
    return e


async def test_rules_survive_a_restart_and_still_fire_once(tmp_path):
    first = file_env(tmp_path)
    rule = (await first.s.rules.create(spec())).rule
    first.s.db.close()  # the server stops

    second = file_env(tmp_path)  # a brand-new process on the same database file
    [loaded] = second.s.rule_store.list()
    assert loaded.id == rule.id and loaded.status is RuleStatus.ACTIVE
    assert loaded.condition.trigger_price == paise(3800) and loaded.order_template.quantity == 5
    await second.tick("NSE:TCS", 3799)
    [card] = second.s.pending.all()
    second.s.db.close()

    third = file_env(tmp_path)  # restarted again after it fired
    assert third.s.rule_store.get(rule.id).status is RuleStatus.FIRED
    await third.tick("NSE:TCS", 3700)
    # the card it made survives the restart (cards are saved), and the rule does not fire a second time
    assert [p.id for p in third.s.pending.all()] == [card.id]
    third.s.db.close()


async def test_a_rule_whose_delivery_was_lost_in_a_crash_is_delivered_once_on_recovery(env):
    """The rule is marked fired, then the process dies before the card is made."""
    rule = (await env.s.rules.create(spec())).rule
    env.s.rule_store.mark_fired(rule.id, T0)  # the crash happens right here
    assert env.s.pending.all() == [] and len(env.s.rule_store.undelivered_fired()) == 1

    env.broker.set_price("NSE:TCS", paise(3790))
    env.drain()
    assert await env.s.rule_engine.recover() == 1
    [card] = env.s.pending.awaiting_approval()
    assert card.client_order_id == f"rule-{rule.id}"
    event = next(e for e in env.drain() if e.type == "rule_fired")
    assert event.message.startswith("Your rule fired: While the app was restarting, TCS is now")
    assert await env.s.rule_engine.recover() == 0  # delivered; never again
    assert len(env.s.pending.all()) == 1


async def test_recovery_waits_while_the_broker_is_unreachable(env):
    rule = (await env.s.rules.create(spec())).rule
    env.s.rule_store.mark_fired(rule.id, T0)
    env.broker.network_down = True
    assert await env.s.rule_engine.recover() == 0
    assert len(env.s.rule_store.undelivered_fired()) == 1
    env.broker.network_down = False
    assert await env.s.rule_engine.recover() == 1


# ============================================================================================ #
# The HTTP routes and the live feed
# ============================================================================================ #


@pytest.fixture
def client():
    settings = Settings(ticker_interval=None, reconcile_interval=None, timeout_reconcile_delay=0, account_push_interval=3600)
    with TestClient(create_app(settings, broker=MockBroker(clock=lambda: T0), clock=lambda: T0)) as c:
        yield c


def on_loop(client, fn, *args, **kwargs):
    return client.portal.call(functools.partial(fn, *args, **kwargs))


RULE_BODY = dict(kind="TRIGGER_ORDER", instrument="tcs", comparator="BELOW", price_rupees=3800, side="BUY", quantity=5)


def test_rules_over_http(client):
    reply = client.post("/api/rules", json=RULE_BODY).json()
    assert reply["cards"][0]["type"] == "rule" and "Nothing is sent without your approval" in reply["text"]
    rule = reply["cards"][0]["rule"]
    assert rule["status"] == "ACTIVE" and rule["condition"]["trigger_price"] == paise(3800)

    assert [r["id"] for r in client.get("/api/rules").json()] == [rule["id"]]
    assert client.get("/api/rules?status=FIRED").json() == []
    assert client.delete(f"/api/rules/{rule['id']}").json()["status"] == "CANCELLED"
    assert client.delete(f"/api/rules/{rule['id']}").status_code == 409
    assert client.delete("/api/rules/r-nope").status_code == 404
    assert client.post("/api/rules", json={**RULE_BODY, "approved": True}).status_code == 422
    assert client.post("/api/rules", json={**RULE_BODY, "percent": 3}).status_code == 422
    assert client.post("/api/rules", json={**RULE_BODY, "price_rupees": 4000}).json()["cards"][0]["level"] == "blocked"


def test_a_new_websocket_client_starts_with_the_rules(client):
    client.post("/api/rules", json=RULE_BODY)
    with client.websocket_connect("/ws") as ws:
        snap = ws.receive_json()
    assert [r["kind"] for r in snap["rules"]] == ["TRIGGER_ORDER"]


def test_the_live_feed_fires_the_rule_and_the_client_hears_about_it(client):
    client.post("/api/rules", json=RULE_BODY)
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        on_loop(client, client.app.state.broker.set_price, "NSE:TCS", paise(3799))
        seen = []
        for _ in range(12):
            seen.append(ws.receive_json())
            if seen[-1]["type"] == "rule_fired":
                break
    fired = seen[-1]
    assert fired["type"] == "rule_fired" and fired["pending"]["client_order_id"].startswith("rule-r-")
    assert fired["pending"]["quantity"] == 5 and fired["rule"]["status"] == "FIRED"
    assert client.get("/api/orders").json() == []  # no order until the card is approved
    p = client.get("/api/pending").json()["orders"][0]
    done = client.post(f"/api/approvals/{p['id']}/approve", json={"order_hash": p["order_hash"]}).json()
    assert done["outcome"] == "SENT"


# ============================================================================================ #
# Through the chat
# ============================================================================================ #


async def test_the_standing_instruction_from_the_pitch_by_chat(env):
    reply = await env.s.copilot.handle("Buy 5 TCS if it falls below 3800")
    assert [c.type for c in reply.cards] == ["rule"]
    assert reply.text.startswith("When TCS falls below ₹3,800.00, I'll prepare an order to buy 5 shares of TCS")
    assert env.s.pending.all() == [] and env.broker._orders == {}
    assert len(env.s.rule_store.list(RuleStatus.ACTIVE)) == 1


async def test_the_alert_from_the_pitch_by_chat(env):
    reply = await env.s.copilot.handle("Alert me if HDFC Bank drops 3% from my buy price")
    assert reply.text.startswith("I'll tell you when HDFCBANK falls 3% from your average buy price (₹1,600.00)")
    [rule] = env.s.rule_store.list()
    assert rule.condition.trigger_price == paise(1552) and rule.kind.value == "ALERT"


async def test_listing_and_cancelling_rules_by_chat(env):
    assert (await env.s.copilot.handle("show my rules")).text == "You have no standing rules."
    await env.s.copilot.handle("Buy 5 TCS if it falls below 3800")
    listing = (await env.s.copilot.handle("show my rules")).text
    [rule] = env.s.rule_store.list()
    assert f"{rule.id} [ACTIVE]" in listing
    cancelled = await env.s.copilot.handle(f"cancel rule {rule.id}")
    assert cancelled.text.startswith("Cancelled: When TCS falls below")
    assert env.s.rule_store.get(rule.id).status is RuleStatus.CANCELLED
    assert "can't find a rule" in (await env.s.copilot.handle("cancel rule r-nope")).text


async def test_a_rule_that_is_already_true_is_explained_in_chat(env):
    reply = await env.s.copilot.handle("Buy 5 TCS if it falls below 4000")
    assert reply.cards[0].level == "blocked" and "straight away" in reply.text


async def test_chat_asks_which_tata_for_a_rule_too(env):
    reply = await env.s.copilot.handle("buy 5 tata if it falls below 100")
    assert [c.type for c in reply.cards] == ["ambiguity"] and env.s.rule_store.list() == []


async def test_a_fooled_model_can_only_create_rules_that_prepare_cards(env):
    """A hostile model creates a 'sell everything' rule. When it fires there is still only a card."""
    llm_turns = [
        LLMTurn(tool_calls=[ToolCall("c1", "create_rule", dict(
            kind="TRIGGER_ORDER", instrument="tcs", comparator="BELOW", price_rupees=3800, side="SELL", quantity=5,
            approved=True))]),  # tries to smuggle approval: rejected
        LLMTurn(tool_calls=[ToolCall("c2", "create_rule", dict(
            kind="TRIGGER_ORDER", instrument="tcs", comparator="BELOW", price_rupees=3800, side="SELL", quantity=5))]),
        LLMTurn(text="Done, I will sell everything automatically."),
    ]

    class Scripted:
        async def complete(self, *, system, messages, tools):
            return llm_turns.pop(0)

    s = env.s
    copilot = Copilot(Scripted(), build_tools(), env.broker.read_only(), s.cards, s.rules, PlanAssistant(s.plans), s.audit, env.clock)
    reply = await copilot.handle("sell 5 tcs automatically if it falls below 3800")
    assert "automatically" not in reply.text  # the model's claim never reaches the trader
    assert reply.text.startswith("When TCS falls below")
    [rule] = s.rule_store.list()  # only the well-formed one exists
    await env.tick("NSE:TCS", 3799)
    [card] = s.pending.awaiting_approval()
    assert card.side.value == "SELL" and card.state is PendingState.PENDING
    assert env.broker._orders == {} and s.db.query("SELECT * FROM executions") == []


def test_create_rule_tool_schema_is_closed():
    schema = next(t.spec.input_schema for n, t in build_tools().items() if n == "create_rule")
    assert schema["additionalProperties"] is False
    assert {"kind", "instrument", "comparator"} <= set(schema["required"])
