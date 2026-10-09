"""A stand-in for 021's sandbox, built from the API guide, to drive the real adapter in tests.

It speaks the same envelopes, status codes, units and quirks the guide describes: paise, signed qty, no
client order id, orderId as a string on place and a number in the order book, no partial status,
`qtyRemaining` / `qtyTraded`, trades without an order id, one live token per account, and a gzipped
instrument CSV. Hooks make it misbehave on purpose (timeouts, ambiguous 500s, lost replies).
"""

import gzip
import json
import re
import struct
from datetime import datetime, timezone

import httpx

HEADER = (
    "token,exchange,underlying_token,underlying_exchange,min_lot_size,board_lot_quantity,freeze_quantity,"
    "lower_circuit,upper_circuit,expiry,option_type,strike_price,symbol,instrument_type,isin,ticksize"
)


def expiry_seconds(year: int, month: int, day: int) -> int:
    """Expiries in the real file are plain Unix seconds."""
    return int(datetime(year, month, day, 9, 0, tzinfo=timezone.utc).timestamp())


def csv_text() -> str:
    exp1, exp2 = expiry_seconds(2026, 10, 13), expiry_seconds(2026, 10, 20)
    rows = [
        "1594,NSECM,1594,NSECM,0,1,984600,115200,172800,0,,0,INFY,STK,INE009A01021,5",
        "11536,NSECM,11536,NSECM,0,1,500000,311200,466800,0,,0,TCS,STK,INE467B01029,5",
        "2885,NSECM,2885,NSECM,0,1,900000,112000,168000,0,,0,RELIANCE,STK,INE002A01018,5",
        "1330,NSECM,1330,NSECM,0,1,900000,132000,198000,0,,0,HDFCBANK,STK,INE040A01034,5",
        "3456,NSECM,3456,NSECM,0,1,900000,7000,9000,0,,0,TATAMOTORS,STK,INE155A01022,5",
        "3499,NSECM,3499,NSECM,0,1,900000,12000,18000,0,,0,TATASTEEL,STK,INE081A01020,5",
        "22699,NSECM,22699,NSECM,0,100,984600,9649,10663,0,,0,747HR36,STK,IN1620230376,1",
        "500209,BSEEQ,500209,BSEEQ,0,1,500000,115200,172800,0,,0,INFY,STK,INE009A01021,5",
        '26000,NSEIDX,26000,NSEIDX,0,0,0,0,0,0,"",0,"",IDX,            ,0',  # the real file leaves index names empty
    ]
    token = 70000
    for exp in (exp1, exp2):
        for strike in (24400, 24450, 24500, 24550, 24600):
            for kind in ("CALL", "PUT"):
                token += 1
                rows.append(f"{token},NSEFO,26000,NSEIDX,0,75,1800,5,500000,{exp},{kind},{strike * 100},NIFTY,OPTIDX,,5")
    return HEADER + "\n" + "\n".join(rows) + "\n"


class Fake021:
    """Use `Fake021().client()` as the adapter's httpx client."""

    def __init__(self, *, ucc="HACK1234", password="pw"):
        self.ucc, self.password = ucc, password
        self.token: str | None = None
        self.logins = 0
        self.orders: dict[int, dict] = {}
        self.trades: dict[int, list[dict]] = {}
        self.holdings: list[dict] = []
        self.positions: list[dict] = []
        self.requests: list[tuple[str, str, dict | None]] = []
        self._next_id = 1042
        self.now = int(datetime(2026, 10, 9, 10, 0, tzinfo=timezone.utc).timestamp())
        self.auto_fill = False  # True: a placed order fills at once at its price
        self.reject_on_place: str | None = None  # the exchange refuses the order right after the broker accepts it
        self.cancel_settles_after: int | None = None  # a cancel finishes after this many reads of the order
        self.auto_settle_cancel = False  # True: a cancel finishes at once instead of lingering as SentForCancellation
        self.last_price = {2885: 140000}
        self.csv = csv_text()
        self.lock_out = False  # another copy of the app keeps logging in, so every token we get is revoked at once
        self.fail_next: list[dict] = []  # {"match": "POST /orders", "status": 500, "error": "...", "apply": bool, "raise": bool}

    # ---- plumbing ---------------------------------------------------------------------------- #

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self._handle), timeout=5)

    def _ok(self, data, status=200):
        return httpx.Response(status, json={"data": data, "success": True, "error": None})

    def _err(self, text, status):
        return httpx.Response(status, json={"data": None, "success": False, "error": text})

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/developer-api/v1")
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, path, body))
        if path == "/auth/token":
            if body != {"username": self.ucc, "password": self.password}:
                return self._err("Invalid username or password.", 401)
            self.logins += 1
            self.token = f"tok-{self.logins}"
            return self._ok({"accessToken": self.token, "tokenType": "Bearer", "expiresAt": "2026-10-10T05:00:00+05:30", "expiresIn": 39600})
        if self.lock_out or request.headers.get("authorization") != f"Bearer {self.token}":
            return self._err("Invalid or expired access token.", 401)

        key = f"{request.method} {path}"
        for hook in list(self.fail_next):
            if key.startswith(hook["match"]):
                self.fail_next.remove(hook)
                if hook.get("apply"):
                    self._route(request.method, path, body)  # the broker did it, then the reply went wrong
                if hook.get("raise"):
                    raise httpx.ReadTimeout("timed out", request=request)
                return self._err(hook.get("error", ""), hook["status"])
        return self._route(request.method, path, body)

    def _route(self, method: str, path: str, body):
        if method == "GET" and path == "/instruments":
            return httpx.Response(200, content=gzip.compress(self.csv.encode()), headers={"content-type": "text/csv", "content-encoding": "gzip"})
        if method == "GET" and path == "/websocket/ephemeral-key":
            return self._ok({"token": "eph-key"})
        if method == "GET" and path == "/portfolio/holdings":
            return httpx.Response(200, json=self.holdings)
        if method == "GET" and path == "/portfolio/positions":
            return httpx.Response(200, json=self.positions)
        if method == "GET" and path == "/trades":
            return httpx.Response(200, json=[t for ts in self.trades.values() for t in ts])
        if method == "GET" and path == "/orders":
            return httpx.Response(200, json=sorted(self.orders.values(), key=lambda o: -o["orderId"]))
        if method == "POST" and path == "/orders":
            return self._place(body)
        if m := re.fullmatch(r"/orders/(\d+)/trades", path):
            return httpx.Response(200, json=self.trades.get(int(m[1]), []))
        if m := re.fullmatch(r"/orders/(\d+)", path):
            oid = int(m[1])
            if method == "GET":
                if oid in self.orders and self.orders[oid]["status"] == "SentForCancellation" and self.cancel_settles_after is not None:
                    self.cancel_settles_after -= 1
                    if self.cancel_settles_after <= 0:
                        self.orders[oid]["status"] = "Cancelled"
                return httpx.Response(200, json=[self.orders[oid]]) if oid in self.orders else self._err("not found", 404)
            if method == "PUT":
                return self._modify(oid, body)
            if method == "DELETE":
                return self._cancel(oid)
        return self._err(f"no route {method} {path}", 404)

    # ---- the order book ---------------------------------------------------------------------- #

    def _place(self, b):
        required = {"exchange", "token", "qty", "price", "book", "product", "validity"}
        if not required <= set(b) or b["book"] not in ("RL", "SL") or b["validity"] not in ("Day", "IOC"):
            return self._err("Bad request body", 400)
        if b["product"] == "CNC" and b["exchange"] not in ("NSE", "BSE"):
            return self._err("CNC is only valid for equity instruments", 400)
        if b["book"] == "SL" and not b.get("trigger"):
            return self._err("Trigger price required for SL orders", 400)
        oid = self._next_id
        self._next_id += 1
        ex = {"NSE": "NSECM", "BSE": "BSEEQ", "NSEFO": "NSEFO", "BSEFO": "BSEEQD"}[b["exchange"]]
        qty = b["qty"]
        row = {
            "orderId": oid, "time": self.now, "lastActivity": self.now, "token": b["token"], "exchange": ex,
            "product": b["product"], "qtyRemaining": qty, "qtyTraded": 0, "discQty": 0, "price": b["price"],
            "triggerPrice": b.get("trigger", 0), "book": b["book"], "validity": b["validity"], "status": "Pending",
            "reason": "", "placedBy": "User", "amo": False, "exchangeOrderNumber": f"11000000{oid}",
        }
        self.orders[oid] = row
        self.trades[oid] = []
        if self.reject_on_place:
            self.reject(oid, self.reject_on_place)
        elif self.auto_fill:
            self.fill(oid)
        return self._ok({"type": "SUCCESS", "message": "Order placed successfully", "orderId": str(oid)})

    def fill(self, oid: int, quantity: int | None = None, price: int | None = None) -> None:
        row = self.orders[oid]
        remaining = abs(row["qtyRemaining"])
        take = quantity or remaining
        sign = 1 if row["qtyRemaining"] > 0 else -1
        row["qtyTraded"] = abs(row["qtyTraded"]) + take
        row["qtyRemaining"] = sign * (remaining - take)
        row["status"] = "Executed" if remaining - take == 0 else "Pending"
        self.trades[oid].append({
            "tradeId": 5000 + len(self.trades[oid]) + oid, "tradeTime": self.now, "quantity": sign * take,
            "token": row["token"], "exchange": row["exchange"], "product": row["product"],
            "price": price or row["price"], "symbol": "X",
        })

    def reject(self, oid: int, reason: str) -> None:
        self.orders[oid]["status"] = "Rejected"
        self.orders[oid]["reason"] = reason

    def _modify(self, oid, b):
        row = self.orders.get(oid)
        if row is None:
            return self._err("not found", 404)
        if row["status"] not in ("Pending", "Placed"):
            return self._err("Only pending orders can be modified", 500)
        row["price"], row["triggerPrice"] = b["price"], b.get("trigger", 0)
        row["qtyRemaining"] = b["qty"] - (abs(row["qtyTraded"]) * (1 if b["qty"] > 0 else -1))
        return self._ok({"type": "SUCCESS", "message": "Order modified"})

    def _cancel(self, oid):
        row = self.orders.get(oid)
        if row is None:
            return self._err("not found", 404)
        row["status"] = "Cancelled" if self.auto_settle_cancel else "SentForCancellation"
        self.pending_cancel = oid
        return self._ok({"type": "SUCCESS", "message": "Cancel request sent"})

    def settle_cancel(self) -> None:
        self.orders[self.pending_cancel]["status"] = "Cancelled"


# ---- websocket frames -------------------------------------------------------------------------- #


def ltp_packet(exchange: int, token: int, ltp: int) -> bytes:
    return struct.pack(">HHII", 1, exchange, token, ltp)


def full_nse_cash(token: int, ltp: int, close: int, open_: int, high: int, low: int, bid: int = 0, ask: int = 0) -> bytes:
    buf = bytearray(220)
    struct.pack_into(">HHII", buf, 0, 3, 1, token, ltp)
    struct.pack_into(">IIII", buf, 44, close, open_, high, low)
    struct.pack_into(">QIH", buf, 64, 100, bid, 1)  # best bid level
    struct.pack_into(">QIH", buf, 64 + 5 * 14, 100, ask, 1)  # best ask level
    return bytes(buf)


def full_index(token: int, value: int, close: int, open_: int, high: int, low: int) -> bytes:
    buf = bytearray(36)
    struct.pack_into(">HHI", buf, 0, 3, 3, token)
    struct.pack_into(">I", buf, 8, value)
    struct.pack_into(">IIII", buf, 12, close, open_, high, low)
    return bytes(buf)


def chain_packet(token: int, ltp: int, oi: int | None = None, volume: int | None = None) -> bytes:
    """An option-chain packet. With only a price it is what we ask for; OI and volume are optional extras."""
    fields = b"\x01" + struct.pack(">I", ltp)
    if oi is not None:
        fields += b"\x02" + struct.pack(">Q", oi)
    if volume is not None:
        fields += b"\x03" + struct.pack(">Q", volume)
    count = 1 + (oi is not None) + (volume is not None)
    return struct.pack(">HIHB", 9, token, 2, count) + fields


HEARTBEAT = b"\x00\x0a"


class FakeSocket:
    """A websocket-like object: async-iterates frames put into it, records what the client sends."""

    def __init__(self):
        import asyncio

        self.sent: list[str] = []
        self.queue: asyncio.Queue = asyncio.Queue()
        self.closed = False

    async def send(self, message: str) -> None:
        self.sent.append(message)

    def push(self, frame: bytes) -> None:
        self.queue.put_nowait(frame)

    def close(self) -> None:
        self.queue.put_nowait(None)

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self.queue.get()
        if item is None:
            raise StopAsyncIteration
        return item


class FakeConnector:
    """`connect(url)` for the feed: hands out FakeSockets, remembers urls."""

    def __init__(self):
        self.urls: list[str] = []
        self.sockets: list[FakeSocket] = []

    def __call__(self, url: str):
        connector = self

        class _Ctx:
            async def __aenter__(self_inner):
                sock = FakeSocket()
                connector.urls.append(url)
                connector.sockets.append(sock)
                return sock

            async def __aexit__(self_inner, *exc):
                return False

        return _Ctx()
