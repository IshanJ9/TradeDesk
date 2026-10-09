"""App factory. Run with: uvicorn app.main:create_app --factory --reload"""

import asyncio
import contextlib
import logging
from collections.abc import Callable
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.account import build_account
from app.api import rest, ws_router
from app.api_models import AccountUpdateEvent, OrderUpdateEvent, TickEvent
from app.audit import AuditLog
from app.broker.base import BrokerAdapter, BrokerTimeout
from app.broker.mock import MockBroker
from app.config import Settings
from app.db import Database
from app.events import EventHub
from app.history.store import InMemoryActivityStore
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
from app.risk.guard import NoRiskGuard
from app.rules.engine import RuleEngine
from app.rules.service import RuleService
from app.rules.store import RuleStore
from app.schemas import RuleStatus

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
    seen: dict[str, tuple] | None = None  # order signatures already pushed
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
            orders = await state.broker.get_orders()
            sigs = {o.order_id: (o.status, o.filled_quantity, o.quantity, o.limit_price) for o in orders}
            if seen is not None:  # first pass only records the baseline
                for order in orders:
                    if seen.get(order.order_id) != sigs[order.order_id]:
                        state.hub.publish(OrderUpdateEvent, order=order)
            seen = sigs
        except BrokerTimeout:
            continue
        except Exception:  # keep the feed alive; one bad push must not stop live prices
            log.exception("tick bridge error")


async def _reconcile_loop(app: FastAPI, interval: float) -> None:
    while True:
        await asyncio.sleep(interval)
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

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        # A live broker logs in and opens its price feed first. If it cannot (wrong password, no network) the
        # app does not start, rather than running with a broker that is not there.
        await app.state.broker.start()
        await app.state.broker.watch(
            sorted({r.condition.instrument_key for r in app.state.rule_store.list(RuleStatus.ACTIVE)})
        )
        # Crash recovery: anything that was mid-send when we last stopped gets looked up, not re-sent.
        try:
            await app.state.executor.reconcile()
            await app.state.rule_engine.recover()
        except BrokerTimeout:
            log.warning("broker unreachable at startup; unresolved executions and rules stay as they are")
        tasks = [asyncio.create_task(_tick_bridge(app))]
        if settings.reconcile_interval:
            tasks.append(asyncio.create_task(_reconcile_loop(app, settings.reconcile_interval)))
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
    hub, store, db = EventHub(), PendingStore(), Database(settings.database_url)
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
    app.state.pending = store
    app.state.db = db
    app.state.audit = audit
    app.state.builder = builder
    app.state.executor = executor
    # Shared hooks for the parallel workstreams (each owner replaces only their own line):
    history = InMemoryActivityStore()  # voice-live: database-backed store + order sync
    risk = NoRiskGuard()  # risk-goals: the trader's own limits
    app.state.history = history
    app.state.risk = risk
    cards = CardService(builder, store, hub, audit, risk)
    tools = build_tools()
    llm = make_llm(settings, {name: t.render for name, t in tools.items()})
    rule_store = RuleStore(db)
    rules = RuleService(rule_store, the_broker.read_only(), cards, builder.limits, audit, hub, settings, clock)
    plan_store = PlanStore()
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

    app.include_router(rest)
    app.include_router(ws_router)
    return app
