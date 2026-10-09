r"""Prints what 021 REALLY returns for your orders, holdings, positions and trades, next to how our app read each row.
Read-only: it places, changes and cancels nothing.

    .venv\Scripts\python scripts\show_account.py

Use it when something in the chat does not match 021's own screen (an order "not found", shares "not held").
The rows contain only your sandbox account's data. Logging in revokes any other token for the account, so close
other copies of the app first if you see a 401.
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.broker.zerotwoone import ZeroTwoOneAdapter  # noqa: E402
from app.config import Settings  # noqa: E402


def compact(row: dict) -> str:
    return json.dumps(row, separators=(",", ":"), ensure_ascii=False)


async def main() -> int:
    s = Settings.from_env()
    if not s.zerotwoone_username or not s.zerotwoone_password:
        print("Set ZEROTWOONE_USERNAME and ZEROTWOONE_PASSWORD in .env first.")
        return 2
    print("Note: 021 allows one login per account. If the app (uvicorn) is running, stop it first, or this and the app will cancel each other's login.\n")
    a = ZeroTwoOneAdapter(username=s.zerotwoone_username, password=s.zerotwoone_password,
                          base_url=s.zerotwoone_base_url, cache_dir=s.zerotwoone_cache_dir, price_wait=5.0)
    try:
        await a.start()

        print("=== ORDERS: 021's rows, and what our app made of each ===")
        raw_orders = await a._read("GET", "/orders") or []
        for row in raw_orders:
            listing = a.master.by_token(row.get("exchange", ""), int(row.get("token") or 0))
            name = listing.instrument.symbol if listing else f"(token {row.get('token')} not in our list)"
            parsed = None
            try:
                parsed = await a._build_order(row, with_fills=True)
            except Exception as exc:  # show it, do not hide it
                print(f"  ! could not read order {row.get('orderId')}: {type(exc).__name__}: {exc}")
            print(f"- order {row.get('orderId')} {name}")
            print(f"    021 says : {compact(row)}")
            if parsed is None:
                print("    we read  : LEFT OUT (side or size could not be told, or not a stock we trade)")
            else:
                print(f"    we read  : {parsed.side.value} {parsed.quantity} {parsed.status.value}, filled {parsed.filled_quantity}, "
                      f"pending {parsed.pending_quantity}, avg {parsed.avg_fill_price}, limit {parsed.limit_price}")
        if not raw_orders:
            print("  (021 returned no orders)")

        print("\n=== HOLDINGS (shares in your demat account) ===")
        for row in await a._read("GET", "/portfolio/holdings") or []:
            print(f"  {compact(row)}")
        print("\n=== POSITIONS (what you traded today) ===")
        for row in await a._read("GET", "/portfolio/positions") or []:
            print(f"  {compact(row)}")
        print("\n=== TRADES (fills) ===")
        for row in await a._read("GET", "/trades") or []:
            print(f"  {compact(row)}")

        print("\n=== what our app shows the trader ===")
        for h in await a.get_holdings():
            print(f"  holding : {h.instrument.symbol} x{h.quantity}")
        for p in await a.get_positions():
            print(f"  position: {p.instrument.symbol} x{p.quantity} ({p.product.value})")
        print(f"  orders  : {[(o.order_id, o.instrument.symbol, o.status.value) for o in await a.get_orders()]}")
        return 0
    finally:
        await a.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
