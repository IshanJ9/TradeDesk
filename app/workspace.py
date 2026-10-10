"""One trader's desk: their broker session, stores, assistant, rules, plans and background loops.

Before accounts, create_app built one of each. Now it builds a `Workspace` for every user (on demand, and at startup
for everyone who already has an account so their rules keep watching prices while they are away). Everything a
workspace holds is bound to its user: stores filter by `user_id`, the event hub publishes only to that user, the
broker session is that user's own. Nothing in here can be reached with another user's id, because the route layer
picks the workspace from the signed-in session (see `bind_workspace`), never from the request.

The loops of different users are separate asyncio tasks: one that is slow, stuck or failing does not stall another.
"""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime

from app.account import build_account
from app.api_models import AccountUpdateEvent, TickEvent
from app.audit import AuditLog
from app.broker.base import BrokerAdapter, BrokerTimeout
from app.broker.kind import AccountKind, broker_kind
from app.broker.mock import MockBroker
from app.config import Settings
from app.db import Database
from app.events import EventHub, UserHub
from app.history.sqlite_store import SqliteActivityStore
from app.identity import Actor
from app.llm.copilot import Copilot
from app.llm.factory import make_llm
from app.llm.tools import build_tools
from app.orders.approval import ApprovalService
from app.orders.builder import OrderBuilder
from app.orders.cards import CardService
from app.orders.executor import Executor
from app.pending import PendingStore
from app.plans.builder import PlanBuilder
from app.plans.service import PlanAssistant, PlanService
from app.plans.store import PlanStore
from app.risk.engine import ProfileGuard
from app.risk.report_store import ReportStore
from app.risk.service import DisciplineService
from app.risk.store import ProfileStore
from app.rules.engine import RuleEngine
from app.rules.service import RuleService
from app.rules.store import RuleStore
from app.schemas import AuditKind, PendingState, PlanState, RuleStatus
from app.sync.external import ExternalOrderSync
from app.sync.order_events import OrderPublisher, run_order_watcher, wait_or_wake

log = logging.getLogger("tradedesk.workspace")


class Workspace:
    def __init__(self, *, user: Actor, settings: Settings, clock: Callable[[], datetime], db: Database,
                 events: EventHub, broker: BrokerAdapter, broker_started: bool = False):
        uid = user.id
        self.user, self.user_id = user, uid
        self.settings, self.clock, self.db = settings, clock, db
        self.broker = broker
        self._broker_started = broker_started
        self.hub: UserHub = events.for_user(uid)
        hub = self.hub
        self.pending = PendingStore(db, user_id=uid)  # cards waiting for approval survive a restart
        self.audit = AuditLog(db, clock, hub, user_id=uid)
        self.builder = OrderBuilder(broker, settings, clock)
        self.executor = Executor(
            broker, db, self.audit, self.pending, hub, clock, user_id=uid,
            reconcile_attempts=settings.timeout_reconcile_attempts,
            reconcile_delay=settings.timeout_reconcile_delay,
            grace_seconds=settings.reconcile_grace_seconds,
        )
        self.order_publisher = OrderPublisher(broker, hub)  # one "order changed" stream for the screen
        recording_source = "mock" if isinstance(broker, MockBroker) else settings.broker
        self.history = SqliteActivityStore(db, user_id=uid, recording_source=recording_source)
        self.profile_store = ProfileStore(db, user_id=uid)
        self.risk = ProfileGuard(broker.read_only(), self.profile_store, clock)
        self.discipline = DisciplineService(
            broker.read_only(), self.profile_store, ReportStore(db, user_id=uid), lambda: self.history, hub, clock,
            settings.demo_mode and settings.broker == "mock", recording_source=recording_source,
        )
        self.cards = CardService(self.builder, self.pending, hub, self.audit, self.risk)
        tools = build_tools(profile_reader=self.profile_store.get_profile, discipline_reader=self.discipline.refresh)
        renderers = {name: t.render for name, t in tools.items()}
        llm = make_llm(settings, renderers)
        if settings.llm_provider not in ("", "rules"):  # a provider outage falls back to the keyword stand-in
            from app.llm.fallback import FallbackLLM
            from app.llm.rules import RuleBasedLLM

            llm = FallbackLLM(llm, RuleBasedLLM(renderers))
        self.rule_store = RuleStore(db, user_id=uid)
        self.rules = RuleService(self.rule_store, broker.read_only(), self.cards, self.builder.limits, self.audit, hub, settings, clock)
        self.plan_store = PlanStore(db, user_id=uid)  # plans and their reports survive a restart
        self.plans = PlanService(
            self.plan_store, PlanBuilder(self.builder, broker.read_only(), settings, clock), self.builder, self.executor,
            broker, self.audit, hub, settings, clock, self.risk,  # every plan step is checked against the trader's own limits too
        )
        self.rule_engine = RuleEngine(self.rule_store, self.cards, broker.read_only(), self.audit, hub, settings, clock)
        copilot_args = (llm, tools, broker.read_only(), self.cards, self.rules, PlanAssistant(self.plans), self.audit, clock)
        if settings.orchestrator == "langgraph":
            from app.agent.graph import GraphCopilot  # imported only when used: classic mode needs no LangGraph

            self.copilot = GraphCopilot(*copilot_args, hub=hub)
        elif settings.orchestrator == "classic":
            self.copilot = Copilot(*copilot_args)
        else:
            raise RuntimeError(f"ORCHESTRATOR={settings.orchestrator!r}: use classic or langgraph")
        self.approvals = ApprovalService(self.pending, self.builder, self.executor, broker, self.audit, hub, settings, clock, self.risk)
        self._tasks: list[asyncio.Task] = []
        self.started = False

    @property
    def account_kind(self) -> AccountKind:
        return broker_kind(self.broker)

    @property
    def broker_status(self) -> str:
        """mock | connected | needs_reconnect (a 021 session that is not usable right now)."""
        if self.account_kind == "mock":
            return "mock"
        return "needs_reconnect" if getattr(self.broker, "needs_reconnect", False) else "connected"

    def switch_blocker(self) -> str | None:
        """Why this desk's broker account cannot be changed right now, or None. An order whose outcome is not known, or
        a plan that is running, belongs to the account it was sent to."""
        if self.executor.unresolved():
            return "An order you sent is still waiting for 021's answer. Wait until it is confirmed, then try again."
        if any(p.state in (PlanState.APPROVED, PlanState.RUNNING) for p in self.plan_store.all()):
            return "A plan is running. Wait until it finishes, then try again."
        return None

    async def reject_pending_for_account_change(self) -> int:
        """Cards and plans were priced and checked against the old account: none of them may be approved on the new one."""
        n = 0
        for card in self.pending.awaiting_approval():
            try:
                await self.approvals.reject(card.id)
                n += 1
            except Exception:  # already moved on (approved, expired): nothing to reject
                pass
        for plan in self.plan_store.awaiting_approval():
            try:
                await self.plans.reject(plan.id)
                n += 1
            except Exception:
                pass
        return n

    def note_account_change(self, what: str) -> None:
        self.audit.record(AuditKind.BROKER_LINK, "user", what)

    # ---- lifecycle ------------------------------------------------------------------------------ #

    async def start(self) -> None:
        """Log the broker in, recover anything that was mid-send, and start this user's loops."""
        settings = self.settings
        if not self._broker_started:
            await self.broker.start()
            self._broker_started = True
        await self.broker.watch(sorted({r.condition.instrument_key for r in self.rule_store.list(RuleStatus.ACTIVE)}))
        # Crash recovery: anything that was mid-send when we last stopped gets looked up, not re-sent.
        self.plans.recover()  # a plan whose runner stopped with the app is halted, never resumed blindly
        try:
            await self.executor.reconcile()
            await self.rule_engine.recover()
        except BrokerTimeout:
            log.warning("broker unreachable at startup; unresolved executions and rules stay as they are")
        tasks = [asyncio.create_task(self._tick_bridge())]
        # A broker with an orders socket (021) wakes these loops on every order event and after every reconnect;
        # they then read REST. Without one (the mock) they simply poll on their timers.
        order_wake = getattr(self.broker, "order_wake", None)
        wake = order_wake.subscribe if order_wake is not None else (lambda: None)
        if order_wake is not None:
            tasks.append(asyncio.create_task(run_order_watcher(self, order_wake.subscribe())))
        if settings.external_sync_interval is not None:
            sync = ExternalOrderSync(self.broker, self.db, self.history, self.hub, self.clock,
                                     settings.reconcile_grace_seconds, user_id=self.user_id)
            tasks.append(asyncio.create_task(sync.run(settings.external_sync_interval, wake())))
        tasks.append(asyncio.create_task(self.discipline.run()))  # 20-second reports
        if settings.reconcile_interval:
            tasks.append(asyncio.create_task(self._reconcile_loop(settings.reconcile_interval, wake())))
        if isinstance(self.broker, MockBroker) and settings.ticker_interval:
            tasks.append(asyncio.create_task(self.broker.run_ticker(settings.ticker_interval)))
        self._tasks = tasks
        self.started = True

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        self.started = False
        await self.plans.shutdown()  # a plan in flight stops cleanly; unsent steps stay unsent
        with contextlib.suppress(Exception):
            await self.broker.close()

    # ---- loops ---------------------------------------------------------------------------------- #

    async def _tick_bridge(self) -> None:
        """Broker ticks -> this user's websocket events, plus a throttled live account and order refresh."""
        loop = asyncio.get_running_loop()
        last_push = loop.time()  # clients start from a snapshot, so the first push waits one interval
        async for tick in self.broker.subscribe_ticks([]):
            try:
                self.hub.publish(TickEvent, tick=tick)
                try:
                    await self.rule_engine.on_tick(tick)
                except BrokerTimeout:
                    pass  # the card/alert is retried by the periodic recover()
                except Exception:
                    log.exception("rule engine error")
                if loop.time() - last_push < self.settings.account_push_interval:
                    continue
                last_push = loop.time()
                self.hub.publish(AccountUpdateEvent, account=await build_account(self.broker))
                await self.order_publisher.publish_changes()  # shared with the orders-socket watcher
            except BrokerTimeout:
                continue
            except Exception:  # keep the feed alive; one bad push must not stop live prices
                log.exception("tick bridge error")

    async def _reconcile_loop(self, interval: float, wake: asyncio.Event | None = None) -> None:
        while True:
            await wait_or_wake(interval, wake)  # sooner when the orders socket reports something
            try:
                await self.executor.reconcile()
                await self.rule_engine.recover()
            except Exception:
                log.exception("reconcile error")


class WorkspaceUnavailable(Exception):
    """This user's desk could not be started (for example their broker login failed)."""


class WorkspaceRegistry:
    """Finds or builds the workspace for a user. Workspaces live until the app stops."""

    def __init__(self, *, settings: Settings, clock: Callable[[], datetime], db: Database, events: EventHub,
                 broker_for: Callable[[Actor], tuple[BrokerAdapter, bool]],
                 linked_broker_for: Callable[[Actor], Awaitable[tuple[BrokerAdapter, bool] | None]] | None = None):
        self._settings, self._clock, self._db, self._events = settings, clock, db, events
        self._broker_for = broker_for  # (actor) -> (broker, already_started): the server's account, or a mock
        self._linked_broker_for = linked_broker_for  # async (actor) -> the user's own linked 021 session, or None
        self._workspaces: dict[str, Workspace] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def peek(self, user_id: str) -> Workspace | None:
        return self._workspaces.get(user_id)

    def all(self) -> list[Workspace]:
        return list(self._workspaces.values())

    def build(self, user: Actor, broker: BrokerAdapter | None = None, started: bool = False) -> Workspace:
        """Make (but do not start) this user's workspace and remember it. Normally called by `get`."""
        if broker is None:
            broker, started = self._broker_for(user)
        ws = Workspace(user=user, settings=self._settings, clock=self._clock, db=self._db, events=self._events,
                       broker=broker, broker_started=started)
        self._workspaces[user.id] = ws
        return ws

    async def get(self, user: Actor) -> Workspace:
        existing = self._workspaces.get(user.id)
        if existing is not None and existing.started:
            return existing
        lock = self._locks.setdefault(user.id, asyncio.Lock())
        async with lock:  # two requests arriving together must not build two desks (and two broker logins)
            ws = self._workspaces.get(user.id)
            if ws is not None and ws.started:
                return ws
            fresh = ws is None
            if ws is None:
                linked = await self._linked_broker_for(user) if self._linked_broker_for else None
                ws = self.build(user, *linked) if linked else self.build(user)
            try:
                await ws.start()
            except Exception as exc:
                with contextlib.suppress(Exception):
                    await ws.stop()
                if fresh:
                    self._workspaces.pop(user.id, None)
                log.warning("could not start a desk for a user: %s", type(exc).__name__)
                raise WorkspaceUnavailable() from exc
            return ws

    async def replace(self, user: Actor, broker: BrokerAdapter | None = None, started: bool = False) -> Workspace:
        """Give this user a new desk on a different broker account (they linked, unlinked or reconnected). Their pending
        cards and plans are rejected first, the old desk stops, and their open pages are told to reconnect so nothing on
        screen still belongs to the old account. With no broker given, the user's saved link (if any) decides."""
        lock = self._locks.setdefault(user.id, asyncio.Lock())
        async with lock:
            old = self._workspaces.get(user.id)
            if old is not None:
                await old.reject_pending_for_account_change()
                await old.stop()
                self._workspaces.pop(user.id, None)
            if broker is None:
                linked = await self._linked_broker_for(user) if self._linked_broker_for else None
                ws = self.build(user, *linked) if linked else self.build(user)
            else:
                ws = self.build(user, broker, started)
            try:
                await ws.start()
            except Exception as exc:
                with contextlib.suppress(Exception):
                    await ws.stop()
                self._workspaces.pop(user.id, None)
                raise WorkspaceUnavailable() from exc
            self._events.disconnect_user(user.id)
            return ws

    async def stop_all(self) -> None:
        workspaces, self._workspaces = list(self._workspaces.values()), {}
        await asyncio.gather(*(w.stop() for w in workspaces), return_exceptions=True)
