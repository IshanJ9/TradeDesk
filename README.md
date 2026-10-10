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

Pages: `/` is the landing page, `/how-it-works` explains the assistant with a playable diagram of the LangGraph
orchestrator, `/login` is the log-in and sign-up screen, and `/app` is the desk. The log-in screen is a UI only for
now: no account is checked, nothing typed there is stored or sent, and it says so on the page. Every page has a
theme switch (System, Light, Dark).
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

The full system prompt, every tool definition the model receives, and the fixed replies that code substitutes for the
model are in [PROMPT.md](PROMPT.md). It is generated from the code (`scripts/gen_prompt_md.py`), and a test fails if it
ever differs from what the model really receives.

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

## Discipline: the trader's own limits, goals and "was it worth it?"

021's stated aim is protecting retail traders from overtrading. One threshold cannot fit everyone, so each trader
sets their own (`app/risk/`, the desk's **Discipline** tab). Everything shown is a fact or the trader's own
setting; nothing is advice or a forecast.

- **Profile.** Five onboarding answers suggest a Conservative, Balanced or Aggressive starting profile. The numbers
  are editable and labelled "starting values; not recommendations"; nothing is saved until the trader saves it.
  Limits: orders a day, one order's size and one stock's weight as a share of the portfolio, a daily loss limit
  after charges, a cooling-off window after consecutive losses, a re-entry window after closing a stock at a loss,
  and whether intraday is allowed. Profile and goal are stored in SQLite.
- **Friction, then hard limits only if switched on.** A card that crosses a limit shows the fact in the trader's
  own terms ("You set a 10% single-order limit; this order is about 13.38% of your portfolio"), and Approve stays
  disabled until the trader ticks "I've read this". Daily order count and daily loss become blocking only when the
  trader switches that on; blocking limits are checked again at the moment of approval. Cancels are never blocked;
  modifications only get warnings. The guard never sends anything.
- **Plans count too.** Every step of a plan (including the level-4 plans) is checked like a single card, and a
  plan's earlier steps count as orders: with a hard limit of 1 order a day, a 2-step plan is refused at step 2.
- **Today against your usual.** A 0-100 score for the day from five visible parts (activity, order size,
  concentration, intraday share, loss-chasing), compared with the trader's own recent average.
- **Was it worth it?** Recorded days above the trader's usual risk against days at or below it, with net P&L after
  charges and profitable-day counts, and the note "Past days don't predict future ones."
- **Goal.** A target gain and date, measured from the portfolio value when saved: progress, days left, amount still
  needed per week, a straight-line pace, and maximum-loss headroom. Arithmetic only. Mindful mode hides today's P&L.

Limits: 021's API returns today's orders only, so history starts when the app first records it. With demo mode on,
an empty history gets 20 synthetic days, labelled DEMO DATA everywhere they appear. P&L is an estimate from fills
(approximate FIFO; missing prices or cost bases are disclosed, not invented). Orders placed in 021's own app count
toward today's numbers but cannot be blocked by this app. One trader per database.

Checked: about 215 risk tests, including deliberate breaks of every guard rule (18 of 18 caught), plus the plan-step
tests. On the live 021 sandbox with our account: onboarding, saving a profile, card warnings at live prices (a
preview placed nothing), goal arithmetic against the real portfolio value, live refresh, and the profile and goal
surviving a restart. The blocking limits were tested with the mock broker only.

## Voice input

The mic button next to the chat box records a short clip; the backend sends it to Groq's `whisper-large-v3-turbo`
(`app/voice/`, `GROQ_API_KEY` in `.env`, never sent to the browser). The words land in the chat box, with numbers
highlighted, for the trader to check and edit. Nothing is sent until they press Send, and the reply then goes
through every guard above, the same as typed text. Voice can never place, approve or confirm anything.

Clips are capped at 5 MB, at most 15 a minute (inside Groq's free tier); audio and transcripts are not stored, and
the audit log records only that a clip was transcribed. Without a key the mic says "Voice is not set up on this
server". Checked: the route's tests (missing key, rate limits, timeouts, size and type limits, the key never
appearing in any response or log), and one real recording ("buy five ITC at market") sent through the running app
to Groq: it returned "Buy 5 ITC at market." and the broker received nothing. Groq is an outside service; a local
Whisper model would keep audio in-house (future scope).

## Orders placed in 021's own app

The trader may also trade in 021's own app, on the same account. Every 5 seconds the backend reads the same order
book (same login, no second session) and labels each order: ours if it is in our send log, otherwise "Placed in
021's app, not by TradeDesk". While one of our own sends is still unconfirmed, nothing is labelled as outside, so
an order of ours whose reply was lost is never mislabelled. Outside orders appear in the desk's **021 app** tab,
are saved to SQLite (`app/history/`), and count toward the Discipline numbers. Checked: sync tests against the mock
and the fake 021, and on the demo build (an order placed straight at the broker appeared in the tab within 5
seconds). Not yet seen with a real order placed in 021's app. 021 offers no push for this, so it is polling, not
instant.

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

## Restarts

Everything that matters is in SQLite (`DATABASE_URL`): the send log, the audit log, standing rules, cards and plans
waiting for approval, plan reports, the Discipline profile, goal and history. After a restart:

- A card or plan that was waiting is still there, with the same fingerprint, so approving it still has to match
  exactly; one that expired meanwhile is refused as expired, and one already sent cannot be approved again.
- An order that was mid-send is looked up in the broker's order book, never re-sent (see "No client order id").
- A plan that was running cannot continue where it stopped. It is marked halted and its report says so: steps not
  yet sent are never sent, and any step already sent is in the order book.

Checked by restarting the app on the same database file in tests, and by breaking each part on purpose (4 of 4
caught). With `DATABASE_URL=sqlite:///:memory:` (the demo setup) a restart starts clean, by design.

## Not built yet (future scope)

- The orders websocket (live fills). Order state is read over REST, which the guide calls the source of truth,
  and orders from 021's own app are found by polling every 5 seconds.
- A local speech-to-text model (voice uses Groq today), spoken replies, and regional languages.
- F&O orders. Orders are equity only; the option chain is read-only.
- Authentication (single demo user) and Co-Captain co-approval.

## Demo controls

With `DEMO_MODE=true` and `BROKER=mock`, a **Demo** menu in the header makes the judged failure cases happen on
demand (`app/demo.py`): a price jump after a card was shown, the next order's reply lost or its request lost, the
broker unreachable, a stock whose name carries a prompt-injection attempt, two losing intraday positions, an
order placed straight at the broker as 021's own app would, and 021's Anchor lock. They change the fake market only; the cards, approval checks and send log run as always. With
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
If Bedrock is unavailable (missing or expired key, denied model access, throttling, network errors), the turn is
answered by the built-in keyword stand-in instead (`app/llm/fallback.py`), with a notice telling the trader so;
every guard and approval check is unchanged. Bedrock is skipped for a minute after a failure, so an outage doesn't
make every message wait for a timeout. `scripts/model_eval.py` always tests the real model, never the fallback.

## Current implementation work

See [FEATURE_CHECKLIST.md](FEATURE_CHECKLIST.md) for completed items and the full
remaining scope, and [ANALYTICS_HANDOFF.md](ANALYTICS_HANDOFF.md) for the new
observed-risk/returns/patterns calculations and manual 021 acceptance steps.
These analytics exclude mock/demo and unverified legacy records; an empty view
means actual recorded history is still needed.

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
| `app/risk/` | Discipline: profile, goals, the limits guard, daily facts and reports |
| `app/voice/` | voice transcription (Groq) |
| `app/sync/`, `app/history/` | orders from 021's own app; saved daily activity |
| `app/demo.py` | demo controls (demo mode + mock broker only) |
| `app/broker/zerotwoone/` | the 021 adapter: REST, binary market socket, instrument list |
| `scripts/` | live checks against 021, network-failure checks, the real-model prompt check |
| `frontend/src/lib/` | typed API client, live-feed reducer, formatting (tested) |
| `frontend/src/components/` | the desk: chat, approval tickets, account, activity |
| `app/audit.py`, `app/db.py` | SQLite audit log and execution write-ahead log |
