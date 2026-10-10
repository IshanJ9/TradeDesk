"""App factory. Run with: uvicorn app.main:create_app --factory --reload"""

import contextlib
import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import rest, ws_router
from app.auth.api import private as auth_private, public as auth_public
from app.auth.ratelimit import LoginLimiter
from app.auth.service import AuthService
from app.auth.store import AuthStore, UserRow
from app.cocaptain.actors import AccountDirectory
from app.cocaptain.api import router as cocaptain_router
from app.cocaptain.audit import OwnerAudit
from app.cocaptain.events import UserNotifier
from app.cocaptain.pairing import Pairing
from app.cocaptain.review_api import router as review_router
from app.cocaptain.store import ReviewStore
from app.broker.base import BrokerAdapter, BrokerTimeout
from app.broker.kind import broker_kind
from app.broker.mock import MockBroker
from app.broker_api import router as broker_router
from app.broker_links import BrokerLinks, LinkedBrokers
from app.config import Settings
from app.db import Database
from app.events import EventHub
from app.identity import Actor
from app.llm.factory import make_llm
from app.llm.types import LLMUnavailable
from app.risk.api import router as risk_router
from app.vault import Vault
from app.schema import adopt_legacy_rows
from app.auth.service import actor_of
from app.voice.api import router as voice_router  # voice-live: transcription only
from app.sync.dev import router as sync_dev_router  # voice-live: demo-only trace sample
from app.sync.api import router as activity_router  # voice-live: restore saved activity on refresh
from app.workspace import WorkspaceRegistry

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



def _real_021(settings: Settings, username: str, password: str) -> BrokerAdapter:
    from app.broker.zerotwoone import ZeroTwoOneAdapter

    return ZeroTwoOneAdapter(username=username, password=password, base_url=settings.zerotwoone_base_url,
                             cache_dir=settings.zerotwoone_cache_dir)


def create_app(
    settings: Settings | None = None,
    broker: BrokerAdapter | None = None,
    clock: Callable[[], datetime] = _utcnow,
    broker_factory: Callable[[Actor], BrokerAdapter] | None = None,
    zerotwoone_factory: Callable[[str, str], BrokerAdapter] | None = None,
) -> FastAPI:
    """`broker` is the account configured for this server (BROKER / .env): it belongs to the owner, the first person
    to register (or TRADEDESK_OWNER_EMAIL). Every other user gets `broker_factory(user)`, which by default is their
    own mock account."""
    settings = settings or Settings.from_env()
    # Misconfiguration stops the app at start, not at some user's first request.
    if settings.orchestrator not in ("classic", "langgraph"):
        raise RuntimeError(f"ORCHESTRATOR={settings.orchestrator!r}: use classic or langgraph")
    make_llm(settings, {})
    default_broker = broker or make_broker(settings)
    make_user_broker = broker_factory or (lambda _actor: MockBroker(clock=clock))
    claimed = {"default": False}

    db = Database(settings.database_url)
    events = EventHub()
    auth_store = AuthStore(db)

    def owner() -> UserRow | None:
        return auth_store.user_by_email(settings.owner_email) if settings.owner_email else auth_store.first_user()

    def user_created(user: UserRow, first: bool) -> None:
        """Data saved before accounts existed goes to the owner (and to nobody else)."""
        if (settings.owner_email and user.email == settings.owner_email) or (not settings.owner_email and first):
            adopted = adopt_legacy_rows(db, user.id)
            if adopted:
                log.info("handed data saved before accounts existed to the owner account: %s", adopted)

    def broker_for(actor: Actor) -> tuple[BrokerAdapter, bool]:
        o = owner()
        if o is not None and o.id == actor.id and not claimed["default"]:
            claimed["default"] = True
            return default_broker, True  # already started in the lifespan, so a wrong 021 password stops the app early
        return make_user_broker(actor), False

    # A user may link their own 021 account (Settings). Their login is stored encrypted under TRADEDESK_SECRET_KEY;
    # with no key, linking is off and everything else works exactly as before.
    vault = Vault(settings.secret_key, settings.secret_key_previous)
    links = BrokerLinks(db, vault)
    make_021 = zerotwoone_factory or (lambda username, password: _real_021(settings, username, password))
    linked = LinkedBrokers(links, make_021, clock)
    server_has_real_account = broker_kind(default_broker) == "021"

    def uses_server_account(actor: Actor) -> bool:
        """The owner of a server that is configured with a real 021 login trades on it; it is managed in .env."""
        o = owner()
        return server_has_real_account and o is not None and o.id == actor.id

    def uses_server_account_ucc(ucc: str) -> bool:
        return server_has_real_account and settings.zerotwoone_username.strip().upper() == ucc.strip().upper()

    # Co-Captain: the pairing between two accounts and the reviews of cards sent to a Co-Captain are the only things
    # two people share. Each trader's own gate lives in their workspace (app/workspace.py).
    shared_audit = OwnerAudit(db, clock, events)
    review_hub = UserNotifier(events)  # a pairing change or a card sent for review reaches that person's desk at once
    pairing = Pairing(db, AccountDirectory(auth_store), shared_audit, review_hub, clock)
    reviews = ReviewStore(db, pairing, shared_audit, clock)

    registry = WorkspaceRegistry(settings=settings, clock=clock, db=db, events=events, broker_for=broker_for,
                                 linked_broker_for=linked.open, cocaptain=(pairing, reviews) if settings.cocaptain_enabled else None)

    def on_revoke(link, actor):
        """Ending a pairing closes every open review under it and cancels the cards still waiting on that Co-Captain."""
        reviews.revoke_link(link, actor)
        desk = registry.peek(link.owner_id)
        if desk is not None:
            note = "Your Co-Captain link ended, so this card is cancelled. Nothing was sent. Ask again for a fresh one."
            desk.approvals.void_waiting_on(link.reviewer_id, note)
            desk.plans.void_waiting_on(link.reviewer_id, note)

    pairing.on_revoke = on_revoke

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        # A live broker logs in and opens its price feed first. If it cannot (wrong password, no network) the
        # app does not start, rather than running with a broker that is not there.
        await default_broker.start()
        if vault.available:
            links.rewrap(clock())  # records sealed under the previous key are re-sealed under the current one
        o = owner()
        if o is not None:
            adopt_legacy_rows(db, o.id)  # finishes the hand-over if a crash interrupted it; a no-op once done
        # Every existing account gets its desk now, so standing rules keep watching prices while their owner is away.
        for user in auth_store.all_users():
            if user.disabled:
                continue
            try:
                await registry.get(actor_of(user))
            except Exception:
                if o is not None and user.id == o.id:
                    raise  # the configured account must work, as it always has
                log.warning("a user's desk could not be started at startup; it will be retried on their next request")
        try:
            yield
        finally:
            await registry.stop_all()
            if not claimed["default"]:
                with contextlib.suppress(Exception):
                    await default_broker.close()
            db.close()

    app = FastAPI(title="TradeDesk-AI", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.clock = clock
    app.state.db = db
    app.state.events = events
    app.state.cocaptain_pairing, app.state.cocaptain_reviews = pairing, reviews
    app.state.workspaces = registry
    app.state.default_broker = default_broker
    app.state.vault, app.state.links, app.state.linked = vault, links, linked
    app.state.zerotwoone_factory = make_021
    app.state.uses_server_account, app.state.uses_server_account_ucc = uses_server_account, uses_server_account_ucc
    app.state.link_limiter = LoginLimiter(clock, max_per_email=5, max_per_ip=20)  # attempts to link, per user and per IP
    app.state.auth = AuthService(
        auth_store, clock, LoginLimiter(clock),
        idle_timeout=timedelta(hours=settings.session_idle_hours),
        absolute_timeout=timedelta(days=settings.session_absolute_days),
        on_user_created=user_created,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.allowed_origins),  # the Vite dev server by default; the cookie needs credentials
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Content-Type", "X-CSRF-Token"],
    )

    @app.exception_handler(BrokerTimeout)
    async def broker_unreachable(_: Request, exc: BrokerTimeout):
        return JSONResponse(status_code=503, content={"detail": "broker unreachable"})

    @app.exception_handler(LLMUnavailable)
    async def assistant_unavailable(_: Request, exc: LLMUnavailable):
        return JSONResponse(status_code=503, content={"detail": "assistant unavailable"})

    @app.get("/api/health", tags=["health"])
    async def health():
        return {"status": "ok"}

    app.include_router(auth_public)
    app.include_router(auth_private)
    app.include_router(rest)
    app.include_router(broker_router)
    app.include_router(ws_router)
    app.include_router(voice_router)  # voice-live: editable text, never an order action
    app.include_router(sync_dev_router)  # voice-live: returns 404 outside demo mode
    app.include_router(activity_router)  # voice-live: read-only persisted history
    app.include_router(risk_router)  # risk-goals
    app.include_router(cocaptain_router)  # who is whose Co-Captain
    app.include_router(review_router)  # the Co-Captain's inbox and Approve / Decline: only ever their own reviews
    from app.demo import router as demo_router  # demo controls: 404 unless DEMO_MODE and the mock broker

    app.include_router(demo_router)
    return app
