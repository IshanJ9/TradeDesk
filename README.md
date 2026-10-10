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
orchestrator, `/login` is the log-in and sign-up screen, and `/app` is the desk, which needs a signed-in account (an
anonymous visit is sent to `/login`). The first account to register inherits anything saved before accounts existed
(or set `TRADEDESK_OWNER_EMAIL`). Every page has a theme switch (System, Light, Dark).
The UI follows the system light/dark setting, bundles its fonts (no network needed), and works down to
phone width (an Ask / Desk switch appears below 1024px).

## Test

    .venv\Scripts\python -m pytest -q                 # backend
    cd frontend && npm test && npm run typecheck      # frontend

CI (`.github/workflows/ci.yml`) runs on every pull request and every push to `main`: the backend tests (which include
the check that `PROMPT.md` matches what the model receives), the frontend typecheck, tests and build, and a check that
`frontend/openapi.json` and `types.gen.ts` match what the backend generates. It needs no secrets.

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

    START -> input_guard -> router -> read | risk | order | rule | plan -> model <-> tools (at most 6 rounds)
          -> output_guard -> END

- **input_guard**: the rule-override check; such a message ends here, answered by code.
- **router** (plain code, no model call) sends each message down one of five routes, and each route's node fixes
  the tools the model may use. **read** (a question) and **risk** (your limits and discipline report) get only
  tools that read; **order** adds the order-card tool; **rule** adds alerts and standing rules; **plan** adds
  multi-step plans and the whole-portfolio tools. A tool outside the route is refused in code even if the model
  asks for it, so a question can never draft an order and an order request can never save a rule. The router
  chooses tools only; prices, quantities and sending are never its call. Its exact rules are in
  [PROMPT.md](PROMPT.md).
- **output_guard**: every check listed above (code-written card text, grounded numbers, no "I placed it", no advice).
- Every step is published live to the desk's **Assistant** tab (node, tool, guard verdict, time taken).

Nothing in the graph can send an order: the tools only read or draft, and only the Approve click sends. Checked
by running the whole test suite with the graph as the default (identical except the extra trace messages) and
`scripts/model_eval.py --orchestrator langgraph` with the real Bedrock model (`openai.gpt-oss-120b-1:0`). Re-run
on 10 October 2026 with the five routes: 32 prompts, 23 passed by code, 9 read by hand and correct, 0 failed,
65 model calls, no order reached the broker. Every order, rule, plan, alert and trim case reached its tool inside
its route. The tests also check that the built-in keyword reader's tool choices fall inside each route.

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
toward today's numbers but cannot be blocked by this app. Each account has its own history, limits and goal.

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
to Groq: it returned "Buy 5 ITC at market." and the broker received nothing.

**Local voice (optional).** With `VOICE_PROVIDER=local`, recordings are transcribed on this machine by
faster-whisper (`small`, CPU, int8), so the audio never leaves it. One-time setup:

    .venv\Scripts\python -m pip install -r requirements-voice-local.txt
    .venv\Scripts\python scripts\download_voice_model.py     # ~480 MB into .cache/whisper-small (git-ignored)

The app never downloads a model itself: if the files are missing, local voice reports "not set up". One recording
is transcribed at a time, for at most 20 seconds, and recordings over 35 seconds are refused. If the local model
fails and `VOICE_FALLBACK=groq` (the default), Groq transcribes it instead and the chat says so ("the audio was
uploaded"); with `VOICE_FALLBACK=none` the audio stays local and the trader types instead. The chat footer always
says where audio goes. Checked: tests with a fake engine (no upload, fallback and no-fallback, busy, timeout,
the real engine refusing to run without its files), and a real recording through the running app: "Buy 5 ITC at
market.", 3 s with the English hint (about 7 s for the first clip while the model loads).

## Options

"Buy 1 lot NIFTY 24500 CE" drafts an option card. The model only names the contract (underlying, strike, call or
put, optional expiry) and the number of lots; code finds the exact contract (from 021's instrument file on the live
broker, with its real lot size and token), multiplies lots by the lot size, and picks the nearest expiry when none
was named, which the card states. The strike and lot count must be numbers the trader typed, like every other
figure. Options are carried as NRML (021's F&O product) or held intraday as MIS.

- **Buy, sell what you hold, or write (writing needs `ALLOW_UNLIMITED_RISK_FO=true`).** Selling up to the long position held (in that product) is closing, and
  buying back a short is closing too. Selling more than you hold is **writing**: see "Futures and writing" below.
- **A factual notice on every option buy, never advice:** the whole premium can be lost if the contract expires
  worthless on its date, and "SEBI's study of FY22 to FY24 found that 93% of individual traders in equity F&O made a
  loss". The card asks for the typed `I UNDERSTAND` before Approve.
- **021's Options charges:** flat ₹20 brokerage, STT 0.15% on the sell side, exchange charges 0.03503% (NSE),
  stamp duty 0.003% on buys, IPFT 0.0005%, all on the premium (`app/orders/charges.py`).
- Approval re-checks, the send log, your own limits and the hard stops apply as for shares. Options and futures
  are ordered one at a time: rules and plans stay shares only.

Checked: tests on the mock (lots, nearest and named expiry, puts, wrong strike or size, charges worked by hand,
buy then sell what is held) and on the fake 021 (the contract and lot size from 021's file, the order sent as
`NSEFO` / `NRML` with the right token, F&O rows read back from the order book and positions), and in the browser
(the card, the typed acknowledgment, approval, the position). Not yet sent to the live sandbox.

## Futures and writing options

**Off by default.** Opening a futures position or selling an option you don't hold (writing) is refused in code: the
trader gets a plain message and no card is made. Closing futures or options you already hold, and buying options,
still work. The operator can switch the rest on with `ALLOW_UNLIMITED_RISK_FO=true` in `.env`
(`tests/test_fo_refused_by_default.py` checks the default and that both guards fail the tests if removed). Everything
below describes the behavior **when that switch is on**.

Futures (`buy 1 lot NIFTY futures`) and **writing** (selling an option you don't hold) are the two orders whose loss
can exceed what the trader puts in, so they get their own rules, all in code and all tested:

- **Whole lots, from 021's file.** Index and stock futures (219 underlyings in the 10 Oct 2026 file) come from 021's instrument file with the real
  lot size, tick and token (NIFTY: lot 65 in the 10 Oct 2026 file). Nearest expiry unless a date is named; the card
  states it. Carried as NRML, or intraday as MIS. A buy against a short, or a sale against a long, **closes** it.
- **New exposure is capped per order** (`MAX_FO_LOTS_PER_ORDER`, default 2 lots). Only the part beyond what you hold
  counts, so closing a position you hold is never capped.
- **The server demands the typed `I UNDERSTAND`.** A card that opens new futures or short-option exposure carries
  `risk_ack_required`; `POST /api/approvals/{id}/approve` answers `409 ACK_REQUIRED` without the exact words, sends
  nothing, and leaves the card open. It is not just a disabled button on the screen.
- **Never by voice.** A message dictated through the microphone is sent with `via_voice` set by the app, and the
  card builder refuses futures and writing for it ("please type the order"); the model cannot clear that flag.
  Closing what you hold is fine by voice.
- **Paused while you are past a limit you set yourself** (the Discipline limits that start "You set ..."): no new
  futures or short options until you are back inside it, checked again at the click. Closing still works.
- **Facts on the card, never advice:** a short position's loss "can grow without limit" (a written call), or the
  strike less the premium at worst (a written put); the premium is the most a writer can gain; "each ₹1 move changes
  this position's value by ₹N" (futures); the contract value; the expiry; and the SEBI F&O statistic.
- **Margin is not checked, and the card says so:** 021's API reports no margin, so TradeDesk cannot check it and the
  card says "if margin is short, 021 will reject the order". We do not invent a margin figure.
- **021's Futures charges** (from its pricing page): flat ₹20 brokerage, STT 0.05% on sells, NSE 0.00173%, stamp
  duty on buys (the page prints 0.02%; followed as printed), SEBI and IPFT 0.0001%, GST 18% on brokerage, exchange
  charges, stamp duty and IPFT (`app/orders/charges.py`). The card shows contract value and charges, not a
  "total cost", because a future is not paid for in full.
- **Cash estimate:** the live adapter's available-cash estimate does not count an open future's value as spent (a
  closed one counts its gain or loss), so a futures position doesn't make share orders look unaffordable.

Checked: mock tests (cards, short sale, lot caps, closing, flips through zero, charges worked by hand, the server
refusing without the words, no voice/rule/plan, the pause at the card and at the click), fake-021 tests (contract
from the file, `NSEFO`/`NRML` with lot 65, positions, funds), and 13 mutations (each guard removed on purpose: all
caught). In the browser: the futures card, Approve locked until the words are typed, the fill, the position, and a
close-out card with no acknowledgment. Not yet sent to the live sandbox.

## Co-Captain (a second person, only past your own limits)

A **Co-Captain** is someone you trust who must approve an order too, but only when you are past a limit **you set
yourself**. Inside your limits nothing changes: one approval, as before. It is TradeDesk's own feature (021's API has
no Co-Captain), and a person, never the assistant, approves by clicking in the app. **Off by default**
(`COCAPTAIN_ENABLED`); off, nothing in the order path changes. Both people are real accounts (see Accounts): anyone can
invite one other account by its email, and anyone can be invited. Each person keeps their own desk.

- **The zone** (`app/cocaptain/zone.py`, plain arithmetic, no model): an order is "past your limit" when it would take
  you strictly over your orders-a-day, your 20-minute allowance or your daily turnover allowance (the Discipline
  limits), counting the order itself. A limit you did not set is ignored; no profile means never in the zone. A plan
  counts as its steps and the sum of its step values. Cancelling or changing an existing order is never a new order.
- **Past the limit with no Co-Captain, the order is paused** ("Add a Co-Captain in Settings, or wait"), when the card
  is made and again at the click.
- **Two approvals on the same card.** You approve; the card waits ("Waiting for your Co-Captain, Ravi"); they approve
  the **same exact card**, bound to its order hash, account, expiry, rule version and the pairing it was made under
  (`app/cocaptain/store.py`); only then does it go, through the same send path and the same "never twice" log as every
  order. A re-quote makes a new card that starts again. A repeat click changes nothing. The trader, a stranger, an
  unaccepted invitee and a former Co-Captain are all refused with a 404; their decline ends it.
- **It overrides nothing.** After both approvals the order is checked again: your hard stops, Anchor, price band,
  funds, price drift and the broker. A hard stop still voids it. One more look runs right before the broker call, so a
  pairing that ended in the meantime stops it. If you are back inside your limit by then, your Co-Captain's click does
  not send it: you must click Approve yourself on that card.
- **A whole plan is one reviewed object.** A multi-step plan past your limit (the plan counts as its steps and the sum of
  its step values) goes to the Co-Captain as a single review bound to the plan's hash: you approve, it waits, they
  approve the same plan, and only then does it start. Every rule above applies to it. Once it is running, the plan checks
  before each step that the pairing still stands, so ending the pairing stops every step not yet sent, whatever the
  plan's failure policy says.
- **Ending the pairing is immediate**, by either side: it cancels every card still waiting on that Co-Captain, and an
  approval given under an earlier pairing never counts again.
- **The Co-Captain reaches one thing of yours: the cards sent to them.** The only code that touches another user's desk
  is the review route (`app/cocaptain/review_api.py`), and it goes through the review's owner and that owner's own
  approval service. Your portfolio, orders, rules, audit and chat stay on your desk (tested), and Co-Captain events are
  written to the trader's own audit log with the real person who acted.
- **The Co-Captain's desk is told at once.** An invitation, a card or a plan waiting for them, and a pairing that ends are
  pushed over the live feed every desk already has open (one event per person, delivered only to them), so the tab
  updates in a fraction of a second, even in a background tab; a 5-second poll stays as the safety net.
- In the desk: the **Co-Captain** tab (invite by email, remove, accept an invitation, and the inbox of orders and plans with
  Approve and Decline), and a ticket that says up front "after you approve it also needs your Co-Captain", then
  "Waiting for ...".
- **Checked:** zone boundaries and agreement with the existing "You set ..." warnings; the whole two-person flow over
  HTTP between signed-in users; paused with no Co-Captain; decline; cancel; expiry; re-quote; wrong hash; leaving the
  zone while waiting; ending the pairing (also racing an approval, and between the last checks and the broker call);
  a hard stop and Anchor after both approvals; simultaneous approvals sending once; restart with a card waiting; no
  model tool able to approve; GETs changing nothing; the review store's own 18 tests; plans (waiting, decline, cancel, wrong hash,
  leaving the zone, ending the pairing while waiting and after it started, the last look before it starts, a hard stop);
  the live push (an invitation, an order and a plan reach the Co-Captain's feed and no one else's); and 21 mutations (each
  guard removed on purpose, all caught). In the browser: two real accounts, one on `localhost` and one on `127.0.0.1` (separate
  cookies): an invitation sent, accepted in the other account's desk, a past-limit order waiting, approved from the
  Co-Captain's own desk and filled, while the Co-Captain's desk showed none of the trader's orders. A whole plan was then approved the same way: it appeared
in the Co-Captain's inbox within a fraction of a second while that tab was in the background, and ran to COMPLETED after
their click.

**What it is not yet:** the Co-Captain is told over the live feed only while their desk is open; there is no email,
WhatsApp or phone push, so a Co-Captain who never opens TradeDesk will not see a waiting card (it expires after its
normal time and nothing is sent). You can remove your own Co-Captain at once. Closing orders are counted like any other.
Real-world use needs the accounts feature deployed with a real `TRADEDESK_SECRET_KEY` and origins set. It has not been
run against the live 021 sandbox (see "Live sandbox checks" below).

## Orders placed in 021's own app

The trader may also trade in 021's own app, on the same account. Every 5 seconds the backend reads the same order
book (same login, no second session) and labels each order: ours if it is in our send log, otherwise "Placed in
021's app, not by TradeDesk". While one of our own sends is still unconfirmed, nothing is labelled as outside, so
an order of ours whose reply was lost is never mislabelled. Outside orders appear in the desk's **021 app** tab,
are saved to SQLite (`app/history/`), and count toward the Discipline numbers. Checked: sync tests against the mock
and the fake 021, and on the demo build (an order placed straight at the broker appeared in the tab within 5
seconds). Not yet seen with a real order placed in 021's app.

**Live order updates (orders socket).** On the live broker the backend also keeps 021's orders websocket open
(`app/broker/zerotwoone/orders_feed.py`), decoded exactly as the API guide lays out its two packets (NSE, 46 bytes;
BSE, 32 bytes; nine event types). A frame never sets an order's status by itself: as the guide advises, REST is the
source of truth, so each event, and every reconnect (events missed while disconnected are not replayed), wakes the
loops that read `GET /orders`. A fill, rejection or order from 021's app then shows up about a quarter of a second
later instead of at the next poll, even when the market is closed and no price ticks arrive. Events for another
account, unknown codes and short frames are ignored; rejection text is outside text and is never logged or shown.
The 5-second poll stays as the safety net. Checked: decoder tests built from the guide's byte tables, reconnect
with a fresh key and catch-up, and the whole app on the fake 021 (a fill reaches the screen with no price tick);
breaking the watcher or the account check on purpose fails the tests. Not yet run against the live sandbox.

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

## Live sandbox checks still to run

Everything below under "Verified on the live sandbox" was run for real. These were **not**, and are listed in
`LIVE_CHECKS.md` with exact steps, what to expect and a table to fill in: an option buy and sell-to-close; a futures
order (with `ALLOW_UNLIMITED_RISK_FO=true`); an order placed in 021's own app arriving over the orders socket within
about a second; a real sell fill; a rate-limit (429) response; and Hindi voice. Until someone runs them, treat options,
futures, the orders socket and Co-Captain as tested against our simulator and a replica of 021's API only.

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

- Spoken replies and a translated interface (local speech-to-text is now optional, see Voice input).
- Email sign-in recovery (password reset), and telling a Co-Captain by email, WhatsApp or phone push (the live feed
  already tells an open desk at once).

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

## Accounts and security

Sign-up and log-in are real (`app/auth/`). Each account is a separate desk: its own broker session, cards, plans,
rules, send log, Discipline settings, history, assistant memory, audit log and live events.

- **Passwords** are hashed with argon2id (`argon2-cffi` defaults) and are never stored, logged, echoed or returned.
  Minimum 10 characters, not equal to the email, no composition rules. Hashing runs off the event loop.
- **Sessions** are a random 256-bit token in an `HttpOnly`, `SameSite=Lax` cookie (`Secure` unless the app is served
  from this machine, or `COOKIE_SECURE`). The server keeps only the token's SHA-256, with an idle timeout (12 h) and
  an absolute one (7 days). Logging out deletes the row; changing the password or disabling a user deletes all of that
  user's sessions (the device that changed it gets a fresh one).
- **CSRF**: synchronizer token. Each session has a random token, returned in the body of log-in/register/`/api/auth/me`
  and sent back by the page in `X-CSRF-Token` on every request that changes something; the server also refuses a
  request whose `Origin` is not ours. The token lives in page memory, never in storage. Without it, even a valid
  cookie cannot approve an order.
- **Log-in throttling**: 5 failures per email and 20 per IP in 15 minutes lock that key for 5 minutes (even the right
  password is refused while locked). The same words ("Incorrect email or password.") are used for an unknown email and
  a wrong password, and an unknown email still costs a password hash, so neither the message nor the timing says which
  accounts exist.
- **Every route** needs a session unless it is listed in `app/auth/public.py` (health, register, log-in, the generated
  API docs). `tests/test_route_coverage.py` walks the real route table and calls each route anonymously. `/ws` is
  refused before it is accepted without a session or from a foreign Origin.
- **Isolation**: every table that holds trader data has a `user_id` and every store filters by it
  (`app/schema.py`); `executions.client_order_id` is still the global primary key, so an order cannot be sent twice by
  anyone. Approving, rejecting, cancelling or reading another user's card, plan or rule answers 404 (it does not say
  the thing exists). Events are published per user. `tests/test_multi_user.py` and `tests/test_user_scoped_stores.py`
  cover it, and each `user_id` filter was removed on purpose to confirm a test fails (see "Checked" below).

Not protected, so you know: there is no password reset, no email verification and no multi-factor sign-in; anyone who
can reach the server can register; the throttle's counters are in memory and reset on restart; the client IP is the
socket's address (behind a reverse proxy every user looks like one IP); the SQLite file is not encrypted at rest; and
a user with no linked 021 account trades on a simulated account, not a real one (the screen says so everywhere).

Checked: the isolation suites, a database written before accounts existed upgraded to the new shape (rows kept,
primary keys rebuilt, owned by the first account, upgrade twice = no change), and 69 deliberate breaks (63 in the
backend: CSRF, origin, expiry, hashing, lock-out, a `user_id` filter in every store, the hub, the route guards, the
socket, and the 021 linking guards below; 6 in the page's session and broker handling): every one made a test fail. In a browser at 375 px and desktop width: sign-up, wrong password, reload, an order card and its Approve click,
change password, log out, and a second account that sees an empty desk of its own.

## Linking your own 021 account

By default every account trades on a **simulated account** (a mock market with no real money) and the desk says so: a
"Simulated account (not real money)" badge in the header and on every approval ticket. To trade on a real 021 account,
a user opens **Account** (top right of the desk) and links their 021 client id and password.

- **Checked for real, once.** The server logs in to 021 with those details. 021 allows one session per account, so the
  session that checked the login is the one the desk then uses. A refused or unreachable login saves nothing.
- **Stored encrypted.** AES-256-GCM (`app/vault.py`) with a fresh nonce per record and the user's id bound in, so a saved
  login copied onto another user's row will not decrypt. The key is `TRADEDESK_SECRET_KEY` in `.env` (generate one with
  the command in `.env.example`); without it linking is switched off and everything else works as before. To change
  the key, put the old one in `TRADEDESK_SECRET_KEY_PREVIOUS`: saved logins are re-sealed under the new key at startup.
  The password never appears in an API response, a log, an audit event or the page; the API only says "client id ends
  …1234".
- **One 021 account, one TradeDesk user.** A keyed fingerprint of the client id refuses a second link (two logins would
  revoke each other), including the server's own `.env` account.
- **The server's account** (`BROKER=zerotwoone` in `.env`) belongs to the owner (the first account, or
  `TRADEDESK_OWNER_EMAIL`) and is managed there; the owner cannot link or unlink in the app. Everyone else starts on the
  simulated account. A server running the mock (the default) treats the owner like any other user.
- **Switching accounts rejects what is waiting.** Linking or unlinking rejects every pending card and plan (they were
  priced against the other account), closes the user's open pages so they reconnect to the new account's data, and is
  refused while an order's send is unresolved or a plan is running. Rules are kept.
- **Reconnect, not a crash.** If 021 revokes the session (another copy of the app logged in) or a saved login stops
  working, the desk shows a banner with a Reconnect button, and approvals are refused with "Nothing was sent" until it is
  connected (the card stays open). Reconnect logs in with the saved login and checks it with a real read; if 021 still
  refuses it, it says so.
- **Unlink** deletes the saved login and goes back to the simulated account.

Limits: each linked user holds their own 021 session, market socket and instrument list read, so the load grows with the
number of linked users; the log-in attempts to link are throttled (5 failures per user per 15 minutes); the sandbox's own
rate limits (429) have not been exercised.

Checked: tests against the fake 021 (`tests/test_broker_link.py`: encryption, no plaintext in the database file, any
response or log, a wrong or unreachable login saving nothing, duplicates, two users on two separate 021 sessions, pending
cards rejected on link and unlink, open pages told to reconnect, a revoked session refusing card and plan approvals with
nothing sent, a login that fails at startup, the key lost, changed and rotated), and in a browser at 375 px against a
fake 021: the badge and labelled tickets, a wrong then a right login, the badge disappearing, the old card no longer
approvable, the Reconnect banner (and its plain message while 021 still refuses), and Unlink. **Not yet tried against
the live 021 sandbox.**

## Where things are

| Path | What |
|---|---|
| `app/auth/`, `app/identity.py` | accounts, sessions, CSRF, log-in throttling; `current_user` is the one answer to "who is the caller?" |
| `app/workspace.py`, `app/desk.py` | one desk per user (broker, stores, assistant, loops) and the route dependency that picks it from the session |
| `app/schema.py` | every per-user table and the upgrade of older databases |
| `app/vault.py`, `app/broker_links.py`, `app/broker_api.py` | a user's linked 021 login (encrypted), opening their session, and the link / unlink / reconnect routes |
| `app/schemas.py` | Shared data contract (money in integer paise) |
| `app/api_models.py` | REST and WebSocket message types |
| `app/broker/` | `BrokerAdapter` interface, `ReadOnlyView`, `MockBroker` |
| `app/orders/` | charges, hard limits, card builder, approval service, executor |
| `app/llm/` | LLM interface, tools (incl. `portfolio_tools.py`), guards, the stand-in parser, the classic orchestrator |
| `app/agent/` | LangGraph orchestrator and the tool router |
| `app/trace.py` | live trace events for the Assistant tab |
| `app/risk/` | Discipline: profile, goals, the limits guard, daily facts and reports |
| `app/voice/` | voice transcription (Groq, or faster-whisper on this machine) |
| `app/sync/`, `app/history/` | orders from 021's own app; saved daily activity |
| `app/demo.py` | demo controls (demo mode + mock broker only) |
| `app/broker/zerotwoone/` | the 021 adapter: REST, binary market socket, instrument list |
| `scripts/` | live checks against 021, network-failure checks, the real-model prompt check |
| `frontend/src/lib/` | typed API client, live-feed reducer, formatting (tested) |
| `frontend/src/components/` | the desk: chat, approval tickets, account, activity |
| `app/audit.py`, `app/db.py` | SQLite audit log and execution write-ahead log |
