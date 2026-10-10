"""021's orders socket: live notice that one of OUR orders changed. REST stays the source of truth.

From the 021 API guide (Orders socket):
- Connect to /api/developer/websocket/orders?token=<ephemeral key> and listen; nothing is sent.
- Only your own order events arrive, one packet per binary frame, in order, big-endian.
- Events while disconnected are NOT replayed: after every reconnect, read GET /orders and GET /trades again.
- TC 4 = NSE order event (46 bytes + optional text), TC 8 = BSE order event (32 bytes + optional text).

So this module never decides an order's state from a frame. A frame (or a reconnect) only wakes the code that
reads REST, so a fill, a rejection or an order placed in 021's own app shows up at once instead of at the next
poll. The rejection text in a frame is outside text: it is never logged, stored or shown.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("tradedesk.orders_feed")

STATUS = {
    1: "TRADE", 2: "ACCEPTED", 3: "REJECTED", 4: "SL_TRIGGERED", 5: "MODIFIED", 6: "MODIFY_REJECTED",
    7: "CANCELLED", 8: "CANCEL_REJECTED", 9: "PRICE_CONFIRMATION",
}
NSE_EVENT, BSE_EVENT = 4, 8
NSE_LENGTH, BSE_LENGTH = 46, 32


@dataclass(frozen=True)
class OrderEvent:
    status: str
    ucc: str
    order_id: str
    quantity: int  # negative = sell; for a trade, the quantity filled
    price: int  # paise; for a trade, the fill price (confirm exact values with GET /orders)
    symbol: str | None = None  # NSE only, and blank for NSE F&O: match on order_id


def _int(data: bytes, offset: int, size: int, signed: bool = False) -> int:
    return int.from_bytes(data[offset : offset + size], "big", signed=signed)


def decode_order_event(frame: bytes) -> OrderEvent | None:
    """One frame -> one event, or None for anything that is not a well-formed order event."""
    if len(frame) < 2:
        return None
    tc = _int(frame, 0, 2)
    if tc == NSE_EVENT and len(frame) >= NSE_LENGTH:
        ids = dict(ucc=(4, 10), oid=34, qty=38, price=42)
        symbol = frame[14:34].split(b"\x00", 1)[0].decode("ascii", "replace").strip() or None
    elif tc == BSE_EVENT and len(frame) >= BSE_LENGTH:
        ids = dict(ucc=(4, 10), oid=20, qty=24, price=28)
        symbol = None
    else:
        return None
    status = STATUS.get(_int(frame, 2, 2))
    if status is None:
        return None
    start, size = ids["ucc"]
    return OrderEvent(
        status=status,
        ucc=frame[start : start + size].decode("ascii", "replace").strip(),
        order_id=str(_int(frame, ids["oid"], 4)),
        quantity=_int(frame, ids["qty"], 4, signed=True),
        price=_int(frame, ids["price"], 4, signed=True),
        symbol=symbol,
    )


class OrdersFeed:
    """Keeps the orders socket open and calls `on_change` for every event of ours, and after every reconnect
    (events while it was down are lost, so REST must be read again)."""

    def __init__(
        self,
        *,
        url: str,
        connect: Callable[[str], Any],
        get_key: Callable[[], Awaitable[str]],
        ucc: str,
        on_change: Callable[[OrderEvent | None], None],
        backoff: Sequence[float] = (1, 2, 5, 10, 30),
    ):
        self._url, self._connect, self._get_key = url, connect, get_key
        self._ucc = ucc.strip().upper()
        self._on_change = on_change
        self._backoff = tuple(backoff)
        self._task: asyncio.Task | None = None
        self.connected = False
        self.connections = 0
        self.events = 0
        self.ignored_frames = 0

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="orders-feed")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    def handle_frame(self, frame: bytes) -> None:
        event = decode_order_event(frame)
        if event is None or (self._ucc and event.ucc.upper() != self._ucc):
            self.ignored_frames += 1  # malformed, unknown, or not this account's: never acted on
            return
        self.events += 1
        self._notify(event)

    def _notify(self, event: OrderEvent | None) -> None:
        try:
            self._on_change(event)
        except Exception:
            log.warning("order event handler failed")

    async def _run(self) -> None:
        failures = 0
        while True:
            try:
                key = await self._get_key()  # a fresh key every time: a stale one fails the handshake
                async with self._connect(f"{self._url}?token={key}") as ws:
                    self.connected = True
                    self.connections += 1
                    failures = 0
                    self._notify(None)  # (re)connected: catch up on anything missed, from REST
                    async for message in ws:
                        if isinstance(message, (bytes, bytearray)):
                            self.handle_frame(bytes(message))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("orders socket dropped (%s); reconnecting", type(exc).__name__)
            finally:
                self.connected = False
            await asyncio.sleep(self._backoff[min(failures, len(self._backoff) - 1)])
            failures += 1
