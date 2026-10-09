r"""Makes the REAL sandbox misbehave on purpose and shows what our app does about it.

    .venv\Scripts\python scripts\chaos_live.py          # shows the plan, places nothing
    .venv\Scripts\python scripts\chaos_live.py --yes    # runs it: places up to 4 small buy orders of ITC (virtual money)

For each situation it does the real thing end to end: builds an order card, then "clicks Approve" with exactly one
order call sabotaged:

  lost-reply     021 receives and processes the order, but our reply is lost
  http-500       021 processes the order, then we are told 500 'Internal Server Error'
  http-503       021 processes the order, then we are told 503
  lost-request   the request never reaches 021 at all

and then compares what the app SAID with what 021's own order book SHOWS. The rule: an order is never sent twice,
and the app never claims something it cannot prove. Orders are 1, 2, 3 and 4 shares so look-alikes cannot confuse
the check. Uses the real 021 login from .env; it uses the keyword stand-in for chat (no AI calls).
"""

import argparse
import dataclasses
import functools
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.broker.zerotwoone import ZeroTwoOneAdapter  # noqa: E402
from app.broker.zerotwoone.chaos import ChaosTransport  # noqa: E402
from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402

PLAN = [("lost-reply", 1), ("http-500", 2), ("http-503", 3), ("lost-request", 4)]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--yes", action="store_true", help="actually place the (virtual-money) orders")
    parser.add_argument("--only", choices=[m for m, _ in PLAN], help="run just one situation")
    args = parser.parse_args()
    plan = [p for p in PLAN if not args.only or p[0] == args.only]

    base = Settings.from_env()
    if not base.zerotwoone_username or not base.zerotwoone_password:
        print("Set ZEROTWOONE_USERNAME and ZEROTWOONE_PASSWORD in .env first.")
        return 2
    print("Note: 021 allows one login per account. If the app (uvicorn) is running, stop it first, or this and the app will cancel each other's login.\n")
    print("Plan (each is a buy of ITC at a protected market price, on your 021 sandbox account):")
    for mode, qty in plan:
        print(f"  {mode:<13} {qty} share(s)")
    if not args.yes:
        print("\nNothing was placed. Run again with --yes to do it.")
        return 0

    settings = dataclasses.replace(
        base, broker="zerotwoone", llm_provider="rules", database_url="sqlite:///:memory:", ticker_interval=None,
        reconcile_interval=None, reconcile_grace_seconds=3.0, timeout_reconcile_delay=0.5, account_push_interval=3600,
    )
    chaos = ChaosTransport()
    adapter = ZeroTwoOneAdapter(
        username=base.zerotwoone_username, password=base.zerotwoone_password, base_url=base.zerotwoone_base_url,
        cache_dir=base.zerotwoone_cache_dir, http=httpx.AsyncClient(transport=chaos, timeout=httpx.Timeout(10.0, connect=5.0)),
    )
    failures = 0
    with TestClient(create_app(settings, broker=adapter)) as client:
        def on_loop(fn, *a):
            return client.portal.call(functools.partial(fn, *a))

        time.sleep(1.5)  # let the price feed connect
        seen = {o.order_id for o in on_loop(adapter.get_orders)}
        print(f"\nstarting with {len(seen)} order(s) already in the order book\n")
        for mode, qty in plan:
            card = client.post("/api/orders/preview", json=dict(action="PLACE", instrument_ref="ITC", side="BUY", quantity=qty, order_type="MARKET"))
            cards = card.json().get("cards", [{}])
            if cards[0].get("type") != "pending_order":
                print(f"FAIL  {mode}: could not make an order card: {card.json().get('text')}")
                failures += 1
                continue
            p = cards[0]["pending"]
            chaos.arm(mode)
            body = client.post(f"/api/approvals/{p['id']}/approve", json={"order_hash": p["order_hash"]}).json()
            outcome = body.get("outcome") or body.get("code")
            note = ""
            if mode == "lost-request" and outcome == "UNKNOWN":
                time.sleep(settings.reconcile_grace_seconds + 1)
                resolved = client.post("/api/executions/reconcile").json()
                note = f" | after the wait, reconcile said {resolved}"
            orders = on_loop(adapter.get_orders)
            new = [o for o in orders if o.order_id not in seen]
            seen |= {o.order_id for o in new}
            row = client.app.state.db.query("SELECT status FROM executions WHERE pending_id = ?", (p["id"],))
            logged = row[0]["status"] if row else "(none)"
            print(f"--- {mode} ({qty} share(s))")
            print(f"    the app said : {outcome} | {body.get('message', '')[:140]}")
            print(f"    our send log : {logged}{note}")
            print(f"    021 shows    : {len(new)} new order(s) {[(o.order_id, o.quantity, o.status.value) for o in new]}")

            if len(new) > 1:
                verdict, ok = "TWO ORDERS: a duplicate was sent", False
            elif mode == "lost-request":
                ok = not new and outcome == "UNKNOWN" and logged == "NOT_SENT"
                verdict = "no order, said unknown, then not-sent after the wait" if ok else "unexpected"
            else:
                ok = len(new) == 1 and outcome in ("SENT", "UNKNOWN") and not (outcome == "SENT" and not new)
                verdict = "exactly one order, found without re-sending" if ok and outcome == "SENT" else (
                    "exactly one order; the app said unknown (safe, but could not tell)" if ok else "unexpected")
            print(f"    {'PASS' if ok else 'FAIL'}         : {verdict}\n")
            failures += 0 if ok else 1
    print("done." if not failures else f"{failures} situation(s) did not behave as expected: send me this output.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
