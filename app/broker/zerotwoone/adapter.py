"""The 021 broker adapter.

Everything the rest of the app knows about 021 is in this package. Notes on where 021 differs from what
the app would like:

- No client order id: see `app/broker/matching.py` and the executor.
- No funds endpoint: cash is the day's starting balance less today's net trades and the money tied up in
  open buy orders. It is an ESTIMATE and the account panel's numbers for cash should be read that way.
- No locks endpoint: Anchor / Co-Captain are simulated locally (`locks`), nothing is read from 021.
- No quote endpoint: prices come from the market socket, so reading a price subscribes to it.
- No company names in the instrument file: see `names.py`.

Writes are never retried. Reads are retried a couple of times on 429/5xx and timeouts.
"""

import asyncio
import contextlib
import gzip
import logging
import os
from collections.abc import AsyncIterator, Callable, Collection, Sequence
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from app.broker.base import BrokerAdapter, BrokerError, BrokerRejected, BrokerTimeout
from app.broker.errors import classify_order_failure, reason_from_text
from app.broker.matching import match_sent_order
from app.broker.zerotwoone import codec
from app.broker.zerotwoone.feed import MarketFeed
from app.broker.zerotwoone.orders_feed import OrdersFeed
from app.sync.order_events import OrderWake
from app.broker.zerotwoone.instruments import WS_CODE, InstrumentMaster, Listing
from app.schemas import (
    AccountLocks,
    Funds,
    Holding,
    Instrument,
    MatchResult,
    OptionChain,
    OptionChainRow,
    OptionQuote,
    OptionType,
    Order,
    OrderAction,
    OrderStatus,
    OrderType,
    PendingOrder,
    Position,
    Quote,
    RejectionReason,
    SentOrderSpec,
    Side,
    Tick,
    paise,
)

log = logging.getLogger("tradedesk.021")

IST = timezone(timedelta(hours=5, minutes=30))
LIVE = (OrderStatus.PENDING, OrderStatus.OPEN, OrderStatus.PARTIAL)
DEFAULT_BASE_URL = "https://devapi.021.trade/api/developer-api/v1"


SESSION_TAKEN = (
    "021 says this login is no longer valid. 021 allows only one login per account at a time, so another copy of "
    "the app (or a script such as live_check.py) has probably logged in and cancelled this one. "
    "Close the other copy and try again."
)


class BrokerAuthFailed(BrokerError):
    """Login was refused (wrong UCC or password, or the account cannot trade)."""


def websocket_base(base_url: str) -> str:
    """https://host/api/developer-api/v1 -> wss://host/api/developer/websocket"""
    root = base_url.split("/api/", 1)[0]
    return root.replace("https://", "wss://").replace("http://", "ws://") + "/api/developer/websocket"


def _default_connect(url: str):
    from websockets.asyncio.client import connect

    return connect(url, max_size=2**22)


class ZeroTwoOneAdapter(BrokerAdapter):
    def __init__(
        self,
        *,
        username: str,
        password: str,
        base_url: str = DEFAULT_BASE_URL,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        http: httpx.AsyncClient | None = None,
        connect: Callable[[str], Any] = _default_connect,
        cache_dir: str | os.PathLike | None = ".cache",
        starting_funds: int = paise(1_000_000),  # 021 tops every account up to Rs 10,00,000 each trading day
        read_retries: int = 2,
        retry_delay: float = 0.5,
        price_wait: float = 3.0,
    ):
        self._username, self._password = username, password
        self._base_url = base_url.rstrip("/")
        self._clock = clock
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0))
        self._owns_http = http is None
        self._connect = connect
        self._cache_dir = Path(cache_dir) if cache_dir else None
        self._starting_funds = starting_funds
        self._read_retries, self._retry_delay, self._price_wait = read_retries, retry_delay, price_wait

        self.locks = AccountLocks()  # simulated: 021's API exposes no Anchor / Co-Captain state
        self.master = InstrumentMaster([], [], {})
        self._token: str | None = None
        self.session_taken = False  # 021 revoked our token and logging in again did not help: see needs_reconnect
        self._login_lock = asyncio.Lock()
        self._feed = self._make_feed(mode="full", filters=None, publish=True, resolve=self._resolve_cash)
        self._chain_feed: MarketFeed | None = None
        # live notice of our own order events; the app reads REST when woken (orders_feed.py)
        self.order_wake = OrderWake()
        self.orders_feed = OrdersFeed(url=f"{websocket_base(self._base_url)}/orders", connect=connect,
                                      get_key=self._ephemeral_key, ucc=username, on_change=self.order_wake.notify)
        self._chain_keys: dict[tuple[int, int], str] = {}
        self._sent: dict[str, tuple[Side, int]] = {}  # order id -> what we sent (for rows that lose their size)
        self._fills: dict[str, tuple[int, list[dict]]] = {}  # order id -> (traded quantity, its trades)

    # ------------------------------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------------------------------ #

    def _make_feed(self, *, mode: str, filters: str | None, publish: bool, resolve) -> MarketFeed:
        return MarketFeed(
            url=f"{websocket_base(self._base_url)}/market",
            connect=self._connect,
            get_key=self._ephemeral_key,
            resolve=resolve,
            clock=self._clock,
            mode=mode,
            filters=filters,
            publish_ticks=publish,
        )

    def _resolve_cash(self, exchange_code: int, token: int) -> str | None:
        name = next((n for n, c in WS_CODE.items() if c == exchange_code), None)
        listing = self.master.by_token(name, token) if name else None
        return listing.instrument.key if listing else None

    async def start(self) -> None:
        await self._login()
        await self._load_master()
        self._feed.start()
        self.orders_feed.start()
        # Follow what the trader holds from the start, so the first account read already has prices.
        with contextlib.suppress(BrokerError):
            await self._watch_account()

    async def close(self) -> None:
        for feed in (self._feed, self._chain_feed, self.orders_feed):
            if feed is not None:
                await feed.stop()
        if self._owns_http:
            await self._http.aclose()

    # ------------------------------------------------------------------------------------------ #
    # HTTP
    # ------------------------------------------------------------------------------------------ #

    async def _login(self) -> None:
        async with self._login_lock:
            try:
                r = await self._http.post(
                    f"{self._base_url}/auth/token", json={"username": self._username, "password": self._password}
                )
            except httpx.HTTPError as exc:
                raise BrokerTimeout(f"could not reach 021 to log in ({type(exc).__name__})") from exc
            body = _json(r)
            if r.status_code == 401:
                raise BrokerAuthFailed("021 refused the login: check the UCC and password in .env")
            token = ((body or {}).get("data") or {}).get("accessToken") if isinstance(body, dict) else None
            if r.status_code != 200 or not token:
                raise BrokerTimeout(f"021 login failed (HTTP {r.status_code})")
            self._token = token
            self.session_taken = False

    @property
    def needs_reconnect(self) -> bool:
        return self.session_taken

    async def relogin(self) -> None:
        """Log in again (the Reconnect button). Raises BrokerAuthFailed if 021 refuses the saved login."""
        await self._login()

    async def _ephemeral_key(self) -> str:
        data = await self._read("GET", "/websocket/ephemeral-key")
        key = (data or {}).get("token") if isinstance(data, dict) else None
        if not key:
            raise BrokerTimeout("021 gave no websocket key")
        return key

    async def _send(self, method: str, path: str, json: Any = None) -> httpx.Response:
        """One request. A 401 for an expired token logs in again and tries ONCE more: a 401 means the request
        was refused before it was processed, so this is safe even for an order."""
        for attempt in (0, 1):
            if self._token is None:
                await self._login()
            try:
                r = await self._http.request(method, f"{self._base_url}{path}", json=json,
                                             headers={"Authorization": f"Bearer {self._token}"})
            except httpx.HTTPError as exc:
                raise BrokerTimeout(f"{method} {path}: {type(exc).__name__}") from exc
            if r.status_code == 401 and attempt == 0 and "token" in _error_text(r).lower():
                self._token = None
                continue
            return r
        return r  # pragma: no cover

    async def _read(self, method: str, path: str, *, allow_404: bool = False) -> Any:
        """A read: retried on rate limits, server errors and timeouts. Returns the unwrapped data."""
        last: Exception = BrokerTimeout(f"{method} {path}: no response")
        for attempt in range(self._read_retries + 1):
            if attempt:
                await asyncio.sleep(self._retry_delay * attempt)
            try:
                r = await self._send(method, path)
            except BrokerTimeout as exc:
                last = exc
                continue
            if r.status_code == 404 and allow_404:
                return None
            if r.status_code == 401:  # we already logged in again once inside _send, so someone else holds the account
                self.session_taken = True
                raise BrokerTimeout(SESSION_TAKEN)
            if r.status_code in (429, 500, 502, 503, 504):
                last = BrokerTimeout(f"{method} {path}: HTTP {r.status_code}")
                continue
            if r.status_code != 200:
                raise BrokerTimeout(f"{method} {path}: HTTP {r.status_code} {_error_text(r)[:120]}")
            try:
                return codec.unwrap(_json(r))
            except codec.MalformedResponse as exc:
                raise BrokerTimeout(str(exc)) from exc
        raise last

    async def _write(self, method: str, path: str, body: dict) -> dict:
        """An order write. Never retried. Returns the response's data on success; otherwise raises
        BrokerRejected (the broker clearly said no) or BrokerTimeout (we cannot tell)."""
        r = await self._send(method, path, body)  # transport trouble is BrokerTimeout
        payload = _json(r)
        text = _error_text(r)
        if r.status_code == 401:  # refused before it was processed, even after a fresh login: someone else holds the account
            self.session_taken = True
            raise BrokerRejected(RejectionReason.OTHER, SESSION_TAKEN)
        if r.status_code != 200:
            raise classify_order_failure(r.status_code, text)
        if not isinstance(payload, dict) or "success" not in payload:
            raise BrokerTimeout("021 answered an order in a form we could not read")
        if payload["success"] is not True:
            raise BrokerRejected(reason_from_text(text) or RejectionReason.OTHER, text or "rejected")
        return payload.get("data") or {}

    # ------------------------------------------------------------------------------------------ #
    # instruments
    # ------------------------------------------------------------------------------------------ #

    async def _load_master(self) -> None:
        today = self._clock().astimezone(IST).date()
        cached = self._cache_dir / f"instruments-{today}.csv" if self._cache_dir else None
        text: str | None = None
        if cached is not None and cached.exists():
            text = cached.read_text(encoding="utf-8")
        if text is None:
            r = await self._send("GET", "/instruments")
            if r.status_code != 200:
                raise BrokerTimeout(f"could not download the instrument list (HTTP {r.status_code})")
            raw = r.content
            if raw[:2] == b"\x1f\x8b":  # still gzipped if the server didn't mark it as encoded
                raw = gzip.decompress(raw)
            text = raw.decode("utf-8", errors="replace")
            if cached is not None:
                cached.parent.mkdir(parents=True, exist_ok=True)
                cached.write_text(text, encoding="utf-8")
        self.master = await asyncio.to_thread(InstrumentMaster.from_csv, text, today)
        log.info("instrument list loaded: %d tradable instruments", len(self.master))

    def _listing(self, instrument_key: str) -> Listing:
        listing = self.master.get(instrument_key)
        if listing is None:
            raise KeyError(instrument_key)
        return listing

    async def get_instrument(self, instrument_key: str) -> Instrument | None:
        listing = self.master.get(instrument_key)
        return listing.instrument if listing else None

    async def search_instruments(self, query: str, limit: int = 10) -> list[Instrument]:
        return self.master.search(query, limit)

    async def find_option(self, underlying: str, strike: int, option_type: OptionType, expiry: date | None = None) -> Instrument | None:
        """From 021's instrument file: the contract's real lot size, tick and token (NSE F&O preferred)."""
        listing = self.master.find_option(underlying, strike, option_type, expiry, self._clock().date())
        return listing.instrument if listing else None

    async def find_future(self, underlying: str, expiry: date | None = None) -> Instrument | None:
        """From 021's instrument file: the contract's real lot size, tick and token (NSE F&O)."""
        listing = self.master.find_future(underlying, expiry, self._clock().date())
        return listing.instrument if listing else None

    # ------------------------------------------------------------------------------------------ #
    # prices
    # ------------------------------------------------------------------------------------------ #

    async def watch(self, instrument_keys: Sequence[str]) -> None:
        pairs = [listing.ws for k in instrument_keys if (listing := self.master.get(k))]
        if pairs:
            await self._feed.watch(pairs, pin=True)

    async def _watch_account(self) -> None:
        keys = [h.instrument.key for h in await self.get_holdings()] + [p.instrument.key for p in await self.get_positions()]
        await self.watch(keys)

    async def get_quote(self, instrument_key: str) -> Quote:
        listing = self._listing(instrument_key)
        await self._feed.watch([listing.ws])
        snap = await self._feed.wait_for(instrument_key, self._price_wait)
        if snap is None or not snap.ltp:
            raise BrokerTimeout(f"no price for {instrument_key} yet")
        return Quote(
            instrument_key=instrument_key,
            ltp=snap.ltp,
            prev_close=snap.prev_close or snap.ltp,
            bid=snap.bid or None,
            ask=snap.ask or None,
            day_high=snap.high or None,
            day_low=snap.low or None,
            ts=self._clock(),
        )

    async def _prices(self, listings: Sequence[Listing], fallback: dict[str, int]) -> dict[str, int]:
        """Last prices for these listings from the feed; where none arrives, the given fallback (and a log line)."""
        await self._feed.watch([x.ws for x in listings])
        out: dict[str, int] = {}
        snaps = await asyncio.gather(*(self._feed.wait_for(x.instrument.key, self._price_wait) for x in listings))
        for listing, snap in zip(listings, snaps):
            key = listing.instrument.key
            if snap is not None and snap.ltp:
                out[key] = snap.ltp
            else:
                log.warning("no live price for %s; using the previous close", key)
                out[key] = fallback[key]
        return out

    async def subscribe_ticks(self, instrument_keys: Sequence[str]) -> AsyncIterator[Tick]:
        if instrument_keys:
            await self.watch(instrument_keys)
        async for tick in self._feed.ticks(instrument_keys):
            yield tick

    # ------------------------------------------------------------------------------------------ #
    # account
    # ------------------------------------------------------------------------------------------ #

    async def get_holdings(self) -> list[Holding]:
        rows = await self._read("GET", "/portfolio/holdings")
        wanted: list[tuple[dict, Listing, int]] = []
        for row in rows or []:
            qty = int(row.get("freeQty") or 0) + int(row.get("btstQty") or 0)
            listing = self.master.by_isin(row.get("isin", "")) or self.master.by_symbol(row.get("symbol", ""))
            if qty > 0 and listing is not None and int(row.get("price") or 0) > 0:
                wanted.append((row, listing, qty))
        prices = await self._prices([w[1] for w in wanted], {w[1].instrument.key: int(w[0].get("prevClose") or w[0]["price"]) for w in wanted})
        return [
            Holding(
                instrument=listing.instrument,
                quantity=qty,
                avg_price=int(row["price"]),
                ltp=prices[listing.instrument.key],
                prev_close=int(row.get("prevClose") or 0) or prices[listing.instrument.key],
            )
            for row, listing, qty in wanted
        ]

    async def get_positions(self) -> list[Position]:
        rows = await self._read("GET", "/portfolio/positions")
        wanted: list[tuple[dict, Listing]] = []
        for row in rows or []:
            listing = self.master.by_token(row.get("exchange", ""), int(row.get("token") or 0))
            product = codec.PRODUCT_FROM_WIRE.get(row.get("product"))
            if listing is not None and product is not None and int(row.get("netQuantity") or 0) != 0:
                wanted.append((row, listing))
        prices = await self._prices(
            [w[1] for w in wanted], {w[1].instrument.key: int(w[0].get("prevClose") or w[0].get("netPrice") or 1) for w in wanted}
        )
        return [
            Position(
                instrument=listing.instrument,
                quantity=int(row["netQuantity"]),
                avg_price=int(row.get("netPrice") or 0) or prices[listing.instrument.key],
                ltp=prices[listing.instrument.key],
                prev_close=int(row.get("prevClose") or 0) or prices[listing.instrument.key],
                product=codec.PRODUCT_FROM_WIRE[row["product"]],
            )
            for row, listing in wanted
        ]

    async def get_funds(self) -> Funds:
        """An estimate: 021 has no funds endpoint. See the module note."""
        trades, orders, positions = await asyncio.gather(
            self._read("GET", "/trades"), self._orders(with_fills=False), self._read("GET", "/portfolio/positions")
        )
        spent = sum(int(t["quantity"]) * int(t["price"]) for t in trades or [])  # buys positive, sells negative
        # A future is not paid for in full: an open one moves no cash until it is closed (021 reports no margin
        # figure). Take the open part's value back out, so closed futures count as their gain or loss only.
        for row in positions or []:
            listing = self.master.by_token(row.get("exchange", ""), int(row.get("token") or 0))
            if listing is not None and listing.instrument.is_future:
                spent -= int(row.get("netQuantity") or 0) * int(row.get("netPrice") or 0)
        tied_up = 0
        for o in orders:
            if o.side is Side.BUY and o.status in LIVE and not o.instrument.is_future:
                tied_up += o.pending_quantity * (o.limit_price or 0)
        return Funds(available_cash=max(self._starting_funds - spent - tied_up, 0), used_margin=tied_up)

    async def get_account_locks(self) -> AccountLocks:
        return self.locks

    # ------------------------------------------------------------------------------------------ #
    # orders
    # ------------------------------------------------------------------------------------------ #

    async def _trades_of(self, order_id: str, traded: int) -> list[dict]:
        cached = self._fills.get(order_id)
        if cached is not None and cached[0] == traded:
            return cached[1]
        trades = await self._read("GET", f"/orders/{order_id}/trades")
        trades = list(trades or [])
        self._fills[order_id] = (traded, trades)
        return trades

    async def _build_order(self, row: dict, *, with_fills: bool) -> Order | None:
        listing = self.master.by_token(row.get("exchange", ""), int(row.get("token") or 0))
        if listing is None:
            return None  # not something this app trades (F&O etc.)
        order_id = str(row["orderId"])
        traded = abs(int(row.get("qtyTraded") or 0))
        trades = await self._trades_of(order_id, traded) if traded and (with_fills or not int(row.get("qtyRemaining") or 0)) else None
        order = codec.order_from_row(row, listing, trades=trades, fallback=self._sent.get(order_id))
        if order is None:
            log.warning("order %s skipped: its side or size cannot be told from the row", order_id)
        return order

    async def _orders(self, *, with_fills: bool) -> list[Order]:
        rows = await self._read("GET", "/orders")
        built = await asyncio.gather(*(self._build_order(r, with_fills=with_fills) for r in rows or []))
        return [o for o in built if o is not None]

    async def get_orders(self) -> list[Order]:
        return await self._orders(with_fills=True)

    async def get_order(self, order_id: str) -> Order | None:
        rows = await self._read("GET", f"/orders/{order_id}", allow_404=True)
        if not rows:
            return None
        return await self._build_order(rows[0], with_fills=True)

    async def find_sent_order(self, spec: SentOrderSpec, exclude_order_ids: Collection[str] = ()) -> MatchResult:
        result = match_sent_order(spec, await self._orders(with_fills=False), exclude_order_ids)
        if result.order is not None:  # the match is decided; now fetch its fills for the report
            full = await self.get_order(result.order.order_id)
            return MatchResult(kind=result.kind, order=full or result.order)
        return result

    async def place_order(self, order: PendingOrder) -> Order:
        self.require_approved(order)
        if order.action is not OrderAction.PLACE:
            raise ValueError("place_order needs a PLACE order")
        listing = self.master.get(order.instrument.key)
        if listing is None:
            raise BrokerRejected(RejectionReason.OTHER, "that instrument is not in 021's instrument list")
        data = await self._write("POST", "/orders", codec.place_body(order, listing))
        order_id = str(data.get("orderId") or "")
        if not order_id:
            raise BrokerTimeout("021 accepted the order but gave no order id")
        self._sent[order_id] = (order.side, order.quantity)
        try:
            placed = await self.get_order(order_id)
        except BrokerError:
            placed = None
        if placed is None:  # the order is in; we just cannot read it back yet. Say only what we know.
            now = self._clock()
            return Order(
                order_id=order_id, instrument=order.instrument, side=order.side, quantity=order.quantity,
                order_type=OrderType.STOP_LIMIT if order.order_type is OrderType.STOP_LIMIT else OrderType.LIMIT,
                limit_price=order.limit_price or order.protection_price, trigger_price=order.trigger_price,
                product=order.product, validity=order.validity, status=OrderStatus.PENDING, created_at=now, updated_at=now,
            )
        if placed.status is OrderStatus.REJECTED:
            raise BrokerRejected(placed.rejection_reason or RejectionReason.OTHER, placed.rejection_message or "", placed)
        return placed

    async def modify_order(self, order: PendingOrder) -> Order:
        self.require_approved(order)
        if order.action is not OrderAction.MODIFY:
            raise ValueError("modify_order needs a MODIFY order")
        current = await self.get_order(order.target_order_id)
        if current is None:
            raise BrokerRejected(RejectionReason.OTHER, "order not found")
        if current.status not in LIVE:
            raise BrokerRejected(RejectionReason.OTHER, f"order is already {current.status.value}", current)
        listing = self._listing(current.instrument.key)
        await self._write("PUT", f"/orders/{current.order_id}", codec.modify_body(current, listing, order))
        with contextlib.suppress(BrokerError):
            fresh = await self.get_order(current.order_id)
            if fresh is not None:
                return fresh
        return current  # accepted, but not readable yet: the executor checks the book itself

    async def cancel_order(self, order: PendingOrder) -> Order:
        self.require_approved(order)
        if order.action is not OrderAction.CANCEL:
            raise ValueError("cancel_order needs a CANCEL order")
        current = await self.get_order(order.target_order_id)
        if current is None:
            raise BrokerRejected(RejectionReason.OTHER, "order not found")
        if current.status not in LIVE:
            raise BrokerRejected(RejectionReason.OTHER, f"order is already {current.status.value}", current)
        listing = self._listing(current.instrument.key)
        await self._write("DELETE", f"/orders/{current.order_id}", codec.cancel_body(current, listing))
        # Cancelling is asynchronous (SentForCancellation, then Cancelled). Look a few times; report what is true.
        latest = current
        for _ in range(4):
            await asyncio.sleep(self._retry_delay / 2)
            try:
                fresh = await self.get_order(current.order_id)
            except BrokerError:
                break
            latest = fresh or latest
            if latest.status is OrderStatus.CANCELLED:
                break
        return latest

    # ------------------------------------------------------------------------------------------ #
    # options (read-only)
    # ------------------------------------------------------------------------------------------ #

    async def get_option_expiries(self, underlying: str) -> list[date]:
        today = self._clock().astimezone(IST).date()
        return await asyncio.to_thread(self.master.option_expiries, underlying, today)

    async def get_option_chain(self, underlying: str, expiry: date, window: int = 5) -> OptionChain:
        under = self.master.index(underlying)
        if under is None:
            raise ValueError(f"no such underlying {underlying}")
        contracts = await asyncio.to_thread(self.master.option_listings, underlying.upper(), expiry)
        if not contracts:
            raise ValueError(f"no option chain for {underlying} {expiry}")
        spot = (await self.get_quote(under.instrument.key)).ltp
        strikes = sorted({c.instrument.strike for c in contracts})
        nearest = min(range(len(strikes)), key=lambda i: abs(strikes[i] - spot))
        chosen = set(strikes[max(nearest - window, 0) : nearest + window + 1])
        picked = [c for c in contracts if c.instrument.strike in chosen]

        feed = self._chain_feed
        if feed is None:
            # Prices only ("l"). The sandbox fills open interest and volume with random numbers, so we do not ask.
            feed = self._chain_feed = self._make_feed(mode="oc", filters="l", publish=False, resolve=self._resolve_chain)
            feed.start()
        self._chain_keys = {c.ws: c.instrument.key for c in picked}
        await feed.watch(list(self._chain_keys))
        snaps = await asyncio.gather(*(feed.wait_for(c.instrument.key, self._price_wait) for c in picked))
        by_key = {c.instrument.key: s for c, s in zip(picked, snaps)}
        rows = []
        for strike in sorted(chosen):
            legs: dict[OptionType, OptionQuote | None] = {OptionType.CE: None, OptionType.PE: None}
            for c in picked:
                snap = by_key[c.instrument.key]
                if c.instrument.strike == strike and snap is not None and snap.ltp is not None:
                    legs[c.instrument.option_type] = OptionQuote(instrument_key=c.instrument.key, ltp=snap.ltp)
            rows.append(OptionChainRow(strike=strike, call=legs[OptionType.CE], put=legs[OptionType.PE]))
        if all(r.call is None and r.put is None for r in rows):
            raise BrokerTimeout("no option prices arrived")
        return OptionChain(underlying=underlying.upper(), spot=spot, expiry=expiry, rows=rows)

    def _resolve_chain(self, exchange_code: int, token: int) -> str | None:
        return self._chain_keys.get((exchange_code, token))


def _json(r: httpx.Response) -> Any:
    try:
        return r.json()
    except ValueError:
        return None


def _error_text(r: httpx.Response) -> str:
    body = _json(r)
    if isinstance(body, dict) and body.get("error"):
        return str(body["error"])
    return ""
