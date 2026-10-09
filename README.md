# TradeDesk-AI

A copilot for a 021 Trade account. The LLM can read data and draft orders; plain code owns every
money rule; only a human click on an exact order card can send an order.

## Run (backend)

    python -m venv .venv
    .venv\Scripts\python -m pip install -r requirements.txt
    copy .env.example .env
    .venv\Scripts\python -m uvicorn app.main:create_app --factory --reload

API docs at http://127.0.0.1:8000/docs. WebSocket at `ws://127.0.0.1:8000/ws`.

## Run (frontend)

    cd frontend
    npm install
    npm run dev          # http://localhost:5173, proxies /api and /ws to the backend on :8000

Start the backend first. `npm run build` makes the production bundle (`npm run preview` serves it).
The UI follows the system light/dark setting, bundles its fonts (no network needed), and works down to
phone width (an Ask / Desk switch appears below 1024px).

## Test

    .venv\Scripts\python -m pytest -q                 # backend
    cd frontend && npm test && npm run typecheck      # frontend

## Frontend types

Pydantic models are the single source of truth. After changing any model or route:

    .venv\Scripts\python scripts/gen_types.py
    cd frontend && npm run typecheck

Never edit `frontend/src/lib/types.gen.ts` by hand.

## The assistant (LLM)

The chat runs behind `app/llm/types.py::LLMClient`. `LLM_PROVIDER=rules` (the default) uses a built-in
keyword parser that makes the same tool calls a real model would, so everything works without a provider.
To add a provider such as Bedrock: write one class with `async complete(system, messages, tools) -> LLMTurn`
(translate to/from the provider's API) and add a branch in `app/llm/factory.py`. Keep keys in `.env`.

Whichever model is used, code (not the model) enforces: order cards are written by code; every number in an
answer must come from a tool result or the trader's message; no claims of having placed an order; no advice;
text from outside the app that looks like instructions is withheld from the model.

## Running on 021's sandbox

    BROKER=zerotwoone
    ZEROTWOONE_USERNAME=<your UCC, e.g. HACK1234>
    ZEROTWOONE_PASSWORD=<your password>

in `.env` (never in chat or code), then start the backend as above. The app refuses to start if the login
fails. Before trusting it, run the read-only check, which places no orders:

    .venv\Scripts\python scripts\live_check.py

**Status: the adapter (`app/broker/zerotwoone/`) has been built from 021's API guide and tested against a fake
021 that follows the guide (`tests/fake021.py`). It has not yet been run against the live sandbox**, so the
guide's gaps (below) are assumptions until `live_check.py` confirms them.

What the guide forced, and what we did about it:

- **No client order id.** `POST /orders` has no idempotency field. Duplicate-send protection is therefore
  ours: a write-ahead row keyed by our own `client_order_id` is inserted before the broker is called, so
  the same order cannot be sent twice from this app. After a timeout we never re-send; we read the order
  book for one order that looks exactly like what we sent (same instrument, side, quantity, price,
  trigger, product, validity, created after we began sending, and not already matched to another of our
  sends). One match means sent. Several look-alikes mean the outcome stays UNKNOWN and the trader is told
  to look at their order book. We say "never placed" only after a clean read of the book plus two minutes.
  The residual risk: an identical order placed elsewhere in the same window cannot be told apart.
- **Ambiguous errors.** A failed order call is called a rejection only when the broker clearly says so
  (`app/broker/errors.py`). An HTTP 500 or 503 without a clear refusal is treated as unknown, because
  calling an unknown "rejected" is what leads to a double order. Orders are never retried; reads are
  retried twice. A 401 for an expired token logs in again and repeats the call once (it was refused before
  processing, so that is safe).
- **Funds are an estimate.** There is no funds endpoint. Cash is Rs 10,00,000 (the daily top-up) less today's
  net trades and money tied up in open buy orders.
- **Anchor and Co-Captain are simulated.** The API has nothing for them.
- **No quote endpoint.** Prices come from the binary market socket, which sends at most one snapshot every
  300 ms. A standing instruction fires on the first snapshot that shows the condition; a dip that comes and
  goes inside one interval is invisible (tested). The socket reconnects with a fresh key and re-subscribes
  after a drop or 021's daily 08:00 restart.
- **No company names in the instrument file.** A small table of well-known NSE names lets "Infosys" find
  INFY; other stocks are found by symbol. The file has no series either, so every cash stock is EQ.
- **Order sizes.** A row gives `qtyTraded` and `qtyRemaining`; size is their sum. A filled order's side comes
  from its trades. A row whose side or size cannot be told is left out rather than guessed.
- **Stop-loss orders** (021's `SL` book with a trigger) can be placed and moved. The card says nothing
  happens until the trigger price is reached and warns that a gap past the limit may not fill.
- **Order validity** is DAY or IOC only, as 021 offers.

## Not built yet (future scope)

- The orders websocket (live fills). Order state is read over REST, which the guide calls the source of truth.
- F&O orders. Orders are equity only; the option chain is read-only.
- Running against the live sandbox has not happened yet (see above).
- Pending cards and plans are held in memory (rules, the audit log and the send log are in SQLite).
- An LLM provider. The built-in keyword parser is used until one is chosen.
- Demo and chaos switches, authentication (single demo user), and Co-Captain co-approval.

## Where things are

| Path | What |
|---|---|
| `app/schemas.py` | Shared data contract (money in integer paise) |
| `app/api_models.py` | REST and WebSocket message types |
| `app/broker/` | `BrokerAdapter` interface, `ReadOnlyView`, `MockBroker` |
| `app/orders/` | charges, hard limits, card builder, approval service, executor |
| `app/llm/` | LLM interface, tools, guards, the stand-in parser, the orchestrator |
| `frontend/src/lib/` | typed API client, live-feed reducer, formatting (tested) |
| `frontend/src/components/` | the desk: chat, approval tickets, account, activity |
| `app/audit.py`, `app/db.py` | SQLite audit log and execution write-ahead log |
| `docs/superpowers/specs/` | design notes |
