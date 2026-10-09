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
Bedrock is supported (see below). To add another provider: write one class with `async complete(system, messages, tools) -> LLMTurn`
(translate to/from the provider's API) and add a branch in `app/llm/factory.py`. Keep keys in `.env`.

Whichever model is used, code (not the model) enforces: order cards are written by code; every number in an
answer must come from a tool result or the trader's message; no claims of having placed an order; no advice;
text from outside the app that looks like instructions is withheld from the model. Two more checks came from
running the real model (`scripts/model_eval.py`): every figure the model puts into an order, rule or plan
(quantity, price, trigger, amount, percentage) must be one the trader actually wrote, otherwise nothing is
prepared and they are asked again; and a message that tries to change the assistant's rules ("ignore your
instructions", "developer mode", "without asking me") is answered by code and never sent to the model. A
number the trader wrote as a word ("ten") cannot be checked this way, so the check stands down for it; the
card still shows the exact quantity for the trader to confirm.

### The graph orchestrator (LangGraph)

`ORCHESTRATOR=langgraph` runs the same assistant as a LangGraph state graph (`app/agent/graph.py`) inside the
backend process; `classic` (the default) keeps the plain loop. The graph:

    START -> input_guard -> router -> model <-> tools (at most 6 rounds) -> output_guard -> END

- **input_guard**: the rule-override check; such a message ends here, answered by code.
- **router** (plain code, no model call): a question gets only the tools that read, so it cannot produce an
  order card, rule or plan even if the model asks for one (that call is refused in code). Anything that might
  be an action gets every tool. The router chooses tools only; prices, quantities and sending are never its call.
- **output_guard**: every check listed above (code-written card text, grounded numbers, no "I placed it", no advice).
- Every step is published live to the desk's **Assistant** tab (node, tool, guard verdict, time taken).

Nothing in the graph can send an order: the tools only read or draft, and only the Approve click sends. Checked
by running the whole test suite with the graph as the default (identical except the extra trace messages) and
`scripts/model_eval.py --orchestrator langgraph` with the real Bedrock model: 31 prompts, 0 failures.

### Whole-portfolio requests (level 4)

The model only picks the tool; code finds the positions and works out every quantity from live account data.
One step becomes an order card, several become one plan card approved once. Nothing is sent without approval.

- "Exit all my losing intraday positions" (`exit_losing_positions`): every intraday position in a loss, biggest
  loss first; a short is closed by buying back; at most 6 steps (it says so if there are more). Each step stands
  on its own: one refused step does not stop the rest.
- "Rebalance so no stock exceeds 20%" (`trim_to_max_weight`): sells just enough of each stock over the limit,
  measuring the portfolio as shares plus cash at current prices (shown on the card). It only sells; it never
  picks anything to buy. Real sale prices can differ slightly from the prices used for sizing.
- "Tell me when any of my holdings falls 3% in a day" (`alert_on_holdings`): one alert per stock held, measured
  from yesterday's close, so they cover today's session. A stock already past the trigger is skipped and named.

## Running on 021's sandbox

    BROKER=zerotwoone
    ZEROTWOONE_USERNAME=<your UCC, e.g. HACK1234>
    ZEROTWOONE_PASSWORD=<your password>

in `.env` (never in chat or code), then start the backend as above. The app refuses to start if the login
fails. Before trusting it, run the read-only check, which places no orders:

    .venv\Scripts\python scripts\live_check.py

To check order actions on the live sandbox (1 share of ITC at a time, virtual money; prints its plan and places
nothing without `--yes`): `scripts\live_actions.py` (modify, cancel, selling today's buy, exit-losers card) and
`scripts\chaos_live.py` (the four network failures).

**Status: the adapter (`app/broker/zerotwoone/`) was built from 021's API guide, tested against a fake 021
that follows the guide (`tests/fake021.py`), and then checked against the live sandbox with our own account.**

Verified on the live sandbox, each through the app's own card -> Approve path, with 021's order book read back
after every step:

- Login, the instrument list (about 15,600 cash instruments), search, the cash estimate, live prices over the
  market socket, and option-chain prices (`live_check.py`, read-only). Two things the guide did not say, both
  handled: the instrument file leaves index names empty (NIFTY is named from its option contracts), and option
  expiries are plain Unix seconds. The sandbox's option-chain OI and volume looked random, so we don't show them.
- Placing orders, including a part-filled order reported as part-filled, not as done.
- Four network failures on a real order call (`chaos_live.py`): reply lost, HTTP 500 and HTTP 503 after 021
  processed the order, and the request never arriving. Each time 021 ended with exactly one order (or none),
  never a duplicate, and the app never claimed what it could not prove.
- Modifying an open order's price and cancelling it; cancelling a part-filled order (`live_actions.py`).
- Selling delivery shares bought the same day: 021 lists them under positions, not holdings, and the app
  accepts the sale (`live_actions.py`).
- "Exit all my losing intraday positions" against the real positions: it prepares a card or says there is
  nothing to do, and never sends anything by itself.

Not verified live: a sell actually filling (our checks ran after market hours, so 021 accepted the orders but
did not fill them; they were cancelled), and the sandbox's own rate limits (429) in practice.

A sandbox behaviour to know about: four 1-share orders that 021 reported as `Cancelled` at the time were reported
by 021 as `Executed` about half an hour later, shortly before the sandbox cleared the day's orders at 17:00 IST.
The app shows 021's own status field and filled quantity (fills are read per order id), so it reported exactly
what 021 said each time. In this sandbox, a cancel confirmed by 021 is not necessarily final.

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
- Pending cards and plans are held in memory (rules, the audit log and the send log are in SQLite).
- Authentication (single demo user) and Co-Captain co-approval.

## Demo controls

With `DEMO_MODE=true` and `BROKER=mock`, a **Demo** menu in the header makes the judged failure cases happen on
demand (`app/demo.py`): a price jump after a card was shown, the next order's reply lost or its request lost, the
broker unreachable, a stock whose name carries a prompt-injection attempt, two losing intraday positions, and
021's Anchor lock. They change the fake market only; the cards, approval checks and send log run as always. With
any other broker, or with demo mode off, the routes answer 404 (tested), so they can never touch a real account.

### Bedrock chat

Install `requirements.txt`, then edit the backend `.env`:

```ini
LLM_PROVIDER=bedrock
AWS_BEARER_TOKEN_BEDROCK=your_bedrock_api_key
AWS_REGION=ap-south-1
BEDROCK_MODEL_ID=openai.gpt-oss-120b-1:0
```

Use the region and model/profile ID available to your AWS account. Restart the backend after changing
`.env`, then use the existing TradeDesk chat. The backend calls Bedrock Converse with conversation
history and the app's tools; requests use up to 800 output tokens and can incur AWS charges.
The key stays in the backend environment. No additional server or AWS infrastructure is required.
Missing or expired keys, denied model access and network errors use the app's existing LLM error response.

## Where things are

| Path | What |
|---|---|
| `app/schemas.py` | Shared data contract (money in integer paise) |
| `app/api_models.py` | REST and WebSocket message types |
| `app/broker/` | `BrokerAdapter` interface, `ReadOnlyView`, `MockBroker` |
| `app/orders/` | charges, hard limits, card builder, approval service, executor |
| `app/llm/` | LLM interface, tools (incl. `portfolio_tools.py`), guards, the stand-in parser, the classic orchestrator |
| `app/agent/` | LangGraph orchestrator and the tool router |
| `app/trace.py` | live trace events for the Assistant tab |
| `frontend/src/lib/` | typed API client, live-feed reducer, formatting (tested) |
| `frontend/src/components/` | the desk: chat, approval tickets, account, activity |
| `app/audit.py`, `app/db.py` | SQLite audit log and execution write-ahead log |
| `docs/superpowers/specs/` | design notes |
