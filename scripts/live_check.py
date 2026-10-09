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
        await step("NIFTY index price", a.get_quote("NSE:NIFTY"))
        if expiries:
            chain = await step("NIFTY option chain (3 strikes)", a.get_option_chain("NIFTY", expiries[0], window=1))
            if chain:
                print(f"      spot {chain.spot/100:.2f}, strikes {[r.strike/100 for r in chain.rows]}")
        print(f"\nchecked at {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC. No orders were placed.")
        return 0
    finally:
        await a.close()


async def _async(value):
    return value


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
