"""The pieces of the 021 adapter on their own: instrument file, REST translation, price packets, the feed."""

import asyncio
from datetime import date, datetime, timedelta, timezone

import pytest

from app.broker.zerotwoone import codec
from app.broker.zerotwoone.feed import MarketFeed
from app.broker.zerotwoone.instruments import InstrumentMaster, expiry_to_date, option_symbol
from app.broker.zerotwoone.packets import ChainPacket, FullPacket, LtpPacket, decode_frame, subscribe_message
from app.schemas import (
    Exchange,
    OptionType,
    Order,
    OrderAction,
    OrderStatus,
    OrderType,
    PendingOrder,
    PendingState,
    Product,
    RejectionReason,
    Side,
    Validity,
    paise,
)
from fake021 import FakeConnector, FakeSocket, HEARTBEAT, chain_packet, csv_text, exchange_seconds, full_index, full_nse_cash, ltp_packet

NOW = datetime(2026, 10, 9, 10, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def master():
    return InstrumentMaster.from_csv(csv_text(), today=date(2026, 10, 9))


# ============================================================================================ #
# The instrument file
# ============================================================================================ #


def test_cash_stocks_are_read_with_their_tick_size_and_price_band(master):
    infy = master.get("NSE:INFY")
    assert infy.token == 1594 and infy.exchange == "NSECM" and infy.ws == (1, 1594)
    assert infy.request_exchange == "NSE"
    assert infy.instrument.tick_size == 5
    assert (infy.instrument.price_band_low, infy.instrument.price_band_high) == (115200, 172800)
    assert infy.instrument.name == "Infosys Ltd"  # 021's file has no names; ours come from a small table
    assert master.get("BSE:INFY").request_exchange == "BSE" and master.get("BSE:INFY").ws == (4, 500209)


def test_the_row_from_the_guide_parses(master):
    bond = master.get("NSE:747HR36")
    assert bond.token == 22699 and bond.board_lot == 100 and bond.freeze_quantity == 984600
    assert bond.instrument.tick_size == 1 and bond.instrument.isin == "IN1620230376"
    assert bond.instrument.name == ""  # unknown names stay blank rather than invented


def test_the_index_is_data_only(master):
    nifty = master.get("NSE:NIFTY")
    assert nifty.exchange == "NSEIDX" and nifty.ws == (3, 26000) and nifty.instrument.series == "INDEX"
    assert nifty.instrument.price_band_low is None  # no circuits on an index


def test_lookup_by_token_isin_and_symbol(master):
    assert master.by_token("NSECM", 11536).instrument.symbol == "TCS"
    assert master.by_isin("INE002A01018").instrument.symbol == "RELIANCE"
    assert master.by_symbol("hdfcbank").instrument.symbol == "HDFCBANK"
    assert master.by_token("NSECM", 999) is None


def test_search_finds_by_symbol_and_by_company_name(master):
    assert [i.symbol for i in master.search("infosys")] == ["INFY"]
    assert master.search("hdfc bank")[0].symbol == "HDFCBANK"
    assert master.search("INFY")[0].exchange is Exchange.NSE  # NSE listing first
    tatas = {i.symbol for i in master.search("tata")}
    assert {"TATAMOTORS", "TATASTEEL"} <= tatas  # ambiguous: the builder will ask which one
    assert master.search("") == [] and master.search("zzzz") == []


def test_search_for_an_index_name_returns_the_index(master):
    assert master.search("nifty")[0].series == "INDEX"


def test_options_are_built_per_underlying_with_real_expiries(master):
    assert master.option_expiries("NIFTY") == [date(2026, 10, 13), date(2026, 10, 20)]
    assert master.option_expiries("NIFTY", today=date(2026, 10, 14)) == [date(2026, 10, 20)]
    contracts = master.option_listings("NIFTY", date(2026, 10, 13))
    assert len(contracts) == 10  # five strikes, a call and a put each
    call = next(c for c in contracts if c.instrument.strike == paise(24500) and c.instrument.option_type is OptionType.CE)
    assert call.exchange == "NSEFO" and call.ws[0] == 2 and call.instrument.lot_size == 75
    assert call.instrument.symbol == "NIFTY26101324500CE"
    assert master.option_expiries("BANKNIFTY") == []


def test_expiry_epochs_are_read_whichever_way_021_means_them():
    today = date(2026, 10, 9)
    assert expiry_to_date(exchange_seconds(2026, 10, 13), today) == date(2026, 10, 13)  # seconds since 1980
    unix = int(datetime(2026, 10, 13, 10, tzinfo=timezone.utc).timestamp())
    assert expiry_to_date(unix, today) == date(2026, 10, 13)  # or plain Unix seconds
    assert expiry_to_date(0, today) is None  # cash instruments
    assert expiry_to_date(exchange_seconds(2020, 1, 1), today) is None  # long expired: not a live contract


def test_option_symbols_keep_half_rupee_strikes_apart():
    assert option_symbol("NIFTY", date(2026, 10, 13), 2450000, OptionType.PE) == "NIFTY26101324500PE"
    assert option_symbol("X", date(2026, 10, 13), 125050, OptionType.CE) == "X2610131250.50CE"


def test_junk_rows_are_skipped_not_fatal():
    text = csv_text() + "abc,NSECM,1,NSECM,0,1,1,1,1,0,,0,,STK,,5\n9,NSECM,9,NSECM,0,1,1,1,1,0,,0,BAD SYMBOL!,STK,,5\n"
    assert InstrumentMaster.from_csv(text, today=date(2026, 10, 9)).get("NSE:INFY") is not None


# ============================================================================================ #
# REST translation
# ============================================================================================ #


def pending(master, symbol="INFY", **over):
    inst = master.get(f"NSE:{symbol}").instrument
    fields = dict(
        id="p1", action=OrderAction.PLACE, instrument=inst, side=Side.BUY, quantity=10, order_type=OrderType.LIMIT,
        limit_price=paise(1450), product=Product.CNC, validity=Validity.DAY, client_order_id="c1",
        ref_ltp=paise(1448), created_at=NOW, expires_at=NOW + timedelta(seconds=60),
    )
    fields.update(over)
    return PendingOrder(**fields).transition(PendingState.APPROVED)


def test_a_buy_limit_order_body_follows_the_guide(master):
    body = codec.place_body(pending(master), master.get("NSE:INFY"))
    assert body == {
        "exchange": "NSE", "token": 1594, "qty": 10, "price": 145000, "book": "RL", "trigger": 0,
        "discQuantity": 0, "product": "CNC", "validity": "Day", "amo": False,
    }


def test_the_sign_of_qty_is_the_side(master):
    body = codec.place_body(pending(master, side=Side.SELL), master.get("NSE:INFY"))
    assert body["qty"] == -10


def test_a_market_order_is_sent_as_its_protection_limit_never_as_price_zero(master):
    p = pending(master, order_type=OrderType.MARKET, limit_price=None, protection_price=paise(1465))
    body = codec.place_body(p, master.get("NSE:INFY"))
    assert body["price"] == 146500 and body["book"] == "RL"


def test_a_stop_order_uses_the_sl_book_with_a_trigger(master):
    p = pending(master, side=Side.SELL, order_type=OrderType.STOP_LIMIT, trigger_price=paise(1400), limit_price=paise(1390))
    body = codec.place_body(p, master.get("NSE:INFY"))
    assert body["book"] == "SL" and body["trigger"] == 140000 and body["price"] == 139000 and body["qty"] == -10


def test_intraday_and_ioc_use_021s_words(master):
    body = codec.place_body(pending(master, product=Product.MIS, validity=Validity.IOC), master.get("NSE:INFY"))
    assert (body["product"], body["validity"]) == ("INTRADAY", "IOC")


def row(**over):
    base = {
        "orderId": 1042, "time": 1791451200, "lastActivity": 1791451201, "token": 1594, "exchange": "NSECM",
        "product": "CNC", "qtyRemaining": 10, "qtyTraded": 0, "discQty": 0, "price": 145000, "triggerPrice": 0,
        "book": "RL", "validity": "Day", "status": "Pending", "reason": "", "placedBy": "User", "amo": False,
    }
    base.update(over)
    return base


def test_an_open_order_row(master):
    o = codec.order_from_row(row(), master.get("NSE:INFY"))
    assert (o.order_id, o.side, o.quantity, o.status) == ("1042", Side.BUY, 10, OrderStatus.OPEN)
    assert o.order_type is OrderType.LIMIT and o.limit_price == 145000 and o.trigger_price is None


def test_021_has_no_partial_status_so_it_is_worked_out(master):
    o = codec.order_from_row(row(qtyRemaining=4, qtyTraded=6), master.get("NSE:INFY"), trades=[{"quantity": 6, "price": 144800}])
    assert o.status is OrderStatus.PARTIAL and o.quantity == 10 and o.filled_quantity == 6 and o.pending_quantity == 4
    assert o.avg_fill_price == 144800


def test_a_filled_sell_is_a_sell_even_though_nothing_is_remaining(master):
    trades = [{"quantity": -3, "price": 100}, {"quantity": -7, "price": 110}]
    o = codec.order_from_row(row(qtyRemaining=0, qtyTraded=10, status="Executed"), master.get("NSE:INFY"), trades=trades)
    assert o.side is Side.SELL and o.status is OrderStatus.FILLED and o.avg_fill_price == 107  # 1070 / 10


def test_a_filled_row_whose_side_cannot_be_told_is_left_out_not_guessed(master):
    assert codec.order_from_row(row(qtyRemaining=0, qtyTraded=10, status="Executed"), master.get("NSE:INFY")) is None


def test_a_rejected_order_with_no_sizes_uses_what_we_sent(master):
    zero = row(qtyRemaining=0, qtyTraded=0, status="Rejected", reason="Insufficient funds")
    assert codec.order_from_row(zero, master.get("NSE:INFY")) is None
    o = codec.order_from_row(zero, master.get("NSE:INFY"), fallback=(Side.BUY, 10))
    assert o.status is OrderStatus.REJECTED and o.quantity == 10 and o.rejection_reason is RejectionReason.INSUFFICIENT_FUNDS
    assert o.rejection_message == "Insufficient funds"


def test_a_stop_order_row(master):
    o = codec.order_from_row(row(qtyRemaining=-5, book="SL", price=139000, triggerPrice=140000), master.get("NSE:INFY"))
    assert o.order_type is OrderType.STOP_LIMIT and o.trigger_price == 140000 and o.limit_price == 139000 and o.side is Side.SELL


@pytest.mark.parametrize(
    "wire, expected",
    [("Received", OrderStatus.PENDING), ("Frozen", OrderStatus.PENDING), ("Placed", OrderStatus.OPEN),
     ("SentForModification", OrderStatus.OPEN), ("SentForCancellation", OrderStatus.OPEN),
     ("Cancelled", OrderStatus.CANCELLED), ("Rejected", OrderStatus.REJECTED), ("SomethingNew", OrderStatus.UNKNOWN)],
)
def test_every_021_status_maps_and_an_unknown_one_is_never_success(wire, expected):
    assert codec.status_of(row(status=wire)) is expected


def test_average_price_is_volume_weighted_and_rounded_half_up():
    assert codec.average_price([{"quantity": 1, "price": 100}, {"quantity": 2, "price": 101}]) == 101  # 100.67
    assert codec.average_price([{"quantity": 2, "price": 100}, {"quantity": 2, "price": 101}]) == 101  # 100.5 -> up
    assert codec.average_price([]) is None


def test_the_envelope_is_unwrapped_and_a_failure_is_never_success():
    assert codec.unwrap({"data": {"a": 1}, "success": True, "error": None}) == {"a": 1}
    assert codec.unwrap([1, 2]) == [1, 2]
    with pytest.raises(codec.MalformedResponse):
        codec.unwrap({"data": None, "success": False, "error": "nope"})
    with pytest.raises(codec.MalformedResponse):
        codec.unwrap("surprise")


# ============================================================================================ #
# Price packets
# ============================================================================================ #


def test_ltp_packets_back_to_back_and_a_heartbeat():
    frame = ltp_packet(1, 2885, 140000) + HEARTBEAT + ltp_packet(2, 70001, 5025)
    packets, leftover = decode_frame(frame)
    assert packets == [LtpPacket(1, 2885, 140000), LtpPacket(2, 70001, 5025)] and leftover == 0


def test_snapshot_ltp_packets_may_carry_open_or_previous_close():
    frame = ltp_packet(1, 2885, 140000) + b"o" + (139000).to_bytes(4, "big") + ltp_packet(1, 1594, 145000) + b"c" + (144000).to_bytes(4, "big")
    packets, leftover = decode_frame(frame)
    assert packets[0].open == 139000 and packets[0].prev_close is None
    assert packets[1].prev_close == 144000 and leftover == 0


def test_a_full_nse_cash_packet():
    [p], leftover = decode_frame(full_nse_cash(1594, 145000, 144000, 144500, 146000, 143500, bid=144995, ask=145005))
    assert p == FullPacket(1, 1594, 145000, 144000, 144500, 146000, 143500, 144995, 145005) and leftover == 0


def test_an_empty_book_side_is_none_not_zero():
    [p], _ = decode_frame(full_nse_cash(1594, 145000, 144000, 144500, 146000, 143500))
    assert p.bid is None and p.ask is None


def test_a_full_index_packet():
    [p], _ = decode_frame(full_index(26000, 2450000, 2440000, 2445000, 2460000, 2430000))
    assert (p.exchange, p.token, p.ltp, p.prev_close) == (3, 26000, 2450000, 2440000) and p.bid is None


def test_an_option_chain_packet():
    [p], _ = decode_frame(chain_packet(70003, 12550, 150000, 900))
    assert p == ChainPacket(2, 70003, 12550, 150000, 900)


def test_a_cut_off_or_unknown_packet_ends_the_frame_without_raising():
    good = ltp_packet(1, 2885, 140000)
    packets, leftover = decode_frame(good + ltp_packet(1, 1594, 145000)[:7])
    assert len(packets) == 1 and leftover == 7
    packets, leftover = decode_frame(good + b"\x00\x63garbage")
    assert len(packets) == 1 and leftover > 0
    assert decode_frame(b"") == ([], 0)


def test_subscribe_messages_match_the_guide():
    assert subscribe_message("subscribe", "ltp", [(1, 2885), (2, 43210)]) == '{"Task":"subscribe","Mode":"ltp","Instruments":[[1,2885],[2,43210]]}'
    assert '"Filters":"l,o,v"' in subscribe_message("subscribe", "oc", [(2, 70001)], "l,o,v")


# ============================================================================================ #
# The feed
# ============================================================================================ #


def make_feed(master, connector=None, **kw):
    connector = connector or FakeConnector()

    keys = iter(range(1000))

    async def key():
        return f"key{next(keys)}"

    def resolve(exchange, token):
        name = {1: "NSECM", 3: "NSEIDX", 4: "BSEEQ"}.get(exchange)
        item = master.by_token(name, token) if name else None
        return item.instrument.key if item else None

    feed = MarketFeed(url="wss://x/market", connect=connector, get_key=key, resolve=resolve, clock=lambda: NOW,
                      backoff=(0.01,), **kw)
    return feed, connector


async def test_a_frame_updates_the_snapshot_and_publishes_numbered_ticks(master):
    feed, _ = make_feed(master)
    got = []

    async def listen():
        async for t in feed.ticks():
            got.append(t)
            if len(got) == 3:
                return

    task = asyncio.create_task(listen())
    await asyncio.sleep(0)
    feed.handle_frame(ltp_packet(1, 1594, 145000))
    feed.handle_frame(ltp_packet(1, 1594, 145005) + ltp_packet(1, 11536, 390000))
    await asyncio.wait_for(task, 1)
    assert [(t.instrument_key, t.ltp, t.seq) for t in got] == [("NSE:INFY", 145000, 1), ("NSE:INFY", 145005, 2), ("NSE:TCS", 390000, 1)]
    assert feed.snapshot("NSE:INFY").ltp == 145005


async def test_unknown_tokens_are_dropped_quietly(master):
    feed, _ = make_feed(master)
    feed.handle_frame(ltp_packet(1, 424242, 100))
    assert feed.snapshot("NSE:INFY") is None


async def test_a_full_packet_fills_the_quote_fields_and_a_later_ltp_keeps_them(master):
    feed, _ = make_feed(master)
    feed.handle_frame(full_nse_cash(1594, 145000, 144000, 144500, 146000, 143500, bid=144995, ask=145005))
    feed.handle_frame(ltp_packet(1, 1594, 145100))
    snap = feed.snapshot("NSE:INFY")
    assert (snap.ltp, snap.prev_close, snap.high, snap.low, snap.bid, snap.ask) == (145100, 144000, 146000, 143500, 144995, 145005)


async def test_wait_for_returns_when_the_first_price_arrives_or_gives_up(master):
    feed, _ = make_feed(master)
    waiter = asyncio.create_task(feed.wait_for("NSE:INFY", 1))
    await asyncio.sleep(0.01)
    feed.handle_frame(ltp_packet(1, 1594, 145000))
    assert (await waiter).ltp == 145000
    assert await feed.wait_for("NSE:TCS", 0.02) is None


async def test_it_subscribes_on_connect_and_again_after_a_reconnect_with_a_new_key(master):
    feed, connector = make_feed(master)
    await feed.watch([(1, 1594), (1, 11536)], pin=True)
    feed.start()
    assert await feed.wait_connected(1)
    first = connector.sockets[0]
    assert first.sent == ['{"Task":"subscribe","Mode":"full","Instruments":[[1,1594],[1,11536]]}']
    assert connector.urls[0].endswith("?token=key0")
    first.close()  # 021's daily 08:00 restart
    for _ in range(100):
        if len(connector.sockets) > 1 and connector.sockets[1].sent:
            break
        await asyncio.sleep(0.01)
    assert connector.urls[1].endswith("?token=key1")  # a fresh key every time
    assert connector.sockets[1].sent == first.sent  # nothing is remembered across connections: all resent
    await feed.stop()


async def test_watching_more_while_connected_sends_only_the_new_ones(master):
    feed, connector = make_feed(master)
    feed.start()
    await feed.wait_connected(1)
    await feed.watch([(1, 1594)])
    await feed.watch([(1, 1594), (1, 2885)])
    assert connector.sockets[0].sent[-2:] == [
        '{"Task":"subscribe","Mode":"full","Instruments":[[1,1594]]}',
        '{"Task":"subscribe","Mode":"full","Instruments":[[1,2885]]}',
    ]
    await feed.stop()


async def test_past_the_connection_limit_the_oldest_unpinned_is_dropped_and_pinned_stay(master):
    feed, connector = make_feed(master, max_instruments=3)
    feed.start()
    await feed.wait_connected(1)
    await feed.watch([(1, 1)], pin=True)
    await feed.watch([(1, 2)])
    await feed.watch([(1, 3)])
    await feed.watch([(1, 4)])  # over the limit: (1, 2) goes, (1, 1) is pinned
    assert (1, 2) not in feed._wanted and (1, 1) in feed._wanted and len(feed._wanted) == 3
    assert any('"Task":"unsubscribe"' in m and "[[1,2]]" in m for m in connector.sockets[0].sent)
    await feed.stop()


async def test_a_connection_failure_is_retried_not_fatal(master):
    attempts = []

    def flaky(url):
        attempts.append(url)
        if len(attempts) < 3:
            raise OSError("refused")
        return FakeConnector()(url)

    feed, _ = make_feed(master, connector=flaky)
    feed.start()
    assert await feed.wait_connected(2)
    assert len(attempts) == 3
    await feed.stop()
