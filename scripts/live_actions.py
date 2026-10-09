r"""Checks, on the REAL 021 sandbox, the order actions not yet verified live: modify, cancel, selling shares
bought today, and the "exit all my losing intraday positions" plan. Everything goes through the app's own path
(card -> Approve), exactly as a trader's click would.

    .venv\Scripts\python scripts\live_actions.py                      # shows the plan, places nothing
    .venv\Scripts\python scripts\live_actions.py --yes                # runs it (virtual money, 1 share of ITC at a time)
    .venv\Scripts\python scripts\live_actions.py --yes --only modify  # one step: modify | sell-today | exit-losers
    .venv\Scripts\python scripts\live_actions.py --yes --cancel 256294   # also cancel one of YOUR open orders

Steps:
  modify       buy 1 ITC with a limit 5% under the price (so it waits), move the limit to 4% under, then cancel it.
               Checks after each step that 021's own order book shows the change.
  sell-today   buy 1 ITC at market (delivery), wait for the fill, then sell that same share. 021 keeps today's
               delivery buys under positions, not holdings; the app must still let you sell it.
  exit-losers  asks the chat "exit all my losing intraday positions" and shows the card. It NEVER approves it.

Uses the keyword stand-in for chat (no AI calls). Logging in revokes any other 021 login for the account: stop the
app (uvicorn) first. If the market is closed the sandbox may refuse or not fill; the script reports what 021 says.
"""

import argparse
import dataclasses
import functools
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app.broker.zerotwoone import ZeroTwoOneAdapter  # noqa: E402
from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.schemas import OrderStatus, fmt_rupees  # noqa: E402

STEPS = ("modify", "sell-today", "exit-losers")
SYMBOL = "ITC"
LIVE = (OrderStatus.OPEN, OrderStatus.PARTIAL)


class Run:
    def __init__(self, client: TestClient, adapter: ZeroTwoOneAdapter, symbol: str = SYMBOL):
        self.client, self.adapter, self.symbol, self.failures = client, adapter, symbol, 0

    def on_loop(self, fn, *a):
        return self.client.portal.call(functools.partial(fn, *a))

    def result(self, ok: bool, what: str) -> bool:
        print(f"    {'PASS' if ok else 'FAIL'}  {what}")
        self.failures += 0 if ok else 1
        return ok

    def card(self, **intent) -> dict | None:
        body = self.client.post("/api/orders/preview", json=intent).json()
        cards = [c for c in body.get("cards", []) if c.get("type") == "pending_order"]
        print(f"    card  : {body.get('text', '')[:200]}")
        return cards[0]["pending"] if cards else None

    def approve(self, pending: dict) -> dict:
        r = self.client.post(f"/api/approvals/{pending['id']}/approve", json={"order_hash": pending["order_hash"]})
        body = r.json()
        if r.status_code == 409 and body.get("code") == "REQUOTE_REQUIRED" and body.get("pending"):
            print("    price moved since the card; approving the fresh card once")
            return self.approve(body["pending"])
        order = body.get("order") or {}
        print(f"    sent  : {body.get('outcome') or body.get('code')} | order {order.get('order_id')} {order.get('status')} | {body.get('message', '')[:140]}")
        return body

    def order_at_021(self, order_id: str):
        return self.on_loop(self.adapter.get_order, order_id)

    def wait_for(self, order_id: str, done, seconds: float = 20.0):
        """Poll 021's own order book until `done(order)` or the time runs out. Returns the last order seen."""
        deadline, order = time.time() + seconds, None
        while time.time() < deadline:
            order = self.order_at_021(order_id)
            if order is not None and done(order):
                return order
            time.sleep(1.0)
        return order

    def price(self) -> int:
        return self.on_loop(self.adapter.get_quote, f"NSE:{self.symbol}").ltp


def describe(order) -> str:
    if order is None:
        return "not found in 021's order book"
    limit = fmt_rupees(order.limit_price) if order.limit_price else "market"
    return f"{order.side.value} {order.quantity} {order.instrument.symbol} @ {limit} | {order.status.value}, filled {order.filled_quantity}"


def tick(paise: int) -> int:
    return paise // 5 * 5  # NSE prices move in 5-paise steps


# ---- steps ----------------------------------------------------------------------------------------- #


def step_cancel_existing(run: Run, order_id: str) -> None:
    print(f"\n--- cancel your open order {order_id}")
    before = run.order_at_021(order_id)
    print(f"    021 before: {describe(before)}")
    if before is None or before.status not in LIVE:
        run.result(False, "that order is not open, so it cannot be cancelled")
        return
    pending = run.card(action="CANCEL", target_order_id=order_id)
    if not run.result(pending is not None, "a cancel card was made"):
        return
    run.approve(pending)
    after = run.wait_for(order_id, lambda o: o.status not in LIVE, 15)
    print(f"    021 after : {describe(after)}")
    run.result(after is not None and after.status is OrderStatus.CANCELLED, "021 shows the order cancelled")


def step_modify(run: Run) -> None:
    print(f"\n--- modify and cancel (1 {run.symbol}, priced so it waits)")
    ltp = run.price()
    first, second = tick(ltp * 95 // 100), tick(ltp * 96 // 100)
    print(f"    {run.symbol} is at {fmt_rupees(ltp)}; limit {fmt_rupees(first)}, then moved to {fmt_rupees(second)}")
    pending = run.card(action="PLACE", instrument_ref=run.symbol, side="BUY", quantity=1, order_type="LIMIT", limit_price=first)
    if not run.result(pending is not None, "a buy card was made"):
        return
    sent = run.approve(pending)
    order_id = (sent.get("order") or {}).get("order_id")
    if not run.result(sent.get("outcome") == "SENT" and order_id, "021 accepted the order"):
        return
    placed = run.wait_for(order_id, lambda o: o.status in LIVE or o.status is OrderStatus.REJECTED, 10)
    print(f"    021 shows : {describe(placed)}")
    if not run.result(placed is not None and placed.status in LIVE, "the order is open at 021"):
        return

    pending = run.card(action="MODIFY", target_order_id=order_id, limit_price=second)
    if run.result(pending is not None, "a modify card was made"):
        run.approve(pending)
        moved = run.wait_for(order_id, lambda o: o.limit_price == second, 10)
        print(f"    021 shows : {describe(moved)}")
        run.result(moved is not None and moved.limit_price == second, f"021 shows the new limit {fmt_rupees(second)}")

    pending = run.card(action="CANCEL", target_order_id=order_id)
    if run.result(pending is not None, "a cancel card was made"):
        run.approve(pending)
        gone = run.wait_for(order_id, lambda o: o.status not in LIVE, 15)
        print(f"    021 shows : {describe(gone)}")
        run.result(gone is not None and gone.status is OrderStatus.CANCELLED, "021 shows the order cancelled")


def step_sell_today(run: Run) -> None:
    print(f"\n--- sell a share bought today (1 {run.symbol}, delivery)")
    todays = [p for p in run.on_loop(run.adapter.get_positions) if p.instrument.symbol == run.symbol and p.product.value == "CNC" and p.quantity > 0]
    if todays:
        print(f"    you already bought {todays[0].quantity} {run.symbol} today (021 lists it under positions, not holdings): selling 1 of those")
        _sell_one(run)
        return
    pending = run.card(action="PLACE", instrument_ref=run.symbol, side="BUY", quantity=1, order_type="MARKET", product="CNC")
    if not run.result(pending is not None, "a buy card was made"):
        return
    sent = run.approve(pending)
    order_id = (sent.get("order") or {}).get("order_id")
    if not run.result(sent.get("outcome") == "SENT" and order_id, "021 accepted the buy"):
        return
    bought = run.wait_for(order_id, lambda o: o.status is OrderStatus.FILLED or o.status not in LIVE, 30)
    print(f"    021 shows : {describe(bought)}")
    if not run.result(bought is not None and bought.status is OrderStatus.FILLED, "the buy filled"):
        if bought is not None and bought.status in LIVE:
            print("    (not filled yet: thin sandbox book or market closed; cancelling it so nothing is left open)")
            pending = run.card(action="CANCEL", target_order_id=order_id)
            if pending:
                run.approve(pending)
        return
    _sell_one(run)


def _sell_one(run: Run) -> None:
    pending = run.card(action="PLACE", instrument_ref=run.symbol, side="SELL", quantity=1, order_type="MARKET", product="CNC")
    if not run.result(pending is not None, "the app lets you sell the share bought today (no 'you don't hold any')"):
        return
    sent = run.approve(pending)
    order_id = (sent.get("order") or {}).get("order_id")
    if not run.result(sent.get("outcome") == "SENT" and order_id, "021 accepted the sell"):
        return
    sold = run.wait_for(order_id, lambda o: o.status not in LIVE, 30)
    print(f"    021 shows : {describe(sold)}")
    if not run.result(sold is not None and sold.status is OrderStatus.FILLED, "the sell filled"):
        if sold is not None and sold.status in LIVE:
            print("    (not filled yet: thin sandbox book or market closed; cancelling it so nothing is left open)")
            pending = run.card(action="CANCEL", target_order_id=order_id)
            if pending:
                run.approve(pending)


def step_exit_losers(run: Run) -> None:
    print("\n--- 'exit all my losing intraday positions' (card only, never approved)")
    positions = run.on_loop(run.adapter.get_positions)
    for p in positions:
        print(f"    position  : {p.instrument.symbol} {p.quantity} ({p.product.value}) P&L {fmt_rupees(p.pnl)}")
    if not positions:
        print("    position  : (none today)")
    before = len(run.on_loop(run.adapter.get_orders))
    body = run.client.post("/api/chat", json={"message": "exit all my losing intraday positions"}).json()
    kinds = [c["type"] for c in body.get("cards", [])]
    print(f"    chat says : {body.get('text', '')[:300]}")
    print(f"    cards     : {kinds}")
    losers = [p for p in positions if p.product.value == "MIS" and p.pnl < 0]
    ok = ("plan" in kinds or "pending_order" in kinds) if losers else "Nothing was prepared" in body.get("text", "")
    run.result(ok, f"{len(losers)} losing intraday position(s) -> {'a card to approve' if losers else 'nothing prepared'}")
    after = len(run.on_loop(run.adapter.get_orders))
    run.result(after == before, f"asking only made a card: 021 had {before} orders before and {after} after")


# ---- main ------------------------------------------------------------------------------------------ #


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--yes", action="store_true", help="actually place the (virtual-money) orders")
    parser.add_argument("--only", choices=STEPS)
    parser.add_argument("--cancel", metavar="ORDER_ID", help="also cancel this open order of yours")
    args = parser.parse_args()
    steps = [s for s in STEPS if not args.only or s == args.only]

    base = Settings.from_env()
    if not base.zerotwoone_username or not base.zerotwoone_password:
        print("Set ZEROTWOONE_USERNAME and ZEROTWOONE_PASSWORD in .env first.")
        return 2
    print("Note: 021 allows one login per account. If the app (uvicorn) is running, stop it first.\n")
    print(f"Plan: {', '.join(steps)}" + (f", and cancel your order {args.cancel}" if args.cancel else ""))
    print(f"Orders are 1 share of {SYMBOL} at a time, on your 021 sandbox account (virtual money).")
    if not args.yes:
        print("\nNothing was placed. Run again with --yes to do it.")
        return 0

    settings = dataclasses.replace(
        base, broker="zerotwoone", llm_provider="rules", orchestrator="classic", database_url="sqlite:///:memory:",
        ticker_interval=None, reconcile_interval=None, account_push_interval=3600, approval_ttl_seconds=120,
    )
    adapter = ZeroTwoOneAdapter(
        username=base.zerotwoone_username, password=base.zerotwoone_password,
        base_url=base.zerotwoone_base_url, cache_dir=base.zerotwoone_cache_dir,
    )
    with TestClient(create_app(settings, broker=adapter)) as client:
        time.sleep(1.5)  # let the price feed connect
        run = Run(client, adapter)
        if args.cancel:
            step_cancel_existing(run, args.cancel)
        for step in steps:
            {"modify": step_modify, "sell-today": step_sell_today, "exit-losers": step_exit_losers}[step](run)
        open_now = [o for o in run.on_loop(adapter.get_orders) if o.status in LIVE]
        print(f"\nopen orders at 021 now: {[(o.order_id, o.instrument.symbol, o.quantity) for o in open_now] or 'none'}")
    print("all passed." if not run.failures else f"{run.failures} check(s) failed: send me this output.")
    return 1 if run.failures else 0


if __name__ == "__main__":
    sys.exit(main())
