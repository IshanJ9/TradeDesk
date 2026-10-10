# Live 021 sandbox checks still to run

Everything here was built and tested against our simulator and a replica of 021's API. None of it has been run against
the real sandbox yet. Run these, fill in the tables, and then change the "not yet sent to the live sandbox" notes in the
README. Nothing here needs code changes; the app's own card -> Approve path does the work.

## Before you start (every check)

1. Put the sandbox login in `.env` only (never in chat or code): `BROKER=zerotwoone`, `ZEROTWOONE_USERNAME`,
   `ZEROTWOONE_PASSWORD`. **021 allows one login per account and logging in revokes every other token**, so stop any
   other copy of the app, `live_*.py` script, or 021 app/web session that uses the same account first.
2. Read-only first: `.venv\Scripts\python scripts\live_check.py` (places nothing).
3. Start the backend and frontend as usual, sign in to TradeDesk, and keep the **Session log** and **021 app** tabs handy.
4. Virtual money, 1 unit or 1 lot at a time. Do these while the market is open; a closed market may refuse or not fill, and
   that is a result worth recording too.
5. After every step, compare what TradeDesk says with what **021's own order book** says. The rule: never a duplicate,
   and never a claim the app cannot prove.

## 1. Option buy and sell-to-close

1. Ask: `buy 1 lot nifty 24500 ce` (use a strike near the money from the option chain: `show me nifty options near the money`).
2. Check the card: the contract name and expiry, **lot size from 021's own file**, the typed `I UNDERSTAND`, the factual
   notice, and charges.
3. Type `I UNDERSTAND`, Approve. Check 021's order book: exchange `NSEFO`, product `NRML`, quantity = one lot.
4. Check the position appears (Account panel shows `Option · 1 lot`, with the live price).
5. Ask: `sell 1 lot nifty 24500 ce` (the same contract). Check the card says it closes the position, needs no acknowledgment.
   Approve and check the fill.

| Item | Expected | What happened |
|---|---|---|
| Lot size on the card equals 021's file |  |  |
| Order in 021's book as NSEFO / NRML |  |  |
| Position shown with a live price |  |  |
| Sell-to-close filled, position gone |  |  |
| Charges on the card vs 021's contract note |  |  |

## 2. A futures order (switch on)

1. In `.env` set `ALLOW_UNLIMITED_RISK_FO=true`, restart the backend. (Leave it `false` afterwards.)
2. Ask: `buy 1 lot nifty futures` (typed, not by voice). The card should show contract value, the "each ₹1 move" fact, the
   "021 does not report margin" line, and need the typed words.
3. Approve with `I UNDERSTAND`. Record exactly what 021 answers: filled, or a margin rejection and its wording.
4. Close it: `sell all my nifty futures` (no acknowledgment should be needed).
5. Also try the same request by **voice**: it must be refused with "type the order".

| Item | Expected | What happened |
|---|---|---|
| Card needs the typed words; server refuses Approve without them |  |  |
| 021's answer (fill or rejection, with its words) |  |  |
| Close-out card needs no acknowledgment, fills |  |  |
| Voice request refused |  |  |
| Futures charges on the card vs 021's contract note (stamp duty looked high: 0.02%) |  |  |

## 3. An order placed in 021's own app arrives within about a second

1. With TradeDesk open on the **021 app** tab, place a 1-share order in 021's own app/web.
2. Start a stopwatch at the click; stop it when the order appears in the tab.
3. Expected on the live broker: about a second (the orders socket). If it takes about 5 seconds, the socket did not wake
   the sync and the polling backstop did; record that and look at the backend log for the orders-socket lines.
4. Change the order's status in 021's app (cancel it) and time that too.

| Item | Expected | Seconds |
|---|---|---|
| New order appears |  |  |
| Cancel appears |  |  |
| Labelled "Placed in 021's app, not by TradeDesk" |  |  |

## 4. A real sell fill

1. Buy 1 ITC (delivery) through the app and wait for the fill.
2. Sell that share through the app. Compare the average fill price, quantity and status TradeDesk shows with 021's order
   book and trade list. Check the cash estimate moves the right way.

| Item | Expected | What happened |
|---|---|---|
| Sell filled at the price 021 shows |  |  |
| Cash estimate after the sale |  |  |

## 5. A 429 (rate-limit) response

There is no safe switch that makes the sandbox answer 429, so provoke it gently and watch:

1. With the app running, load the desk and press refresh about 30 times quickly, or run a short loop of
   `GET /api/account` calls from a signed-in session.
2. The adapter retries **reads** a couple of times on 429/5xx; **writes are never retried**. Expected: the screen stays
   usable, maybe slower; the backend log shows `HTTP 429` retries; no error toast for a read that succeeded on retry.
3. Never test a write this way. If a write ever returns 429, the card must show "not sent"/"not confirmed" and no duplicate
   order may exist in 021's book.

| Item | Expected | What happened |
|---|---|---|
| Retries logged, screen stays usable |  |  |
| No duplicate order after any 429 on a write |  |  |

## 6. Hindi voice

1. Switch the voice language to **Hindi** (or leave on Auto) and say, for example, "पाँच आईटीसी खरीदो" and "इन्फोसिस के दस शेयर बेचो".
2. Check what text appears for you to review (it must appear in the box, never send anything), whether the stock and number
   are right, and the time it takes. The model is Whisper "small", so wrong words are possible: record them.
3. Also try one Hinglish sentence ("5 TCS buy karo at market").

| Item | Expected | What happened |
|---|---|---|
| Hindi sentence transcribed (write what appeared) |  |  |
| Number and stock correct |  |  |
| Seconds to transcribe |  |  |
| Nothing sent without pressing Send, then Approve |  |  |

## When you are done

- Set `ALLOW_UNLIMITED_RISK_FO` back to `false`.
- Paste the filled tables into the team notes, then edit the README sections that say "Not yet sent to the live sandbox".
