"""End to end through the real app: card -> approve -> broker, against the mock broker.

Grouped by the PS's four failure modes: misread request, network failure, price drift,
malicious instructions. Plus the locks and the audit trail.
"""

import functools
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.broker.base import BrokerTimeout
from app.broker.mock import MockBroker
from app.config import Settings
from app.main import create_app
from app.schemas import PendingState, RejectionReason, paise

T0 = datetime(2026, 10, 8, 10, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def broker(clock):
    return MockBroker(clock=clock)


@pytest.fixture
def client(broker, clock):
    settings = Settings(ticker_interval=None, reconcile_interval=None, timeout_reconcile_delay=0, account_push_interval=3600)
    with TestClient(create_app(settings, broker=broker, clock=clock)) as c:
        yield c


def on_loop(client, fn, *args, **kwargs):
    return client.portal.call(functools.partial(fn, *args, **kwargs))


def place(symbol="INFY", **over):
    body = dict(action="PLACE", instrument_ref=symbol, side="BUY", quantity=10, order_type="LIMIT", limit_price=paise(1450))
    body.update(over)
    return body


def preview(client, body=None, **over):
    r = client.post("/api/orders/preview", json=body or place(**over))
    assert r.status_code == 200, r.text
    return r.json()


def card(client, **over):
    reply = preview(client, **over)
    assert reply["cards"][0]["type"] == "pending_order", reply
    return reply["cards"][0]["pending"]


def approve(client, p, order_hash=None):
    return client.post(f"/api/approvals/{p['id']}/approve", json={"order_hash": order_hash or p["order_hash"]})


def audit_kinds(client):
    return [e["kind"] for e in reversed(client.get("/api/audit").json())]


# ============================================================================================ #
# The happy path and the card itself
# ============================================================================================ #


def test_the_pitch_example_end_to_end(client):
    reply = preview(client)
    p = reply["cards"][0]["pending"]
    assert p["instrument"]["symbol"] == "INFY" and p["quantity"] == 10 and p["limit_price"] == paise(1450)
    assert p["state"] == "PENDING" and p["ref_ltp"] == paise(1448)
    assert p["charges"]["total"] == 2273 and p["charges"]["break_even_price"] > paise(1450)
    assert p["est_total"] == paise(14500) + 2273
    assert reply["text"] == (
        "You are buying 10 shares of Infosys Ltd (NSE) at up to ₹1,450.00, "
        "total about ₹14,522.73 including charges."
    )

    # nothing is sent until the click
    assert client.get("/api/orders").json() == []
    result = approve(client, p)
    assert result.status_code == 200
    body = result.json()
    assert body["outcome"] == "SENT" and body["order"]["status"] == "FILLED"
    assert body["pending"]["state"] == "SENT"
    assert [h["quantity"] for h in client.get("/api/account").json()["holdings"] if h["instrument"]["symbol"] == "INFY"] == [30]
    assert len(client.get("/api/orders").json()) == 1


def test_double_click_sends_once(client):
    p = card(client)
    first, second = approve(client, p), approve(client, p)
    assert first.status_code == 200
    assert second.status_code == 409 and second.json()["code"] == "NOT_PENDING"
    assert len(client.get("/api/orders").json()) == 1


def test_a_card_that_was_declined_cannot_be_approved(client):
    p = card(client)
    assert client.post(f"/api/approvals/{p['id']}/reject").status_code == 200
    assert approve(client, p).json()["code"] == "NOT_PENDING"
    assert client.get("/api/orders").json() == []


# ============================================================================================ #
# Failure mode 1: misread request
# ============================================================================================ #


def test_buy_tata_asks_which_tata_and_creates_no_card(client):
    reply = preview(client, place("tata"))
    assert reply["cards"][0]["type"] == "ambiguity"
    assert {c["symbol"] for c in reply["cards"][0]["candidates"]} == {"TCS", "TATAMOTORS", "TATASTEEL"}
    assert client.get("/api/pending").json()["orders"] == []


def test_unknown_instrument_is_a_question_not_a_guess(client):
    reply = preview(client, place("zzzz"))
    assert reply["cards"][0]["type"] == "notice" and "couldn't find" in reply["text"]
    assert client.get("/api/pending").json()["orders"] == []


def test_the_wrong_quantity_cannot_ride_on_an_old_approval(client):
    """The trader saw 10 shares. If the card now says 100, the old hash fails and the card is voided."""
    p = card(client)
    store = client.app.state.pending
    store.put(store.get(p["id"]).model_copy(update={"quantity": 100}))
    r = approve(client, p)
    assert r.status_code == 409 and r.json()["code"] == "HASH_MISMATCH"
    assert client.get("/api/orders").json() == []


def test_amount_based_order_is_converted_in_code_and_explained(client):
    reply = preview(client, place(quantity=None, amount_paise=paise(10000)))
    p = reply["cards"][0]["pending"]
    assert p["quantity"] == 6  # Rs 10,000 / Rs 1,450 limit price, rounded down
    assert any("6 shares" in w and "10,000" in w for w in p["warnings"])


def test_amount_too_small_for_one_share_is_blocked(client):
    reply = preview(client, place(quantity=None, amount_paise=paise(100)))
    assert reply["cards"][0]["type"] == "notice" and reply["cards"][0]["level"] == "blocked"


def test_market_order_is_sent_as_a_protected_limit(client):
    p = card(client, order_type="MARKET", limit_price=None)
    assert p["limit_price"] is None
    assert p["protection_price"] == paise("1462.50")  # 1% above Rs 1,448, rounded up to the tick
    result = approve(client, p).json()
    assert result["order"]["status"] == "FILLED"
    assert result["order"]["avg_fill_price"] <= p["protection_price"]


def test_sell_more_than_you_hold_is_blocked_before_any_card(client):
    reply = preview(client, place(side="SELL", quantity=21))
    assert reply["cards"][0]["level"] == "blocked" and "hold 20" in reply["text"]


def test_selling_something_you_do_not_own_says_so(client):
    reply = preview(client, place("ITC", side="SELL", quantity=1000, limit_price=paise(415)))
    assert "hold 100" in reply["text"]
    broker_has_none = preview(client, place("RELIANCE", side="SELL", quantity=1, limit_price=paise(2915)))
    assert "don't hold any" in broker_has_none["text"]  # RELIANCE is only an intraday position


def test_limit_far_from_the_price_and_marketable_limits_carry_plain_warnings(client):
    far = card(client, limit_price=paise(1200))
    assert any("below the current price" in w for w in far["warnings"])
    marketable = card(client, limit_price=paise(1460))
    assert any("fill straight away at the market price" in w for w in marketable["warnings"])


def test_not_enough_cash_is_a_warning_the_broker_has_the_final_word(client):
    p = card(client, quantity=500, limit_price=paise(1450))
    assert any("available cash" in w for w in p["warnings"])


# ---- hard limits, through the card builder -------------------------------------------------- #


@pytest.mark.parametrize(
    "over, needle",
    [
        (dict(quantity=100_001), "100,001 units"),
        (dict(quantity=50_000, limit_price=paise(1450)), "per order"),  # Rs 72.5 crore
        (dict(limit_price=paise("1450.03")), "steps of"),
        (dict(limit_price=paise(900)), "price band"),
    ],
)
def test_limits_block_the_card_and_say_why(client, over, needle):
    reply = preview(client, **over)
    assert reply["cards"][0]["type"] == "notice" and reply["cards"][0]["level"] == "blocked"
    assert needle in reply["text"]
    assert client.get("/api/pending").json()["orders"] == []
    assert "LIMIT_BLOCKED" in audit_kinds(client)


def test_f_and_o_and_indices_are_not_orderable(client):
    reply = preview(client, place("NIFTY", quantity=1))
    assert reply["cards"][0]["level"] == "blocked" and "only equity" in reply["text"]


def test_suspended_stock_is_blocked(client, broker):
    halt = broker.add_suspended_instrument()
    assert "suspended" in preview(client, place(halt.symbol, limit_price=paise(50)))["text"]


# ============================================================================================ #
# Failure mode 3: prices change between the card and the click
# ============================================================================================ #


def test_price_jump_over_the_limit_replaces_the_card_and_sends_nothing(client, broker):
    p = card(client)
    on_loop(client, broker.set_price, "NSE:INFY", paise(1466))  # +1.2% from Rs 1,448
    r = approve(client, p)
    assert r.status_code == 409
    body = r.json()
    assert body["code"] == "REQUOTE_REQUIRED" and "1,448.00" in body["message"] and "1,466.00" in body["message"]
    fresh = body["pending"]
    assert fresh["id"] != p["id"] and fresh["ref_ltp"] == paise(1466) and fresh["state"] == "PENDING"
    assert fresh["client_order_id"] != p["client_order_id"] and fresh["order_hash"] != p["order_hash"]
    assert client.get("/api/orders").json() == []  # nothing sent
    assert client.app.state.pending.get(p["id"]).state is PendingState.REQUOTE_REQUIRED
    # the old approval cannot be reused, only the fresh card
    assert approve(client, p).json()["code"] == "NOT_PENDING"
    assert approve(client, fresh).json()["outcome"] == "SENT"


def test_a_small_move_inside_the_drift_limit_still_goes_through(client, broker):
    p = card(client)
    on_loop(client, broker.set_price, "NSE:INFY", paise(1455))  # +0.5%
    assert approve(client, p).json()["outcome"] == "SENT"


def test_a_downward_move_over_the_limit_also_requotes(client, broker):
    p = card(client)
    on_loop(client, broker.set_price, "NSE:INFY", paise(1430))  # -1.2%
    assert approve(client, p).json()["code"] == "REQUOTE_REQUIRED"


def test_a_requoted_market_order_gets_a_new_protection_price(client, broker):
    p = card(client, order_type="MARKET", limit_price=None)
    on_loop(client, broker.set_price, "NSE:INFY", paise(1470))
    fresh = approve(client, p).json()["pending"]
    assert fresh["protection_price"] > p["protection_price"]


def test_card_expires_after_sixty_seconds(client, clock):
    p = card(client)
    clock.advance(seconds=59)
    clock.advance(seconds=1)
    r = approve(client, p)
    assert r.status_code == 409 and r.json()["code"] == "EXPIRED"
    assert client.get("/api/orders").json() == []
    assert client.get("/api/pending").json()["orders"] == []


# ============================================================================================ #
# Failure mode 2: the network drops
# ============================================================================================ #


def test_timeout_after_the_broker_accepted_is_found_not_resent(client, broker):
    p = card(client)
    broker.timeout_next_place(accepted=True)
    body = approve(client, p).json()
    assert body["outcome"] == "SENT" and "Confirmed in your order book" in body["message"]
    assert len(client.get("/api/orders").json()) == 1  # exactly one order, never two
    assert "RECONCILE" in audit_kinds(client) and "BROKER_TIMEOUT" in audit_kinds(client)


def test_timeout_before_the_broker_saw_it_is_unknown_and_never_retried(client, broker):
    p = card(client)
    broker.timeout_next_place(accepted=False)
    body = approve(client, p).json()
    assert body["outcome"] == "UNKNOWN" and body["order"] is None
    assert "NOT been re-sent" in body["message"]
    assert client.get("/api/orders").json() == []  # and the app did not retry on its own
    assert [r["status"] for r in client.app.state.executor.unresolved()] == ["UNKNOWN"]


def test_network_down_during_the_send_is_unknown(client, broker):
    p = card(client)
    broker.network_down = True
    approve_result = approve(client, p)
    # the recheck cannot verify anything, so nothing is sent and the card is voided
    assert approve_result.status_code == 409 and approve_result.json()["code"] == "BLOCKED"
    broker.network_down = False
    assert client.get("/api/orders").json() == []
    assert client.app.state.pending.get(p["id"]).state is PendingState.VOID


def test_reconcile_marks_a_never_placed_order_after_the_grace_period(client, broker, clock):
    p = card(client)
    broker.timeout_next_place(accepted=False)
    approve(client, p)
    assert client.post("/api/executions/reconcile").json() == {"resolved": 0, "unresolved": 1}  # too early to say
    clock.advance(seconds=31)
    assert client.post("/api/executions/reconcile").json() == {"resolved": 0, "unresolved": 1}  # 021 gives no id; we wait longer
    clock.advance(seconds=100)
    assert client.post("/api/executions/reconcile").json() == {"resolved": 1, "unresolved": 0}
    assert "never placed" in client.get("/api/audit?kind=RECONCILE").json()[0]["summary"]
    assert client.get("/api/orders").json() == []  # still nothing sent


def test_reconcile_finds_an_order_that_was_accepted_but_whose_reply_was_lost(client, broker):
    p = card(client)
    broker.timeout_next_place(accepted=True)
    broker.network_down = False
    approve(client, p)
    # simulate a crash: the row is still SENDING/UNKNOWN as far as a fresh process knows
    client.app.state.db.execute("UPDATE executions SET status = 'UNKNOWN', broker_order_id = NULL")
    assert client.post("/api/executions/reconcile").json() == {"resolved": 1, "unresolved": 0}
    [row] = client.app.state.db.query("SELECT status, broker_order_id FROM executions")
    assert row["status"] == "SENT" and row["broker_order_id"]


def test_reconcile_leaves_everything_alone_while_the_broker_is_unreachable(client, broker, clock):
    p = card(client)
    broker.timeout_next_place(accepted=False)
    approve(client, p)
    clock.advance(seconds=300)
    broker.network_down = True
    assert client.post("/api/executions/reconcile").json() == {"resolved": 0, "unresolved": 1}


def test_the_same_client_order_id_can_never_be_sent_twice(client):
    """Defence in depth: even a second card with a reused id cannot reach the broker."""
    first = card(client)
    approve(client, first)
    store = client.app.state.pending
    clone = store.get(first["id"]).model_copy(
        update={"id": "pend-clone", "state": PendingState.PENDING, "created_at": T0, "expires_at": T0 + timedelta(seconds=60)}
    )
    store.put(clone)
    r = approve(client, {"id": "pend-clone", "order_hash": clone.order_hash})
    assert r.status_code == 409
    assert len(client.get("/api/orders").json()) == 1


# ---- broker refusals are reported, not hidden ------------------------------------------------ #


def test_broker_rejection_is_an_honest_outcome(client, broker):
    p = card(client)
    broker.reject_next(RejectionReason.RISK_CHECK)
    body = approve(client, p).json()
    assert body["outcome"] == "REJECTED" and body["order"]["rejection_reason"] == "RISK_CHECK"
    assert body["pending"]["state"] == "SENT"
    assert [a["status"] for a in map(dict, client.app.state.db.query("SELECT status FROM executions"))] == ["REJECTED"]


# ============================================================================================ #
# Locks: Anchor and Co-Captain, at card time and at click time
# ============================================================================================ #


def test_anchor_refuses_the_card_and_shows_the_traders_own_message(client, broker):
    from app.schemas import AccountLocks

    broker.locks = AccountLocks(anchor_active=True, anchor_message="Don't revenge trade.")
    reply = preview(client)
    assert reply["cards"][0]["level"] == "blocked" and "Don't revenge trade." in reply["text"]
    assert client.get("/api/pending").json()["orders"] == []
    assert "LOCK_BLOCKED" in audit_kinds(client)
    # reads keep working under Anchor
    assert client.get("/api/account").status_code == 200


def test_anchor_switched_on_after_the_card_was_shown_still_stops_the_order(client, broker):
    from app.schemas import AccountLocks

    p = card(client)
    broker.locks = AccountLocks(anchor_active=True, anchor_message="Pause.")
    r = approve(client, p)
    assert r.status_code == 409 and r.json()["code"] == "BLOCKED" and "Pause." in r.json()["message"]
    assert client.get("/api/orders").json() == []
    assert client.app.state.pending.get(p["id"]).state is PendingState.VOID


def test_stock_suspended_after_the_card_was_shown_stops_the_order(client, broker):
    p = card(client)
    broker._instruments["NSE:INFY"] = broker._instruments["NSE:INFY"].model_copy(update={"suspended": True})
    assert "suspended" in approve(client, p).json()["message"]
    assert client.get("/api/orders").json() == []


# ============================================================================================ #
# Modify and cancel go through the same gate
# ============================================================================================ #


def open_order(client, broker, limit="1400"):
    p = card(client, limit_price=paise(limit))
    body = approve(client, p).json()
    assert body["order"]["status"] == "OPEN"
    return body["order"]


def amend(client, **body):
    reply = preview(client, body)
    return reply["cards"][0]["pending"] if reply["cards"][0]["type"] == "pending_order" else reply


def test_modify_goes_through_approval_and_fills(client, broker):
    order = open_order(client, broker)
    card_ = amend(client, action="MODIFY", target_order_id=order["order_id"], limit_price=paise(1450))
    assert card_["action"] == "MODIFY" and card_["limit_price"] == paise(1450)
    assert client.get("/api/orders").json()[0]["limit_price"] == paise(1400)  # unchanged until approved
    result = approve(client, card_).json()
    assert result["order"]["status"] == "FILLED"


def test_cancel_goes_through_approval(client, broker):
    order = open_order(client, broker)
    card_ = amend(client, action="CANCEL", target_order_id=order["order_id"])
    assert card_["action"] == "CANCEL"
    assert client.get("/api/orders").json()[0]["status"] == "OPEN"
    assert approve(client, card_).json()["order"]["status"] == "CANCELLED"


def test_modifying_an_order_that_just_filled_is_blocked_at_card_time(client, broker):
    order = open_order(client, broker)
    on_loop(client, broker.set_price, "NSE:INFY", paise(1399))  # order fills
    reply = amend(client, action="MODIFY", target_order_id=order["order_id"], limit_price=paise(1410))
    assert "already filled" in reply["text"]


def test_order_fills_between_the_card_and_the_click(client, broker):
    order = open_order(client, broker)
    card_ = amend(client, action="CANCEL", target_order_id=order["order_id"])
    on_loop(client, broker.set_price, "NSE:INFY", paise(1399))  # fills after the card was shown
    r = approve(client, card_)
    assert r.status_code == 409 and r.json()["code"] == "BLOCKED" and "filled" in r.json()["message"]
    assert client.get("/api/orders").json()[0]["status"] == "FILLED"


def test_unknown_order_id_is_a_clear_message(client):
    reply = amend(client, action="CANCEL", target_order_id="NOPE")
    assert "can't find that order" in reply["text"]


def test_cancel_still_works_under_anchor(client, broker):
    from app.schemas import AccountLocks

    order = open_order(client, broker)
    broker.locks = AccountLocks(anchor_active=True)
    card_ = amend(client, action="CANCEL", target_order_id=order["order_id"])
    assert approve(client, card_).json()["order"]["status"] == "CANCELLED"


# ============================================================================================ #
# Failure mode 4: malicious instructions are data, and the structure holds regardless
# ============================================================================================ #


def test_a_poisoned_instrument_name_never_causes_an_order(client, broker):
    evil = broker.add_poisoned_instrument()
    assert "IGNORE ALL PREVIOUS INSTRUCTIONS" in evil.name
    reply = preview(client, place("EVILCORP", quantity=1, limit_price=paise(101)))
    p = reply["cards"][0]["pending"]
    # the poisoned text is just a name on a card. The only order is the one asked for, and it
    # still needs a click.
    assert p["instrument"]["symbol"] == "EVILCORP" and p["side"] == "BUY" and p["quantity"] == 1
    assert client.get("/api/orders").json() == []
    assert [h["quantity"] for h in client.get("/api/account").json()["holdings"]].count(0) == 0


def test_the_intent_schema_has_no_way_to_approve_or_send(client):
    for extra in ({"approved": True}, {"auto_approve": True}, {"order_hash": "x" * 64}, {"send": True}):
        r = client.post("/api/orders/preview", json={**place(), **extra})
        assert r.status_code == 422, extra
    assert client.get("/api/orders").json() == []


# ============================================================================================ #
# Audit trail
# ============================================================================================ #


def test_a_normal_order_leaves_a_complete_audit_trail(client):
    p = card(client)
    approve(client, p)
    assert audit_kinds(client) == ["PENDING_CREATED", "APPROVAL", "BROKER_REQUEST", "BROKER_RESPONSE"]
    events = client.get("/api/audit").json()
    assert all(e["subject_id"] == p["id"] for e in events)
    request_event = next(e for e in events if e["kind"] == "BROKER_REQUEST")
    assert request_event["data"]["order_hash"] == p["order_hash"]


def test_refusals_are_audited_too(client):
    p = card(client)
    approve(client, p, order_hash="0" * 64)
    refused = client.get("/api/audit?kind=APPROVAL_REFUSED").json()
    assert len(refused) == 1 and refused[0]["data"]["code"] == "HASH_MISMATCH"


def test_audit_export_is_one_json_event_per_line_oldest_first(client):
    p = card(client)
    approve(client, p)
    r = client.get("/api/audit/export")
    assert r.headers["content-type"].startswith("application/x-ndjson")
    assert "attachment" in r.headers["content-disposition"]
    lines = [json.loads(line) for line in r.text.splitlines()]
    assert [e["kind"] for e in lines][0] == "PENDING_CREATED" and len(lines) == 4


def test_audit_is_durable_across_a_restart(tmp_path, broker, clock):
    url = f"sqlite:///{(tmp_path / 'td.db').as_posix()}"
    settings = Settings(database_url=url, ticker_interval=None, reconcile_interval=None, timeout_reconcile_delay=0)
    with TestClient(create_app(settings, broker=broker, clock=clock)) as c:
        p = card(c)
        broker.timeout_next_place(accepted=False)
        approve(c, p)
    # a brand-new app process on the same database file sees the history and the unresolved send
    with TestClient(create_app(settings, broker=MockBroker(clock=clock), clock=clock)) as c2:
        kinds = [e["kind"] for e in c2.get("/api/audit").json()]
        assert "BROKER_TIMEOUT" in kinds and "PENDING_CREATED" in kinds
        assert [r["status"] for r in c2.app.state.executor.unresolved()] == ["UNKNOWN"]


def test_live_events_follow_the_order(client, broker):
    p = card(client)
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        approve(client, p)
        # 3 audit rows (approval, broker request, broker response), the sent card, the order, the account
        types = [ws.receive_json()["type"] for _ in range(6)]
    assert sorted(types) == sorted(
        ["audit_event"] * 3 + ["pending_updated", "order_update", "account_update"]
    )
