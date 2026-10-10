"""The real adapter, driven against a fake 021 built from the API guide (tests/fake021.py).

Not a substitute for running against the live sandbox, which has not been done yet."""

import asyncio
import gzip
import json
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

from app.broker.base import BrokerRejected, BrokerTimeout
from app.broker.zerotwoone import ZeroTwoOneAdapter
from app.broker.zerotwoone.adapter import BrokerAuthFailed, websocket_base
from app.schemas import (
    MatchKind,
    OrderAction,
    OrderStatus,
    OrderType,
    PendingOrder,
    PendingState,
    Product,
    RejectionReason,
    SentOrderSpec,
    Side,
    Validity,
    paise,
)
from fake021 import Fake021, FakeConnector, chain_packet, full_index, full_nse_cash, ltp_packet

NOW = datetime(2026, 10, 9, 10, 0, tzinfo=timezone.utc)


@pytest.fixture
def fake():
    return Fake021()


@pytest.fixture
def conn():
    return FakeConnector()


def build(fake, conn, tmp_path=None, **kw):
    return ZeroTwoOneAdapter(
        username="HACK1234", password="pw", http=fake.client(), connect=conn, clock=lambda: NOW,
        cache_dir=tmp_path, retry_delay=0.001, price_wait=0.05, **kw,
    )


@pytest.fixture
async def adapter(fake, conn):
    a = build(fake, conn)
    await a.start()
    yield a
    await a.close()


def feed_prices(conn, prices: dict[int, int], socket=0):
    for token, ltp in prices.items():
        conn.market[socket].push(ltp_packet(1, token, ltp))


async def settle():
    await asyncio.sleep(0.02)


async def approved(adapter, symbol="INFY", *, side=Side.BUY, qty=10, limit="1450", **over):
    inst = await adapter.get_instrument(f"NSE:{symbol}")
    fields = dict(
        id="p1", action=OrderAction.PLACE, instrument=inst, side=side, quantity=qty, order_type=OrderType.LIMIT,
        limit_price=paise(limit), product=Product.CNC, validity=Validity.DAY, client_order_id="c1",
        ref_ltp=paise(1448), created_at=NOW, expires_at=NOW + timedelta(seconds=60),
    )
    fields.update(over)
    return PendingOrder(**fields).transition(PendingState.APPROVED)


# ============================================================================================ #
# Logging in and the instrument list
# ============================================================================================ #


async def test_start_logs_in_once_loads_the_gzipped_instrument_list_and_caches_it(fake, conn, tmp_path):
    a = build(fake, conn, tmp_path)
    await a.start()
    assert fake.logins == 1 and a.master.get("NSE:INFY").token == 1594
    cached = tmp_path / "instruments-2026-10-09.csv"
    assert cached.exists()
    await a.close()

    again = build(fake, conn, tmp_path)  # a restart the same day
    before = len([r for r in fake.requests if r[1] == "/instruments"])
    await again.start()
    assert len([r for r in fake.requests if r[1] == "/instruments"]) == before  # not downloaded again
    await again.close()


async def test_a_wrong_password_stops_start_with_a_clear_message_and_never_echoes_it(conn):
    fake = Fake021(password="right")
    a = ZeroTwoOneAdapter(username="HACK1234", password="wrong-secret", http=fake.client(), connect=conn, cache_dir=None)
    with pytest.raises(BrokerAuthFailed) as exc:
        await a.start()
    assert "UCC and password" in str(exc.value) and "wrong-secret" not in str(exc.value)
    assert "wrong-secret" not in repr(a.__dict__.get("_http", ""))


async def test_a_token_that_was_revoked_logs_in_again_and_the_call_goes_through(adapter, fake):
    fake.token = "someone-else-logged-in"  # logging in elsewhere revokes ours
    assert await adapter.get_orders() == []
    assert fake.logins == 2


async def test_an_expired_token_on_an_order_is_refreshed_and_the_order_placed_exactly_once(adapter, fake):
    fake.token = "revoked"
    placed = await adapter.place_order(await approved(adapter))
    assert placed.order_id == "1042" and len(fake.orders) == 1
    assert [r for r in fake.requests if r[0] == "POST" and r[1] == "/orders"].__len__() == 2  # one refused 401, one processed


async def test_when_another_copy_keeps_taking_the_login_we_say_so_and_do_not_loop_forever(adapter, fake):
    fake.lock_out = True
    logins = fake.logins
    with pytest.raises(BrokerTimeout, match="only one login per account"):
        await adapter.get_orders()
    with pytest.raises(BrokerRejected, match="only one login per account"):
        await adapter.place_order(await approved(adapter))
    assert fake.logins - logins <= 4  # a couple of fresh logins, never an endless loop
    assert not fake.orders  # and an order refused for that reason was never processed


def test_the_websocket_address_is_derived_from_the_rest_address():
    assert websocket_base("https://devapi.021.trade/api/developer-api/v1") == "wss://devapi.021.trade/api/developer/websocket"


# ============================================================================================ #
# Placing orders
# ============================================================================================ #


async def test_a_buy_is_posted_in_paise_with_a_positive_qty_and_no_client_id(adapter, fake):
    placed = await adapter.place_order(await approved(adapter))
    [(_, _, body)] = [r for r in fake.requests if r[0] == "POST" and r[1] == "/orders"]
    assert body == {"exchange": "NSE", "token": 1594, "qty": 10, "price": 145000, "book": "RL", "trigger": 0,
                    "discQuantity": 0, "product": "CNC", "validity": "Day", "amo": False}
    assert "c1" not in json.dumps(body) and "client" not in json.dumps(body).lower()
    assert placed.order_id == "1042" and placed.status is OrderStatus.OPEN and placed.quantity == 10


async def test_a_sell_has_a_negative_qty(adapter, fake):
    await adapter.place_order(await approved(adapter, side=Side.SELL, qty=4))
    assert fake.orders[1042]["qtyRemaining"] == -4


async def test_an_order_that_fills_at_once_comes_back_filled_with_its_average_price(adapter, fake):
    fake.auto_fill = True
    placed = await adapter.place_order(await approved(adapter))
    assert placed.status is OrderStatus.FILLED and placed.filled_quantity == 10 and placed.avg_fill_price == 145000


async def test_a_stop_loss_is_posted_in_the_sl_book(adapter, fake):
    p = await approved(adapter, side=Side.SELL, qty=5, order_type=OrderType.STOP_LIMIT, trigger_price=paise(1400), limit_price=paise(1390))
    placed = await adapter.place_order(p)
    row = fake.orders[1042]
    assert (row["book"], row["triggerPrice"], row["price"], row["qtyRemaining"]) == ("SL", 140000, 139000, -5)
    assert placed.order_type is OrderType.STOP_LIMIT and placed.trigger_price == 140000


async def test_an_order_the_exchange_refuses_after_accepting_is_a_rejection_not_a_success(adapter, fake):
    fake.reject_on_place = "Insufficient funds"
    with pytest.raises(BrokerRejected) as exc:
        await adapter.place_order(await approved(adapter))
    assert exc.value.reason is RejectionReason.INSUFFICIENT_FUNDS and exc.value.order.status is OrderStatus.REJECTED
    assert exc.value.order.quantity == 10  # the size came from what we sent, since the row shows none


async def test_the_broker_saying_no_clearly_is_a_rejection_and_nothing_was_retried(adapter, fake):
    fake.fail_next.append({"match": "POST /orders", "status": 500, "error": "Order rejected by risk checks: insufficient margin"})
    with pytest.raises(BrokerRejected) as exc:
        await adapter.place_order(await approved(adapter))
    assert exc.value.reason is RejectionReason.INSUFFICIENT_FUNDS
    assert len([r for r in fake.requests if r[0] == "POST" and r[1] == "/orders"]) == 1 and not fake.orders


async def test_a_success_false_reply_is_a_rejection(adapter, fake):
    fake.fail_next.append({"match": "POST /orders", "status": 400, "error": "Unknown product"})
    with pytest.raises(BrokerRejected):
        await adapter.place_order(await approved(adapter))


@pytest.mark.parametrize("hook", [
    {"status": 500, "error": "Internal Server Error"},
    {"status": 503, "error": "Service temporarily unavailable"},
    {"status": 500, "error": "", "raise": True},
])
async def test_anything_unclear_is_a_timeout_and_the_order_is_never_re_sent(adapter, fake, hook):
    fake.fail_next.append({"match": "POST /orders", "apply": True, **hook})  # the order DID go in; the reply was lost
    with pytest.raises(BrokerTimeout):
        await adapter.place_order(await approved(adapter))
    assert len(fake.orders) == 1  # exactly one, and the adapter did not try again
    assert len([r for r in fake.requests if r[0] == "POST" and r[1] == "/orders"]) == 1


async def test_an_order_lost_in_transit_leaves_nothing_to_find(adapter, fake):
    fake.fail_next.append({"match": "POST /orders", "status": 503, "error": "down"})  # never applied
    p = await approved(adapter)
    with pytest.raises(BrokerTimeout):
        await adapter.place_order(p)
    assert (await adapter.find_sent_order(SentOrderSpec.from_pending(p, NOW))).kind is MatchKind.NONE


async def test_an_unlisted_instrument_is_refused_without_calling_021(adapter, fake):
    p = await approved(adapter)
    ghost = p.model_copy(update={"instrument": p.instrument.model_copy(update={"symbol": "NOSUCH"})})
    with pytest.raises(BrokerRejected):
        await adapter.place_order(ghost)
    assert not [r for r in fake.requests if r[0] == "POST" and r[1] == "/orders"]


async def test_writes_need_an_approved_order(adapter):
    p = await approved(adapter)
    from app.broker.base import OrderNotApproved

    with pytest.raises(OrderNotApproved):
        await adapter.place_order(p.model_copy(update={"state": PendingState.PENDING}))


# ============================================================================================ #
# Finding an order we cannot look up by id
# ============================================================================================ #


async def test_an_order_whose_reply_was_lost_is_found_by_what_it_looks_like(adapter, fake):
    p = await approved(adapter)
    fake.fail_next.append({"match": "POST /orders", "apply": True, "status": 503, "error": "timeout"})
    with pytest.raises(BrokerTimeout):
        await adapter.place_order(p)
    found = await adapter.find_sent_order(SentOrderSpec.from_pending(p, NOW))
    assert found.kind is MatchKind.FOUND and found.order.order_id == "1042" and found.order.status is OrderStatus.OPEN


async def test_look_alikes_are_ambiguous_and_claimed_orders_are_excluded(adapter, fake):
    p = await approved(adapter)
    await adapter.place_order(p)
    await adapter.place_order(p)  # a twin
    spec = SentOrderSpec.from_pending(p, NOW)
    assert (await adapter.find_sent_order(spec)).kind is MatchKind.AMBIGUOUS
    one = await adapter.find_sent_order(spec, exclude_order_ids={"1042"})
    assert one.kind is MatchKind.FOUND and one.order.order_id == "1043"


# ============================================================================================ #
# The order book
# ============================================================================================ #


async def test_the_order_book_maps_open_partial_filled_sell_and_foreign_orders(adapter, fake):
    await adapter.place_order(await approved(adapter))  # 1042 open buy
    await adapter.place_order(await approved(adapter, qty=10))  # 1043 -> partly filled
    fake.fill(1043, 6, price=144800)
    await adapter.place_order(await approved(adapter, side=Side.SELL, qty=3))  # 1044 -> filled sell
    fake.fill(1044, price=146000)
    fake.orders[999] = {**fake.orders[1042], "orderId": 999, "token": 424242, "exchange": "NSEFO"}  # not ours to show

    by_id = {o.order_id: o for o in await adapter.get_orders()}
    assert set(by_id) == {"1042", "1043", "1044"}  # newest first, and the F&O order is left out
    assert by_id["1042"].status is OrderStatus.OPEN
    partial = by_id["1043"]
    assert (partial.status, partial.quantity, partial.filled_quantity, partial.pending_quantity, partial.avg_fill_price) == (
        OrderStatus.PARTIAL, 10, 6, 4, 144800)
    sold = by_id["1044"]
    assert sold.side is Side.SELL and sold.status is OrderStatus.FILLED and sold.avg_fill_price == 146000


async def test_fills_are_fetched_once_per_change_not_on_every_read(adapter, fake):
    await adapter.place_order(await approved(adapter))
    fake.fill(1042)
    await adapter.get_orders()
    await adapter.get_orders()
    assert len([r for r in fake.requests if r[1] == "/orders/1042/trades"]) == 1


async def test_get_order_returns_none_for_an_unknown_id(adapter):
    assert await adapter.get_order("77777") is None


async def test_reads_are_retried_but_not_forever(adapter, fake):
    fake.fail_next.append({"match": "GET /orders", "status": 503, "error": "busy"})
    assert await adapter.get_orders() == []  # second try works
    for _ in range(3):
        fake.fail_next.append({"match": "GET /orders", "status": 503, "error": "busy"})
    with pytest.raises(BrokerTimeout):
        await adapter.get_orders()


# ============================================================================================ #
# Modify and cancel
# ============================================================================================ #


async def modify_card(adapter, order_id, **over):
    inst = await adapter.get_instrument("NSE:INFY")
    fields = dict(id="m1", action=OrderAction.MODIFY, instrument=inst, target_order_id=order_id, client_order_id="m1",
                  ref_ltp=paise(1448), created_at=NOW, expires_at=NOW + timedelta(seconds=60))
    fields.update(over)
    return PendingOrder(**fields).transition(PendingState.APPROVED)


async def test_modify_sends_the_full_new_state_not_just_the_change(adapter, fake):
    placed = await adapter.place_order(await approved(adapter))
    changed = await adapter.modify_order(await modify_card(adapter, placed.order_id, limit_price=paise(1400)))
    [(_, _, body)] = [r for r in fake.requests if r[0] == "PUT"]
    assert body == {"exchange": "NSE", "token": 1594, "qty": 10, "price": 140000, "book": "RL", "trigger": 0,
                    "discQuantity": 0, "product": "CNC", "validity": "Day", "amo": False}
    assert changed.limit_price == 140000


async def test_moving_a_stop_loss_changes_its_trigger_and_limit(adapter, fake):
    p = await approved(adapter, side=Side.SELL, qty=5, order_type=OrderType.STOP_LIMIT, trigger_price=paise(1400), limit_price=paise(1390))
    placed = await adapter.place_order(p)
    changed = await adapter.modify_order(await modify_card(adapter, placed.order_id, trigger_price=paise(1420), limit_price=paise(1410)))
    [(_, _, body)] = [r for r in fake.requests if r[0] == "PUT"]
    assert (body["book"], body["trigger"], body["price"], body["qty"]) == ("SL", 142000, 141000, -5)
    assert changed.trigger_price == 142000


async def test_modifying_a_finished_order_is_refused_before_calling_021(adapter, fake):
    fake.auto_fill = True
    placed = await adapter.place_order(await approved(adapter))
    with pytest.raises(BrokerRejected):
        await adapter.modify_order(await modify_card(adapter, placed.order_id, limit_price=paise(1400)))
    assert not [r for r in fake.requests if r[0] == "PUT"]


async def test_cancel_sends_exchange_token_and_product_in_the_body(adapter, fake):
    fake.auto_settle_cancel = True
    placed = await adapter.place_order(await approved(adapter))
    inst = await adapter.get_instrument("NSE:INFY")
    cancel = PendingOrder(id="x", action=OrderAction.CANCEL, instrument=inst, target_order_id=placed.order_id, client_order_id="x",
                          ref_ltp=paise(1448), created_at=NOW, expires_at=NOW + timedelta(seconds=60)).transition(PendingState.APPROVED)
    result = await adapter.cancel_order(cancel)
    [(_, path, body)] = [r for r in fake.requests if r[0] == "DELETE"]
    assert path == "/orders/1042" and body == {"exchange": "NSE", "token": 1594, "product": "CNC"}
    assert result.status is OrderStatus.CANCELLED


async def test_a_cancel_that_finishes_a_moment_later_is_picked_up(adapter, fake):
    fake.cancel_settles_after = 3  # 021 shows SentForCancellation for a couple of reads, then Cancelled
    placed = await adapter.place_order(await approved(adapter))
    inst = await adapter.get_instrument("NSE:INFY")
    cancel = PendingOrder(id="x", action=OrderAction.CANCEL, instrument=inst, target_order_id=placed.order_id, client_order_id="x",
                          ref_ltp=paise(1448), created_at=NOW, expires_at=NOW + timedelta(seconds=60)).transition(PendingState.APPROVED)
    assert (await adapter.cancel_order(cancel)).status is OrderStatus.CANCELLED


async def test_a_cancel_still_in_flight_is_reported_as_still_open_not_cancelled(adapter, fake):
    placed = await adapter.place_order(await approved(adapter))
    inst = await adapter.get_instrument("NSE:INFY")
    cancel = PendingOrder(id="x", action=OrderAction.CANCEL, instrument=inst, target_order_id=placed.order_id, client_order_id="x",
                          ref_ltp=paise(1448), created_at=NOW, expires_at=NOW + timedelta(seconds=60)).transition(PendingState.APPROVED)
    assert (await adapter.cancel_order(cancel)).status is OrderStatus.OPEN  # SentForCancellation: not yet true


# ============================================================================================ #
# Account and prices
# ============================================================================================ #


async def test_a_price_is_read_from_the_feed_and_missing_prices_time_out_honestly(adapter, conn):
    await adapter._feed.wait_connected(1)
    conn.market[0].push(full_nse_cash(1594, 145000, 144000, 144500, 146000, 143500, bid=144995, ask=145005))
    q = await adapter.get_quote("NSE:INFY")
    assert (q.ltp, q.prev_close, q.bid, q.ask, q.day_high, q.day_low) == (145000, 144000, 144995, 145005, 146000, 143500)
    with pytest.raises(BrokerTimeout):
        await adapter.get_quote("NSE:TCS")  # nothing has arrived for it


async def test_holdings_use_live_prices_and_fall_back_to_the_previous_close_when_there_is_none(adapter, fake, conn):
    fake.holdings = [
        {"isin": "INE009A01021", "symbol": "INFY", "freeQty": 15, "btstQty": 5, "sellQty": 0, "price": 138000, "prevClose": 144000},
        {"isin": "INE467B01029", "symbol": "TCS", "freeQty": 5, "btstQty": 0, "sellQty": 0, "price": 390000, "prevClose": 389000},
        {"isin": "INE002A01018", "symbol": "RELIANCE", "freeQty": 0, "btstQty": 0, "sellQty": 7, "price": 290000, "prevClose": 290000},
    ]
    await adapter._feed.wait_connected(1)
    conn.market[0].push(ltp_packet(1, 1594, 145000))
    await settle()
    by = {h.instrument.symbol: h for h in await adapter.get_holdings()}
    assert set(by) == {"INFY", "TCS"}  # nothing held in RELIANCE any more
    assert by["INFY"].quantity == 20 and by["INFY"].avg_price == 138000 and by["INFY"].ltp == 145000
    assert by["TCS"].ltp == 389000  # no price arrived: last close, not an invented number
    assert by["INFY"].prev_close == 144000 and by["INFY"].day_pnl == (145000 - 144000) * 20


async def test_positions_skip_closed_and_carry_forward_rows(adapter, fake, conn):
    fake.positions = [
        {"exchange": "NSECM", "token": 2885, "product": "INTRADAY", "netQuantity": 10, "netPrice": 140000, "prevClose": 139200},
        {"exchange": "NSECM", "token": 1594, "product": "INTRADAY", "netQuantity": 0, "netPrice": 0, "prevClose": 144000},
        {"exchange": "NSEFO", "token": 70001, "product": "NRML", "netQuantity": 75, "netPrice": 100, "prevClose": 90},
    ]
    await adapter._feed.wait_connected(1)
    conn.market[0].push(ltp_packet(1, 2885, 141000))
    await settle()
    pos, opt = sorted(await adapter.get_positions(), key=lambda p: p.instrument.is_option)  # the closed row is skipped
    assert (pos.instrument.symbol, pos.quantity, pos.avg_price, pos.ltp, pos.product) == ("RELIANCE", 10, 140000, 141000, Product.MIS)
    assert pos.pnl == 10 * 1000
    # an option carried overnight is read too (it used to be dropped); no live premium yet, so the previous close
    assert (opt.instrument.symbol, opt.quantity, opt.product, opt.ltp) == ("NIFTY26101324400CE", 75, Product.NRML, 90)


async def test_funds_are_an_estimate_from_the_starting_balance_trades_and_open_buys(adapter, fake):
    assert (await adapter.get_funds()).available_cash == paise(1_000_000)
    fake.auto_fill = True
    await adapter.place_order(await approved(adapter, qty=10))  # buys 10 @ 1450
    fake.auto_fill = False
    await adapter.place_order(await approved(adapter, qty=2, limit="1400"))  # 2 x 1400 tied up in an open buy
    funds = await adapter.get_funds()
    assert funds.used_margin == paise(2800)
    assert funds.available_cash == paise(1_000_000) - paise(14500) - paise(2800)


async def test_ticks_carry_a_rising_sequence_number_per_instrument(adapter, conn):
    await adapter._feed.wait_connected(1)
    got = []

    async def listen():
        async for t in adapter.subscribe_ticks(["NSE:INFY"]):
            got.append(t)
            if len(got) == 2:
                return

    task = asyncio.create_task(listen())
    await settle()
    assert any('"Instruments":[[1,1594]]' in m for m in conn.market[0].sent)  # watching INFY was asked for
    feed_prices(conn, {1594: 145000})
    feed_prices(conn, {1594: 145010})
    await asyncio.wait_for(task, 1)
    assert [(t.instrument_key, t.ltp, t.seq) for t in got] == [("NSE:INFY", 145000, 1), ("NSE:INFY", 145010, 2)]


async def test_locks_are_simulated_and_default_to_none(adapter):
    locks = await adapter.get_account_locks()
    assert not locks.anchor_active and not locks.co_captain_locked


# ============================================================================================ #
# Search and options
# ============================================================================================ #


async def test_search_and_instrument_lookup(adapter):
    assert [i.symbol for i in await adapter.search_instruments("infosys")] == ["INFY"]
    assert (await adapter.get_instrument("NSE:TCS")).tick_size == 5
    assert await adapter.get_instrument("NSE:NOPE") is None


async def test_option_expiries_come_from_the_instrument_file(adapter):
    assert await adapter.get_option_expiries("NIFTY") == [date(2026, 10, 13), date(2026, 10, 20)]


async def test_an_option_chain_is_built_around_the_money_from_a_separate_feed(adapter, conn):
    await adapter._feed.wait_connected(1)
    conn.market[0].push(full_index(26000, 2452000, 2440000, 2445000, 2460000, 2430000))  # NIFTY 24520
    task = asyncio.create_task(adapter.get_option_chain("NIFTY", date(2026, 10, 13), window=1))
    for _ in range(100):  # wait until the chain feed has connected and subscribed
        if len(conn.market) > 1 and conn.market[1].sent:
            break
        await asyncio.sleep(0.005)
    sub = json.loads(conn.market[1].sent[0])
    assert sub["Mode"] == "oc" and sub["Filters"] == "l"  # prices only; its own connection, so a bad filter can't stall prices
    tokens = [t for _, t in sub["Instruments"]]
    for i, token in enumerate(sorted(tokens)):
        conn.market[1].push(chain_packet(token, 10000 + i))
    chain = await asyncio.wait_for(task, 2)
    assert chain.spot == paise(24520) and chain.expiry == date(2026, 10, 13)
    assert [r.strike for r in chain.rows] == [paise(24450), paise(24500), paise(24550)]  # the money +/- one step
    assert all(r.call and r.put for r in chain.rows)
    assert chain.rows[0].call.oi is None and chain.rows[0].call.volume is None  # 021's OI/volume are junk: never shown


async def test_an_option_chain_with_no_prices_is_a_timeout_not_an_empty_table(adapter, conn):
    await adapter._feed.wait_connected(1)
    conn.market[0].push(full_index(26000, 2452000, 2440000, 2445000, 2460000, 2430000))
    with pytest.raises(BrokerTimeout):
        await adapter.get_option_chain("NIFTY", date(2026, 10, 13), window=1)


async def test_an_unknown_underlying_or_expiry_is_a_plain_error(adapter, conn):
    with pytest.raises(ValueError):
        await adapter.get_option_chain("NOSUCH", date(2026, 10, 13))
    with pytest.raises(ValueError):
        await adapter.get_option_chain("NIFTY", date(2030, 1, 1))
