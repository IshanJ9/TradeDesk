from datetime import date, datetime, timedelta, timezone

import pytest

from app.db import Database
from app.history.sqlite_store import SqliteActivityStore
from app.history.store import DaySummary, InMemoryActivityStore, trading_day
from app.schemas import Exchange, Instrument, Order, OrderStatus, OrderType, Side

NOW = datetime(2026, 10, 9, 5, tzinfo=timezone.utc)


def test_legacy_order_provenance_is_unknown_until_observed_from_broker():
    db = Database()
    # An existing database must not be retroactively labelled real.
    db.execute("CREATE TABLE activity_orders (order_id TEXT PRIMARY KEY, source TEXT NOT NULL, day TEXT NOT NULL, data TEXT NOT NULL)")
    legacy = order()
    db.execute("INSERT INTO activity_orders VALUES (?,?,?,?)", (legacy.order_id, 'external', NOW.date().isoformat(), legacy.model_dump_json(round_trip=True)))
    store = SqliteActivityStore(db, recording_source='zerotwoone')
    assert store.orders_on(NOW.date())[0].recording_source == 'unknown'
    store.record_order(legacy, 'external')
    assert SqliteActivityStore(db).orders_on(NOW.date())[0].recording_source == 'zerotwoone'
    db.close()


def order(oid="1", at=NOW, **updates):
    values = dict(order_id=oid, instrument=Instrument(symbol="INFY", exchange=Exchange.NSE),
                  side=Side.BUY, quantity=10, order_type=OrderType.LIMIT, limit_price=145005,
                  status=OrderStatus.OPEN, created_at=at, updated_at=at)
    return Order(**(values | updates))


def summary(day, **updates):
    return DaySummary(**(dict(day=day, orders=3, turnover=100001, pnl=-123, charges=17) | updates))


@pytest.fixture(params=["memory", "sqlite"])
def store(request):
    db = Database()
    yield InMemoryActivityStore() if request.param == "memory" else SqliteActivityStore(db)
    db.close()


@pytest.mark.parametrize("source,second", [("external", "app"), ("app", "external")])
def test_upsert_preserves_first_source_and_latest_fill(store, source, second):
    store.record_order(order(), source)
    filled = order(status=OrderStatus.PARTIAL, filled_quantity=3, avg_fill_price=144995)
    store.record_order(filled, second)
    record, = store.orders_on(NOW.date())
    assert record.source == source
    assert record.order == filled
    assert record.order.pending_quantity == 7


def test_indian_date_and_chronological_order(store):
    late = datetime(2026, 10, 9, 20, tzinfo=timezone.utc)
    early = datetime(2026, 10, 10, 0, 45, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    store.record_order(order("later", late), "app")
    store.record_order(order("earlier", early), "external")
    assert store.orders_on(date(2026, 10, 9)) == []
    assert [r.order.order_id for r in store.orders_on(date(2026, 10, 10))] == ["earlier", "later"]


def test_upsert_can_correct_trading_day(store):
    store.record_order(order(), "external")
    next_day = NOW + timedelta(days=1)
    store.record_order(order(at=next_day), "app")
    assert store.orders_on(NOW.date()) == []
    assert store.orders_on(next_day.date())[0].source == "external"


@pytest.mark.parametrize("limit,expected", [(3, [4, 3, 2]), (0, []), (-1, [4, 3, 2, 1]), (30, [4, 3, 2, 1, 0])])
def test_days_are_newest_first_with_reference_limits(store, limit, expected):
    for i in range(5):
        store.save_day(summary(date(2026, 10, 1) + timedelta(days=i), orders=i))
    assert [day.orders for day in store.days(limit)] == expected


def test_day_summary_round_trip_and_replace(store):
    day = NOW.date()
    store.save_day(summary(day, demo=True, risk_score=23))
    final = summary(day, demo=False, risk_score=44, pnl=-123456789, turnover=12345678901)
    store.save_day(final)
    assert store.days() == [final]


def test_database_reopen_keeps_orders_sources_and_days(tmp_path):
    path = f"sqlite:///{tmp_path / 'history.db'}"
    db = Database(path)
    store = SqliteActivityStore(db)
    store.record_order(order(), "external")
    store.save_day(summary(NOW.date(), demo=True))
    db.close()
    db = Database(path)
    try:
        store = SqliteActivityStore(db)
        store.record_order(order(filled_quantity=5, avg_fill_price=145000, status=OrderStatus.PARTIAL), "app")
        record, = store.orders_on(trading_day(NOW))
        assert record.source == "external"
        assert record.order.filled_quantity == 5
        assert store.days() == [summary(NOW.date(), demo=True)]
    finally:
        db.close()


def test_two_store_instances_keep_the_original_attribution(tmp_path):
    path = f"sqlite:///{tmp_path / 'shared.db'}"
    db1, db2 = Database(path), Database(path)
    try:
        a, b = SqliteActivityStore(db1), SqliteActivityStore(db2)
        a.record_order(order(), "external")
        b.record_order(order(status=OrderStatus.FILLED, filled_quantity=10, avg_fill_price=145005), "app")
        record, = a.orders_on(NOW.date())
        assert record.source == "external"
        assert record.order.filled_quantity == 10
    finally:
        db1.close()
        db2.close()
