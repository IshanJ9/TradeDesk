r"""Read-only check of the real 021 adapter against the live sandbox. Places NO orders.

    .venv\Scripts\python scripts\live_check.py

Needs credentials in .env: ZEROTWOONE_USERNAME (your UCC) and ZEROTWOONE_PASSWORD.
Prints one PASS/FAIL line per capability, and facts that our code had to assume from the API guide
(the epoch of option expiries, whether holdings quantities add up, what a rejected order's row looks like).
Nothing secret is printed. Note: logging in revokes any other token for the account.
"""

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.broker.zerotwoone import ZeroTwoOneAdapter
from app.config import Settings


async def step(name, coro):
    try:
        result = await coro
        print(f"PASS  {name}: {result}")
        return result
    except Exception as exc:  # report, keep going: we want to see everything that fails
        print(f"FAIL  {name}: {type(exc).__name__}: {str(exc)[:200]}")
        return None


async def main() -> int:
    s = Settings.from_env()
    if not s.zerotwoone_username or not s.zerotwoone_password:
        print("Set ZEROTWOONE_USERNAME and ZEROTWOONE_PASSWORD in .env first.")
        return 2
    print("Note: 021 allows one login per account. If the app (uvicorn) is running, stop it first, or this and the app will cancel each other's login.\n")
    a = ZeroTwoOneAdapter(
        username=s.zerotwoone_username, password=s.zerotwoone_password, base_url=s.zerotwoone_base_url,
        cache_dir=s.zerotwoone_cache_dir, price_wait=5.0,
    )
    try:
        await step("login, instrument list and price feed", a.start())
        print(f"      {len(a.master)} cash instruments loaded")
        print("      sample:", ", ".join(i.symbol for i in await a.search_instruments("tata", 5)) or "(none)")

        await step("search by company name ('infosys')", _async([i.symbol for i in await a.search_instruments("infosys", 3)]))
        expiries = await step("NIFTY option expiries", a.get_option_expiries("NIFTY"))
        if expiries:
            print(f"      nearest expiry read as {expiries[0]} (check this against the real calendar: it tells us the epoch)")
        await step("funds (estimate)", a.get_funds())
        holdings = await step("holdings", a.get_holdings())
        if holdings:
            for h in holdings[:5]:
                print(f"      {h.instrument.symbol}: qty {h.quantity}, avg {h.avg_price/100:.2f}, ltp {h.ltp/100:.2f}")
        await step("positions", a.get_positions())
        orders = await step("today's orders", a.get_orders())
        for o in (orders or [])[:5]:
            print(f"      #{o.order_id} {o.side.value} {o.quantity} {o.instrument.symbol} {o.status.value} filled {o.filled_quantity}")

        quote = await step("live price for INFY", a.get_quote("NSE:INFY"))
        if quote:
            print(f"      ltp {quote.ltp/100:.2f}, prev close {quote.prev_close/100:.2f}, bid {quote.bid}, ask {quote.ask}")
        await step("market depth, raw (INFY)", _depth(a))
        await step("NIFTY index price", a.get_quote("NSE:NIFTY"))
        if expiries:
            chain = await step("NIFTY option chain (3 strikes)", a.get_option_chain("NIFTY", expiries[0], window=1))
            if chain:
                print(f"      spot {chain.spot/100:.2f}, strikes {[r.strike/100 for r in chain.rows]}")
            await step("option chain, raw packets", _chain_raw(a))
        print(f"\nchecked at {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC. No orders were placed.")
        return 0
    finally:
        await a.close()


async def _chain_raw(a) -> str:
    """Print a few raw option-chain packets in hex, with our reading next to them, to check the field layout."""
    feed = a._chain_feed
    if feed is None or not feed._wanted:
        return "no chain feed running"
    pairs = list(feed._wanted)[:3]
    frames: list[bytes] = []
    original = feed.handle_frame

    def spy(data: bytes) -> None:
        frames.append(data)
        original(data)

    feed.handle_frame = spy
    try:
        await feed.unwatch(pairs)  # unsubscribing then subscribing again makes 021 send a fresh snapshot
        await asyncio.sleep(0.3)
        await feed.watch(pairs)
        for _ in range(50):
            if len(frames) >= 1:
                break
            await asyncio.sleep(0.1)
        await asyncio.sleep(0.5)
    finally:
        feed.handle_frame = original
    if not frames:
        return "no packets arrived"
    from app.broker.zerotwoone.packets import decode_frame

    for data in frames[:3]:
        print(f"      frame of {len(data)} bytes: {data[:60].hex(' ')}{' ...' if len(data) > 60 else ''}")
        packets, leftover = decode_frame(data)
        for p in packets[:3]:
            print(f"        we read: {p}")
        if leftover:
            print(f"        ({leftover} bytes we could not read)")
    return f"{len(frames)} frame(s)"


async def _async(value):
    return value


async def _depth(a) -> str:
    """Print INFY's raw order-book levels exactly as 021 sends them, to check how we read bid and ask."""
    token = a.master.get("NSE:INFY").token
    needle = bytes([0, 3, 0, 1]) + token.to_bytes(4, "big")  # TC 3 (full packet), exchange 1 (NSE cash), token
    seen: list[bytes] = []
    original = a._feed.handle_frame

    def spy(data: bytes) -> None:
        i = data.find(needle)
        if i >= 0 and len(data) >= i + 220:
            seen.append(data[i : i + 220])
        original(data)

    a._feed.handle_frame = spy
    try:
        for _ in range(100):
            if seen:
                break
            await asyncio.sleep(0.1)
    finally:
        a._feed.handle_frame = original
    if not seen:
        return "no full packet arrived in 10 seconds (is the market open?)"
    raw = seen[0]
    ltp = int.from_bytes(raw[8:12], "big")
    print(f"      ltp {ltp/100:.2f}; levels 0-4 should be bids (best first), 5-9 asks (best first):")
    for n in range(10):
        off = 64 + n * 14
        qty, price, orders = int.from_bytes(raw[off:off+8], "big"), int.from_bytes(raw[off+8:off+12], "big"), int.from_bytes(raw[off+12:off+14], "big")
        print(f"      level {n}: price {price/100:>9.2f}  qty {qty:>8}  orders {orders}")
    return "see levels above"


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
