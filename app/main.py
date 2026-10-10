"""App factory. Run with: uvicorn app.main:create_app --factory --reload"""

import asyncio
import contextlib
import logging
from collections.abc import Callable
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.account import build_account
from app.api import rest, ws_router
from app.api_models import AccountUpdateEvent, TickEvent
from app.audit import AuditLog
from app.broker.base import BrokerAdapter, BrokerTimeout
from app.broker.mock import MockBroker
from app.config import Settings
from app.cocaptain.actors import DevDirectory, require_account_owner
from app.cocaptain.api import router as cocaptain_router
from app.cocaptain.events import ReviewHub
from app.cocaptain.pairing import Pairing
from app.db import Database
from app.events import EventHub
from app.history.sqlite_store import SqliteActivityStore  # voice-live: durable activity
from app.orders.approval import ApprovalService
from app.llm.copilot import Copilot
from app.llm.factory import make_llm
from app.llm.tools import build_tools
from app.llm.types import LLMUnavailable
from app.orders.builder import OrderBuilder
from app.orders.cards import CardService
from app.orders.executor import Executor
from app.pending import PendingStore
from app.plans.builder import PlanBuilder
from app.plans.service import PlanAssistant, PlanService
from app.plans.store import PlanStore
# risk-goals: profile/goal persistence, routes and trader-selected limits.
from app.cocaptain.review_api import router as review_router
from app.cocaptain.service import CoCaptainGate
from app.cocaptain.store import ReviewStore
from app.risk.engine import ProfileGuard
from app.risk.api import router as risk_router
from app.risk.store import ProfileStore
from app.risk.report_store import ReportStore  # risk-goals
from app.risk.service import DisciplineService  # risk-goals
from app.rules.engine import RuleEngine
from app.rules.service import RuleService
from app.rules.store import RuleStore
from app.schemas import RuleStatus
from app.voice.api import router as voice_router  # voice-live: transcription only
from app.sync.external import run_external_sync  # voice-live: uses the existing broker session
from app.sync.order_events import OrderPublisher, run_order_watcher, wait_or_wake
from app.sync.dev import router as sync_dev_router  # voice-live: demo-only trace sample
from app.sync.api import router as activity_router  # voice-live: restore saved activity on refresh

log = logging.getLogger("tradedesk")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def make_broker(settings: Settings) -> BrokerAdapter:
    if settings.broker == "mock":
        return MockBroker()
    if settings.broker == "zerotwoone":
        if not settings.zerotwoone_username or not settings.zerotwoone_password:
            raise RuntimeError("BROKER=zerotwoone needs ZEROTWOONE_USERNAME and ZEROTWOONE_PASSWORD in .env")
        from app.broker.zerotwoone import ZeroTwoOneAdapter

        return ZeroTwoOneAdapter(
            username=settings.zerotwoone_username,
            password=settings.zerotwoone_password,
            base_url=settings.zerotwoone_base_url,
            cache_dir=settings.zerotwoone_cache_dir,
        )
    raise NotImplementedError(f"BROKER={settings.broker!r}: use mock or zerotwoone")


async def _tick_bridge(app: FastAPI) -> None:
    """Broker ticks -> websocket events, plus a throttled live account and order refresh."""
    state = app.state
    loop = asyncio.get_running_loop()
    last_push = loop.time()  # clients start from a snapshot, so the first push waits one interval
    async for tick in state.broker.subscribe_ticks([]):
        try:
            state.hub.publish(TickEvent, tick=tick)
            try:
                await state.rule_engine.on_tick(tick)
            except BrokerTimeout:
                pass  # the card/alert is retried by the periodic recover()
            except Exception:
                log.exception("rule engine error")
            if loop.time() - last_push < state.settings.account_push_interval:
                continue
            last_push = loop.time()
            state.hub.publish(AccountUpdateEvent, account=await build_account(state.broker))
            await state.order_publisher.publish_changes()  # shared with the orders-socket watcher
        except BrokerTimeout:
            continue
        except Exception:  # keep the feed alive; one bad push must not stop live prices
            log.exception("tick bridge error")


async def _reconcile_loop(app: FastAPI, interval: float, wake: asyncio.Event | None = None) -> None:
    while True:
        await wait_or_wake(interval, wake)  # sooner when the orders socket reports something
        try:
            await app.state.executor.reconcile()
            await app.state.rule_engine.recover()
        except Exception:
            log.exception("reconcile error")


def create_app(
    settings: Settings | None = None,
    broker: BrokerAdapter | None = None,
    clock: Callable[[], datetime] = _utcnow,
) -> FastAPI:
    settings = settings or Settings.from_env()
    if settings.cocaptain_dev_actors and not settings.demo_mode:
        raise ValueError("COCAPTAIN_DEV_ACTORS requires DEMO_MODE")
    if settings.cocaptain_enabled and not settings.cocaptain_account_owner_id:
        raise ValueError("Co-Captain requires an explicit account owner ID")

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        # A live broker logs in and opens its price feed first. If it cannot (wrong password, no network) the
        # app does not start, rather than running with a broker that is not there.
        await app.state.broker.start()
        await app.state.broker.watch(
            sorted({r.condition.instrument_key for r in app.state.rule_store.list(RuleStatus.ACTIVE)})
        )
        # Crash recovery: anything that was mid-send when we last stopped gets looked up, not re-sent.
        app.state.plans.recover()  # a plan whose runner stopped with the app is halted, never resumed blindly
        try:
            await app.state.executor.reconcile()
            await app.state.rule_engine.recover()
        except BrokerTimeout:
            log.warning("broker unreachable at startup; unresolved executions and rules stay as they are")
        tasks = [asyncio.create_task(_tick_bridge(app))]
        # A broker with an orders socket (021) wakes these loops on every order event and after every reconnect;
        # they then read REST. Without one (the mock) they simply poll on their timers.
        order_wake = getattr(app.state.broker, "order_wake", None)
        wake = order_wake.subscribe if order_wake is not None else (lambda: None)
        if order_wake is not None:
            tasks.append(asyncio.create_task(run_order_watcher(app, order_wake.subscribe())))
        # voice-live: participates in the same cancellation/shutdown as the other tasks.
        if settings.external_sync_interval is not None:
            tasks.append(asyncio.create_task(run_external_sync(app, settings.external_sync_interval, wake())))
        tasks.append(asyncio.create_task(app.state.discipline.run()))  # risk-goals: 20-second reports
        if settings.reconcile_interval:
            tasks.append(asyncio.create_task(_reconcile_loop(app, settings.reconcile_interval, wake())))
        if isinstance(app.state.broker, MockBroker) and settings.ticker_interval:
            tasks.append(asyncio.create_task(app.state.broker.run_ticker(settings.ticker_interval)))
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await app.state.plans.shutdown()  # a plan in flight stops cleanly; unsent steps stay unsent
            await app.state.broker.close()
            app.state.db.close()

    app = FastAPI(title="TradeDesk-AI", version="0.1.0", lifespan=lifespan)
    the_broker = broker or make_broker(settings)
    hub, db = EventHub(), Database(settings.database_url)
    store = PendingStore(db)  # cards waiting for approval survive a restart
    audit = AuditLog(db, clock, hub)
    builder = OrderBuilder(the_broker, settings, clock)
    executor = Executor(
        the_broker,
        db,
        audit,
        store,
        hub,
        clock,
        reconcile_attempts=settings.timeout_reconcile_attempts,
        reconcile_delay=settings.timeout_reconcile_delay,
        grace_seconds=settings.reconcile_grace_seconds,
    )
    app.state.settings = settings
    app.state.clock = clock
    app.state.broker = the_broker
    app.state.hub = hub
    app.state.order_publisher = OrderPublisher(the_broker, hub)  # one "order changed" stream for the screen
    app.state.pending = store
    app.state.db = db
    app.state.audit = audit
    app.state.cocaptain_directory = DevDirectory(settings.cocaptain_dev_users if settings.cocaptain_dev_actors else "[]")
    app.state.cocaptain_hub = ReviewHub()
    app.state.cocaptain_pairing = Pairing(db, app.state.cocaptain_directory, audit, app.state.cocaptain_hub, clock)
    if settings.cocaptain_enabled and settings.cocaptain_dev_actors:
        if app.state.cocaptain_directory.by_id(settings.cocaptain_account_owner_id) is None:
            raise ValueError("The account owner must be in the configured dev actor directory")
    app.state.builder = builder
    app.state.executor = executor
    # Shared hooks for the parallel workstreams (each owner replaces only their own line):
    recording_source = "mock" if isinstance(the_broker, MockBroker) else settings.broker
    history = SqliteActivityStore(db, recording_source=recording_source)
    profile_store = ProfileStore(db)  # risk-goals: own tables on the shared database
    risk = ProfileGuard(the_broker.read_only(), profile_store, clock)  # risk-goals
    app.state.history = history
    app.state.risk = risk
    app.state.profile_store = profile_store  # risk-goals
    app.state.discipline = DisciplineService(  # risk-goals: reads only; owns its report tables
        the_broker.read_only(), profile_store, ReportStore(db), lambda: app.state.history, hub, clock,
        settings.demo_mode and settings.broker == "mock", recording_source=recording_source
    )
    cards = CardService(builder, store, hub, audit, risk)
    tools = build_tools(profile_reader=profile_store.get_profile, discipline_reader=app.state.discipline.refresh)
    renderers = {name: t.render for name, t in tools.items()}
    llm = make_llm(settings, renderers)
    if settings.llm_provider not in ("", "rules"):  # a provider outage falls back to the keyword stand-in
        from app.llm.fallback import FallbackLLM
        from app.llm.rules import RuleBasedLLM

        llm = FallbackLLM(llm, RuleBasedLLM(renderers))
    rule_store = RuleStore(db)
    rules = RuleService(rule_store, the_broker.read_only(), cards, builder.limits, audit, hub, settings, clock)
    plan_store = PlanStore(db)  # plans and their reports survive a restart
    plans = PlanService(
        plan_store,
        PlanBuilder(builder, the_broker.read_only(), settings, clock),
        builder,
        executor,
        the_broker,
        audit,
        hub,
        settings,
        clock,
        risk,  # every plan step is checked against the trader's own limits too
    )
    app.state.cards = cards
    app.state.rule_store = rule_store
    app.state.rules = rules
    app.state.plan_store = plan_store
    app.state.plans = plans
    app.state.rule_engine = RuleEngine(rule_store, cards, the_broker.read_only(), audit, hub, settings, clock)
    copilot_args = (llm, tools, the_broker.read_only(), cards, rules, PlanAssistant(plans), audit, clock)
    if settings.orchestrator == "langgraph":
        from app.agent.graph import GraphCopilot  # imported only when used: classic mode needs no LangGraph

        app.state.copilot = GraphCopilot(*copilot_args, hub=hub)
    elif settings.orchestrator == "classic":
        app.state.copilot = Copilot(*copilot_args)
    else:
        raise RuntimeError(f"ORCHESTRATOR={settings.orchestrator!r}: use classic or langgraph")
    app.state.approvals = ApprovalService(store, builder, executor, the_broker, audit, hub, settings, clock, risk)
    reviews = ReviewStore(db, app.state.cocaptain_pairing, audit, clock)
    gate = CoCaptainGate(settings, app.state.cocaptain_pairing, reviews, risk, audit, clock)
    app.state.cocaptain = app.state.approvals.cocaptain = plans.cocaptain = cards.cocaptain = gate

    def on_revoke(link, actor):  # ending the pairing closes every open review and voids cards still waiting on it
        reviews.revoke_link(link, actor)
        app.state.approvals.void_waiting_on(link.reviewer_id, "Your Co-Captain link ended, so this card is cancelled. "
                                            "Nothing was sent. Ask again for a fresh one.")

    app.state.cocaptain_pairing.on_revoke = on_revoke

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],  # Vite dev server
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["*"],
    )

    @app.exception_handler(BrokerTimeout)
    async def broker_unreachable(_: Request, exc: BrokerTimeout):
        return JSONResponse(status_code=503, content={"detail": "broker unreachable"})

    @app.exception_handler(LLMUnavailable)
    async def assistant_unavailable(_: Request, exc: LLMUnavailable):
        return JSONResponse(status_code=503, content={"detail": "assistant unavailable"})

    owner_only = [Depends(require_account_owner)]
    app.include_router(rest, dependencies=owner_only)
    app.include_router(ws_router, dependencies=owner_only)
    app.include_router(voice_router, dependencies=owner_only)
    app.include_router(sync_dev_router, dependencies=owner_only)
    app.include_router(activity_router, dependencies=owner_only)
    app.include_router(risk_router, dependencies=owner_only)
    app.include_router(cocaptain_router)
    app.include_router(review_router)  # the Co-Captain's inbox and Approve / Decline: only ever their own reviews
    from app.demo import router as demo_router  # demo controls: 404 unless DEMO_MODE and the mock broker

    app.include_router(demo_router, dependencies=owner_only)
    return app
