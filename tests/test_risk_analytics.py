"""Isolated fixtures only; these tests never create account or product history."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.db import Database
from app.config import Settings
from app.main import create_app
from app.history.store import DaySummary, InMemoryActivityStore, trading_day
from app.risk.analytics import build_analytics, pace
from app.risk.models import DisciplineDay
from app.risk.presets import preset
from app.risk.report_store import ReportStore
from app.risk.today import calculate_today
from app.schemas import Funds, Order, Instrument

NOW = datetime(2026,10,9,6,30,tzinfo=timezone.utc)  # noon IST
PROFILE = preset('balanced')
FACTS = calculate_today(orders=[],holdings=[],positions=[],funds=Funds(available_cash=100_000),profile=PROFILE,now=NOW)


def day(offset=1, **kw):
    values = dict(day=trading_day(NOW)-timedelta(days=offset), orders=1,turnover=1000,pnl=1000,charges=100,
                  pnl_after_charges=900,risk_score=25,components=[],demo=False,recording_source='zerotwoone')
    values.update(kw)
    return DisciplineDay(**values)


@pytest.fixture
def reports():
    db=Database()
    yield ReportStore(db)
    db.close()


def test_five_minute_sampling_ignores_refresh_frequency_and_returns_need_baseline(reports):
    first = reports.observe(FACTS.model_copy(update={'pnl_after_charges':100}),10,NOW)
    assert first['observed_return_pct'] is None
    same = reports.observe(FACTS.model_copy(update={'pnl_after_charges':500}),90,NOW+timedelta(seconds=10))
    assert same['risk_samples']==1 and same['average_risk_score']==10
    last = reports.observe(FACTS.model_copy(update={'pnl_after_charges':1100}),30,NOW+timedelta(minutes=5))
    assert last['risk_samples']==2 and last['average_risk_score']==20
    assert last['observed_return_pct']==1
    assert last['opening_portfolio_paise']==100000 and last['opening_pnl_paise']==100


def test_observations_persist_and_zero_baseline_never_divides(tmp_path):
    url='sqlite:///'+str(tmp_path/'observations.db')
    db=Database(url)
    ReportStore(db).observe(FACTS.model_copy(update={'portfolio_value':0}),None,NOW)
    db.close()
    db=Database(url)
    try:
        result=ReportStore(db).observe(FACTS,50,NOW+timedelta(minutes=5))
        assert result['observed_return_pct'] is None
        assert result['risk_samples']==1 and result['average_risk_score']==50
        assert result['first_observed_at']==NOW
    finally:
        db.close()


def test_averages_exclude_synthetic_today_and_missing_risk_without_inventing_zero(reports):
    days=[day(1,average_risk_score=10,observed_return_pct=1),
          day(2,pnl_after_charges=-300,average_risk_score=50,observed_return_pct=-2),
          day(3,pnl_after_charges=0),day(0,pnl_after_charges=999999),day(4,demo=True,pnl_after_charges=999999)]
    report=build_analytics(days,InMemoryActivityStore(),reports,NOW)
    assert report.summary.days==3
    assert report.summary.average_net_pnl_paise==200
    assert report.summary.average_risk==30 and report.summary.risk_days==2
    assert report.summary.average_observed_return_pct==-.5 and report.summary.return_days==2
    assert report.summary.profitable_days==1
    assert sum(g.days for g in report.weekdays)==3
    assert sum(g.days for g in report.risk_bands)==2
    assert report.today.day==NOW.date()
    assert all(not d.demo and d.day<NOW.date() for d in report.days)


def test_no_synthetic_fallback_and_window_capped_at_thirty(reports):
    assert build_analytics([day(demo=True)],InMemoryActivityStore(),reports,NOW).summary.days==0
    report=build_analytics([day(i) for i in range(1,40)],InMemoryActivityStore(),reports,NOW)
    assert len(report.days)==30
    assert report.days[0].day==NOW.date()-timedelta(days=30)
    assert report.pace.usual_orders is None


def order(oid,at):
    return Order(order_id=oid,instrument=Instrument(symbol='ITC',exchange='NSE'),side='BUY',quantity=1,
                 filled_quantity=1,avg_fill_price=10000,order_type='LIMIT',limit_price=10000,status='FILLED',
                 created_at=at,updated_at=at)


def test_pace_requires_covered_windows_and_patterns_include_external_orders(reports):
    history=InMemoryActivityStore(recording_source='zerotwoone')
    past=NOW-timedelta(days=1)
    history.save_day(DaySummary(day=past.date(),orders=1,turnover=10000,pnl=100,charges=10))
    history.record_order(order('past',past-timedelta(minutes=10)),'external')
    history.record_order(order('current',NOW-timedelta(minutes=1)),'app')
    assert pace(history,reports,NOW).baseline_days==0
    for minutes in (20,15,10,5,0):
        reports.observe(FACTS.model_copy(update={'day':past.date()}),10,past-timedelta(minutes=minutes),source='zerotwoone')
    p=pace(history,reports,NOW)
    assert p.orders_now==1 and p.usual_orders==1 and p.baseline_days==1
    a=build_analytics([day()],history,reports,NOW)
    assert a.symbols[0].label=='NSE:ITC' and a.symbols[0].external_orders==1
    assert a.hours[0].label=='11:00 IST'
    assert a.symbols[0].filled_turnover_paise==10000


def test_legacy_day_snapshots_do_not_manufacture_average_risk():
    d=day()
    assert d.average_risk_score is None and d.observed_return_pct is None and d.risk_samples==0


def test_mock_and_unverified_history_never_enter_real_analytics(reports):
    history=InMemoryActivityStore(recording_source='mock')
    history.record_order(order('mock',NOW-timedelta(days=1)),'app')
    days=[day(1),day(2,recording_source='mock'),day(3,recording_source='unknown')]
    report=build_analytics(days,history,reports,NOW,recording_source='mock')
    assert report.summary.days==1
    assert report.symbols==[] and report.pace.orders_now is None
    reports.observe(FACTS,100,NOW,source='mock')
    real=reports.observe(FACTS,20,NOW,source='zerotwoone')
    assert real['average_risk_score']==20 and real['risk_samples']==1


def test_mock_api_does_not_claim_021_observations():
    settings=Settings(ticker_interval=None,reconcile_interval=None,external_sync_interval=None)
    with TestClient(create_app(settings,clock=lambda:NOW)) as c:
        report=c.get('/api/discipline').json()
        assert report['analytics']['today'] is None
        assert report['analytics']['summary']['days']==0
        assert report['analytics']['pace']['orders_now'] is None


def test_api_persists_observations_and_risk_chat_respects_mindful_mode():
    settings=Settings(broker='mock',llm_provider='rules',demo_mode=False,ticker_interval=None,reconcile_interval=None,external_sync_interval=None)
    with TestClient(create_app(settings,clock=lambda:NOW)) as c:
        s=c.app.state
        # Isolated fixture of broker facts; no real credentials, network or account history.
        s.discipline.recording_source='zerotwoone'
        s.profile_store.save_profile(PROFILE.model_copy(update={'hide_day_pnl':True}))
        with patch('app.risk.service.compute_today',new_callable=AsyncMock,return_value=FACTS.model_copy(update={'pnl_after_charges':12345})):
            report=c.get('/api/discipline').json()
            assert report['history']['source']=='none' and report['analytics']['summary']['days']==0
            assert report['analytics']['today']['risk_samples']==1
            reply=c.post('/api/chat',json={'message':'Show my average risk and trading patterns'}).json()
            assert 'mindful' in reply['text'] and '123.45' not in reply['text']
            assert not reply['cards']
            assert c.post('/api/chat',json={'message':'Show my risk profile'}).status_code==200
            assert not s.broker._orders
