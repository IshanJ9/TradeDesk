"""A connection to 021's market-data socket that stays up.

- Needs an ephemeral key per connection (valid 24 h, checked only when connecting); a bad or expired key
  fails the handshake, so every reconnect asks for a fresh one.
- Subscriptions are not remembered by the server, so the full wanted set is re-sent after every reconnect
  (021 restarts all sockets at 08:00 IST every day).
- At most 100 instruments per connection. Past the limit the oldest, unpinned ones are dropped.
- 021 sends a snapshot right after subscribing, then only changed instruments, at most every 300 ms. A price
  that comes and goes inside one interval is never seen; that is the feed's limit, not ours.
- `seq` on a tick is ours (the socket has none): one number per instrument that goes up with every packet.

The websocket itself is injected (`connect`), so tests drive this with fake frames.
"""

import asyncio
import contextlib
import logging
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from app.broker.zerotwoone.packets import ChainPacket, FullPacket, LtpPacket, decode_frame, subscribe_message
from app.schemas import Tick

log = logging.getLogger("tradedesk.feed")

Connect = Callable[[str], Any]  # url -> async context manager yielding a websocket-like object
MAX_INSTRUMENTS = 100


@dataclass(frozen=True)
class Snapshot:
    ltp: int | None = None
    prev_close: int | None = None
    open: int | None = None
    high: int | None = None
    low: int | None = None
    bid: int | None = None
    ask: int | None = None
    oi: int | None = None
    volume: int | None = None


class MarketFeed:
    def __init__(
        self,
        *,
        url: str,
        connect: Connect,
        get_key: Callable[[], Awaitable[str]],
        resolve: Callable[[int, int], str | None],
        clock: Callable[[], datetime],
        mode: str = "full",
        filters: str | None = None,
        publish_ticks: bool = True,
        max_instruments: int = MAX_INSTRUMENTS,
        backoff: Sequence[float] = (1, 2, 5, 10, 30),
    ):
        self._url, self._connect, self._get_key = url, connect, get_key
        self._resolve, self._clock = resolve, clock
        self._mode, self._filters, self._publish_ticks = mode, filters, publish_ticks
        self._max = max_instruments
        self._backoff = tuple(backoff)
        self._wanted: OrderedDict[tuple[int, int], bool] = OrderedDict()  # (exchange, token) -> pinned
        self._ws: Any = None
        self._snaps: dict[str, Snapshot] = {}
        self._seq: dict[str, int] = {}
        self._events: dict[str, asyncio.Event] = {}
        self._subs: list[tuple[frozenset[str] | None, asyncio.Queue[Tick]]] = []
        self._task: asyncio.Task | None = None
        self._connected = asyncio.Event()
        self.unparsed_bytes = 0
        self.connections = 0

    # ---- what we watch ------------------------------------------------------------------------ #

    async def watch(self, pairs: Sequence[tuple[int, int]], *, pin: bool = False) -> None:
        """Start watching these (exchange code, token) pairs. Safe to call again with the same ones."""
        new = [p for p in pairs if p not in self._wanted]
        for p in pairs:
            self._wanted[p] = self._wanted.get(p, False) or pin
            self._wanted.move_to_end(p)
        dropped = self._trim()
        if self._ws is not None:
            if dropped:
                await self._send("unsubscribe", dropped)
            if new:
                await self._send("subscribe", [p for p in new if p in self._wanted])

    def _trim(self) -> list[tuple[int, int]]:
        dropped = []
        while len(self._wanted) > self._max:
            victim = next((p for p, pinned in self._wanted.items() if not pinned), None)
            if victim is None:
                break
            del self._wanted[victim]
            dropped.append(victim)
        return dropped

    async def unwatch(self, pairs: Sequence[tuple[int, int]]) -> None:
        gone = [p for p in pairs if self._wanted.pop(p, None) is not None]
        if gone and self._ws is not None:
            await self._send("unsubscribe", gone)

    async def _send(self, task: str, pairs: Sequence[tuple[int, int]]) -> None:
        for i in range(0, len(pairs), 50):
            try:
                await self._ws.send(subscribe_message(task, self._mode, pairs[i : i + 50], self._filters))
            except Exception:  # the connection loop notices a dead socket and re-sends everything
                log.warning("could not send %s; will resend after reconnect", task)
                return

    # ---- reading ------------------------------------------------------------------------------ #

    def snapshot(self, instrument_key: str) -> Snapshot | None:
        return self._snaps.get(instrument_key)

    async def wait_for(self, instrument_key: str, timeout: float) -> Snapshot | None:
        """The latest snapshot, waiting up to `timeout` for the first one to arrive."""
        snap = self._snaps.get(instrument_key)
        if snap is not None and snap.ltp is not None:
            return snap
        event = self._events.setdefault(instrument_key, asyncio.Event())
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(event.wait(), timeout)
        return self._snaps.get(instrument_key)

    async def ticks(self, keys: Sequence[str] = ()) -> AsyncIterator[Tick]:
        queue: asyncio.Queue[Tick] = asyncio.Queue(maxsize=10_000)
        sub = (frozenset(keys) if keys else None, queue)
        self._subs.append(sub)
        try:
            while True:
                yield await queue.get()
        finally:
            self._subs.remove(sub)

    def _apply(self, packet: Any) -> None:
        key = self._resolve(packet.exchange, packet.token)
        if key is None:
            return  # an instrument we don't know is dropped, as the guide says 021 does with unknown tokens
        old = self._snaps.get(key, Snapshot())
        if isinstance(packet, FullPacket):
            new = replace(old, ltp=packet.ltp, prev_close=packet.prev_close or old.prev_close, open=packet.open,
                          high=packet.high, low=packet.low, bid=packet.bid, ask=packet.ask)
        elif isinstance(packet, LtpPacket):
            new = replace(old, ltp=packet.ltp, open=packet.open or old.open, prev_close=packet.prev_close or old.prev_close)
        elif isinstance(packet, ChainPacket):
            new = replace(old, ltp=packet.ltp if packet.ltp is not None else old.ltp,
                          oi=packet.oi if packet.oi is not None else old.oi,
                          volume=packet.volume if packet.volume is not None else old.volume)
        else:
            return
        self._snaps[key] = new
        if new.ltp:
            self._events.setdefault(key, asyncio.Event()).set()
        if self._publish_ticks and new.ltp and not isinstance(packet, ChainPacket):
            self._seq[key] = self._seq.get(key, 0) + 1
            tick = Tick(instrument_key=key, ltp=new.ltp, seq=self._seq[key], ts=self._clock())
            for keys, queue in self._subs:
                if (keys is None or key in keys) and not queue.full():
                    queue.put_nowait(tick)

    def handle_frame(self, data: bytes) -> None:
        packets, leftover = decode_frame(data)
        self.unparsed_bytes += leftover
        for packet in packets:
            self._apply(packet)

    # ---- the connection ----------------------------------------------------------------------- #

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name=f"feed-{self._mode}")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def wait_connected(self, timeout: float) -> bool:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._connected.wait(), timeout)
        return self._connected.is_set()

    async def _run(self) -> None:
        failures = 0
        while True:
            try:
                key = await self._get_key()
                async with self._connect(f"{self._url}?token={key}") as ws:
                    self._ws = ws
                    self.connections += 1
                    failures = 0
                    await self._send("subscribe", list(self._wanted))  # nothing is remembered across connections
                    self._connected.set()
                    async for message in ws:
                        if isinstance(message, (bytes, bytearray)):
                            self.handle_frame(bytes(message))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("market socket dropped (%s); reconnecting", type(exc).__name__)
            finally:
                self._ws = None
                self._connected.clear()
            await asyncio.sleep(self._backoff[min(failures, len(self._backoff) - 1)])
            failures += 1
