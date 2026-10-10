# Handover: real login + multi-user (Akash), durable conversations + email alerts (Ryan)

Each person copies **their own prompt** (the block under their name) into their LLM coding tool,
with the repo open. Part 0 is background everyone reads once. Part 3 says how the two streams avoid colliding.

---------------------------------------------------------------------------------------------------

## Part 0. Background (read once)

**Project:** TradeDesk-AI, an AI trading copilot for the 021 Trade sandbox (SYRUS 7.0, problem PS-04
"Copilot, Not Autopilot").

**Stack:** FastAPI backend (`app/`), SQLite (`app/db.py`, WAL), React 19 + Vite + Tailwind v4 frontend
(`frontend/`), LangGraph agent (`app/agent/`), events pushed to the browser over a websocket (`app/events.py`).

**Run it**
```
# backend tests (about 1,170, must stay green)
.venv\Scripts\python -m pytest -q
# frontend tests + build
cd frontend && npm test && npm run build
# dev servers: backend 8000, frontend 5173 (Ishan runs these already; use 8810 / 5180 for your own)
```

**The rules of the product. These are not negotiable and the judges check them.**
1. **The LLM only reads and drafts. Code owns the money rules.** Anything about limits, charges, who can
   trade, what is sent, is deterministic Python with tests, never a prompt instruction.
2. **Only the human's Approve click sends an order.** Nothing else (not a rule, not a voice command, not an
   email, not a scheduled job, not a notification) can approve or send. An email must never contain an
   approve link or button.
3. **No investment advice, no predictions** (SEBI). Alerts, emails and chat state facts the user asked for
   (prices, their own numbers, what happened). Never "you should", "likely to", "good time to".
4. **Every claim in README.md / FEATURE_CHECKLIST.md must be backed by code and a test.** Do not write a
   feature into the README until it works and is tested. Do not invent features nobody asked for.
5. **Secrets live in `.env` only.** Never in code, tests, logs, chat, commit messages, or API responses.
   Never print `.env`. `.env` is gitignored; keep it that way. Add new variables to `.env.example` with
   empty values.
6. **Failure must be visible and safe.** If something is unknown, say so ("not confirmed"); never show it
   as success. A side feature failing (email, history) must never block or delay trading.

**Working rules**
- Work on your own branch. **Do not push to `syrus`. Only Ishan pushes to Syrus** (`git push syrus ishan:main`).
- No `--force`. No commits to `main`.
- **Commit messages must not have a `Co-Authored-By` line** (team rule, even if your tool adds one by default).
- One concern per commit. Run the full backend and frontend tests before each commit.
- Use mutation checks: after writing a guard (an isolation check, a dedupe check), break it on purpose and
  confirm a test fails. A test that cannot fail proves nothing.
- Match the surrounding code style: small modules, a store class per table group, plain-English comments.
- `docs/` is gitignored. Put project docs at the repo root or next to the code.
- If a requirement below is unclear, or you hit one of the "ask Ishan first" points, stop and ask. Do not guess.

**How the code is laid out today (verify with grep, it may have moved)**
- `app/main.py`: builds everything at startup (`create_app`), wires stores, the broker, the copilot, background loops.
- `app/api.py`, `app/risk/api.py`, `app/sync/api.py`, `app/voice/api.py`: REST routes. `/ws` is the websocket.
- `app/db.py`: one SQLite connection and the base schema (`audit_events`, `rules`, `executions`). Other stores
  create their own tables with `CREATE TABLE IF NOT EXISTS` (`pending_cards`, `plans`, `plan_requests`,
  `plan_reports`, `activity_orders`, `activity_days`, `risk_*`).
- `app/llm/copilot.py`: the `Copilot` and its in-memory `_history` (`HISTORY_LIMIT = 12`). `app/agent/graph.py`
  is the LangGraph version (`ORCHESTRATOR=langgraph`).
- `app/rules/`: standing rules (alert, trigger). `app/risk/`: risk profile, cooldown, daily limits.
- `app/events.py`: `EventHub.publish(...)` to every connected browser.
- `app/config.py`: `Settings.from_env()`. All configuration goes here.
- `frontend/src/brand/Login.tsx`: the login page. **Today it is UI only**: nothing typed is checked, stored or
  sent, and the page says so. `frontend/src/lib/router.ts`, `api.ts`, `ws.ts`, `store.ts`, `useChat.ts`.
- Read `README.md`, `FEATURE_CHECKLIST.md`, and `app/risk/*HANDOFF.md` before you start.

---------------------------------------------------------------------------------------------------

## Part 1. AKASH: real login and multi-user

### Prompt to paste into your LLM

````
You are working in the TradeDesk-AI repository (FastAPI + SQLite + React). Read HANDOVER_AKASH_RYAN.md
Part 0 first and obey every rule in it: the LLM only drafts, only the human Approve click sends, no advice
or predictions, no secrets in code or chat, no Co-Authored-By line, work on your own branch, never push to
the syrus remote.

YOUR TASK: replace the fake login with real authentication, and make the app multi-user so two people can
use it at the same time without seeing or affecting each other.

WHERE THINGS STAND TODAY
- The app is single-user. Nothing knows who is calling. The login page (frontend/src/brand/Login.tsx) is UI
  only: it discards what is typed and opens the desk. Its comments and on-page text say so.
- One process holds one broker session (one 021 account, credentials in .env), one Copilot with one in-memory
  chat history, one set of risk-profile rows (tables keyed `id = 1`), one EventHub that broadcasts every
  event to every websocket, one shared SQLite file. Tables include audit_events, rules, executions,
  pending_cards, plans, plan_requests, plan_reports, activity_orders, activity_days and the risk_* tables.
  Grep for `CREATE TABLE` to get the full list; do not trust this list.
- 021 allows ONE active login per 021 account, and logging in revokes the other tokens. So two TradeDesk
  users cannot share one 021 account. Each user needs their own.

REQUIREMENTS (build all of these, in this order)

1. Accounts and sessions
   a. A `users` table: id (uuid), email (unique, lower-cased), password_hash, created_at, last_login_at,
      disabled flag. Email is the login name.
   b. Sign-up and login endpoints under /api/auth: register, login, logout, me. Register requires accepting
      the "no investment advice" notice (the UI already has this checkbox; the server must record it with a
      timestamp and refuse if it is missing).
   c. Hash passwords with argon2id (preferred) or bcrypt. Never store, log, echo or return a password or hash.
      Minimum password rules: 10+ characters, not the email. Do not add composition rules.
   d. Session by a random opaque token in an HttpOnly, SameSite=Lax (Secure when not on localhost) cookie.
      Store only a hash of the token server-side, with expiry (idle and absolute) and the user id.
      Logout deletes the session. Changing password or disabling a user kills all their sessions.
   e. CSRF protection for every state-changing request (double-submit token or Origin check; explain which
      and why). The Approve endpoint is the most important one to protect.
   f. Login rate limiting: per email and per IP, with a short lock-out, and a response that does not reveal
      whether an email exists (same message and similar timing for "no such user" and "wrong password").
   g. A FastAPI dependency `current_user` that every non-public route uses. Public routes are only: health,
      register, login, and static files. Everything else returns 401 without a session. List the public
      routes explicitly in one place and add a test that fails if a new route is added without auth.

2. Per-user data (the largest part: be systematic)
   a. Add `user_id` to every table that holds user data, and filter every read and write by it. Go through
      every store class; do not rely on memory. Where a table is a singleton today (`id = 1` in the risk
      tables), make the key (user_id) instead.
   b. A migration for existing data: on first start after the change, existing rows are assigned to the
      first user who registers (or a configurable owner), so Ishan's current data is not lost. Idempotent.
   c. Pending approval cards, rules, plans, executions, activity history, risk profile, cooldowns, daily
      counters, audit log: all per user. The audit log records the user id on every event.
   d. The Copilot object and its chat history become per user (keyed by user id, created on demand, with a
      cap on how many are kept in memory). One user's messages must never reach another user's model call.
   e. The EventHub: websocket connections are authenticated (the cookie is sent on the upgrade request;
      reject unauthenticated sockets) and events are delivered ONLY to the owning user's sockets. Add a
      `user_id` to every event that is published. A user with two tabs gets both.
   f. Background loops (price ticker, reconcile, external-order sync, order-event watcher, rule engine)
      currently assume one account. Make them iterate over active users' broker sessions, or run per user.
      A stuck or failing user must not stall the others.

3. Per-user broker (021) credentials: ASK ISHAN BEFORE BUILDING THIS PART
   Propose a short design (a half page) and get it approved first, because it handles the most sensitive data.
   The expected design:
   a. After signing in, a user "links" their 021 account by entering their 021 username (UCC) and password in
      a settings screen. The server validates by logging in to 021 once.
   b. Store them encrypted at rest (for example Fernet/AES-GCM) with a key from `.env` (TRADEDESK_SECRET_KEY),
      never in plaintext, never in logs, never returned by any API (the API only says "linked: yes, UCC ends
      in ...1234"). Provide "unlink" which deletes them.
   c. Each user's 021 session is separate. Handle "021 revoked my token because I logged in elsewhere" as a
      visible "reconnect" state, not a crash.
   d. In mock mode (BROKER=mock, used for demos and tests) every user gets their own independent mock
      account. No credentials needed.
   e. A user who has not linked 021 can use the app against the mock account only, clearly labelled.
   f. Keep the existing .env 021 credentials working as the default/fallback for single-user mode, so the
      project still runs the way it does now with one user.

4. Approval safety (must be proven by tests)
   a. A pending card belongs to one user. User B approving User A's card id gets 404 (not 403, do not
      confirm it exists) and nothing is sent.
   b. The Approve endpoint requires a valid session, a valid CSRF token, and the card's owner = caller.
   c. Rules created by user A only ever create cards for user A.
   d. Idempotency is unchanged: client_order_id stays the primary key of `executions`; a double-click or a
      retry still cannot send an order twice. Do not weaken this.
   e. Daily limits, cooldowns, Anchor and every risk rule are evaluated per user.

5. Frontend
   a. Rewrite frontend/src/brand/Login.tsx to call the real endpoints. Remove the "UI only / nothing is
      stored" comments and on-page wording. Show real errors (wrong password, rate limited, email taken).
   b. Route guard: the desk needs a session; otherwise redirect to login. Landing and "How it works" stay public.
   c. Logout button on the desk. On a 401 anywhere, clear state and go to login.
   d. api.ts sends the CSRF token and `credentials: "include"`; ws.ts connects with the cookie.
   e. Settings screen for "link my 021 account" (part 3) and "change password".
   f. Keep phone layout working at 375 px wide (there was an overflow bug before; do not reintroduce it).
   g. Do not change the meaning of any trading screen or approval card.

6. Tests (these are the proof; budget as much time on them as on the code)
   - Unit: hashing, token hashing, expiry, rate limiting, CSRF.
   - Isolation (the key ones): two users, same instrument, same time. Assert that user B cannot read, list,
     approve, cancel or receive events for user A's cards, rules, plans, orders, activity, risk profile,
     chat history, and audit rows. One test per store.
   - A route-coverage test that enumerates app.routes and fails on any unauthenticated non-public route.
   - Websocket: no cookie = rejected; user B's socket never receives user A's event.
   - Migration test: old single-user database -> upgraded -> data owned by the first user, run twice = same.
   - Mutation checks: remove a `user_id` filter in a store, confirm an isolation test fails. Do this for at
     least five different stores and report the results.
   - Existing 1,170 backend and 95 frontend tests keep passing (update fixtures to log in, do not delete tests).

7. Documentation (only after it works)
   - README: replace the "login is UI only" statements with what is now true, and a "Security" section
     (hashing, cookie flags, CSRF, rate limit, encryption of 021 credentials, what is NOT protected).
   - FEATURE_CHECKLIST.md: tick only what is tested. Add `.env.example` entries (TRADEDESK_SECRET_KEY, etc.).

OUT OF SCOPE (do not build): OAuth/Google sign-in, password reset by email (Ryan is building email; leave a
clearly marked hook), roles/admin, the "Co-Captain" second-approver feature, teams, billing.

DEFINITION OF DONE
- Two browsers, two accounts, one backend: each sees only its own desk, cards, rules, chat and live events.
- Unauthenticated calls to any API or websocket are refused.
- Approving someone else's card is impossible and tested.
- Full backend and frontend suites pass, `npm run build` passes, phone layout verified at 375 px.
- No secret appears in the repo, the logs, or any API response (grep for it).
- README and FEATURE_CHECKLIST say exactly what is true.

Work in small steps. After each numbered requirement, run the tests and give a short status. At the end
produce: a list of every file changed, the isolation-test and mutation-check results, and any decision that
needs Ishan's approval.
````

---------------------------------------------------------------------------------------------------

## Part 2. RYAN: durable conversations and email alerts

### Prompt to paste into your LLM

````
You are working in the TradeDesk-AI repository (FastAPI + SQLite + React). Read HANDOVER_AKASH_RYAN.md
Part 0 first and obey every rule in it: the LLM only drafts, only the human Approve click sends (an email
can never approve anything), no advice or predictions, no secrets in code or chat, no Co-Authored-By line,
work on your own branch, never push to the syrus remote.

YOUR TASK: two features. (A) Durable conversations: the chat survives restarts and page reloads.
(B) Email alerts: the user gets factual emails about things that happened. Build A first, then B.

COORDINATION WITH AKASH
Akash is adding real login and per-user data at the same time. To avoid a painful merge:
- Every new table you create has an `owner_id TEXT NOT NULL DEFAULT 'local'` column, and every store method
  takes an `owner_id` argument (default 'local'), and filters by it. Akash will swap 'local' for the real
  user id. Do not build your own user concept.
- Create tables inside your own store class with `CREATE TABLE IF NOT EXISTS` (the pattern in app/plans/store.py
  and app/pending.py). Do not edit the base SCHEMA in app/db.py unless unavoidable.
- Do not touch the login page or auth code. Where you need the user's email address, read it from a setting
  you own (see B3) and leave a clearly marked TODO to use the account email later.

==========================================================================================
PART A. DURABLE CONVERSATIONS
==========================================================================================

WHERE THINGS STAND TODAY
- app/llm/copilot.py: `Copilot._history` is an in-memory list of Messages, capped at HISTORY_LIMIT = 12, lost
  on restart. The LangGraph version (app/agent/graph.py) shares the same history behaviour.
- The frontend chat (frontend/src/lib/useChat.ts, components/ChatPanel.tsx) keeps its messages in React state,
  so a page reload empties the chat.
- The history is also used for safety: the copilot's number-grounding check (app/llm/grounding.py) and the
  injection scan (app/llm/injection.py) look at recent text. A reply that contained injected instructions is
  stored as "[reply withheld from history: ...]" instead of the text. Preserve that.

REQUIREMENTS
A1. Storage. Tables `conversations` (id, owner_id, title, created_at, updated_at, archived) and
    `conversation_messages` (id, conversation_id, seq, role user|assistant, text, created_at, plus the minimum
    extra fields needed to redraw the chat: for example the id of an approval card or plan the reply produced,
    and whether the reply was a guard refusal). Store the same safe text the model history stores today.
A2. What is NOT stored: raw tool outputs, the system prompt, anything flagged by the injection scan (store the
    placeholder), voice audio, passwords/tokens, the 021 session. Keep an explicit test for each.
A3. Write path. After each completed turn, persist the user message and the assistant reply in one transaction.
    A crash mid-turn leaves no half conversation. A persistence failure must be logged and must NOT break
    the chat reply (the user still gets the answer; the UI shows a small "this message was not saved" note).
A4. Read path. On load, the app restores the active conversation: the last N messages (HISTORY_LIMIT) are
    loaded into the Copilot's history so the model has context again, and the full visible conversation is
    returned to the UI (paginated, newest last; load older on scroll).
A5. STALE NUMBERS RULE (important, a safety requirement). Numbers in restored messages (prices, balances, P&L)
    are old. They must never count as "grounded" for a new answer. Make sure the grounding check only treats
    numbers from THIS turn's tool results as valid, and add a test that restores a conversation containing
    "INFY is at 1,500" and proves the copilot refuses to repeat 1,500 as current without a fresh tool call.
    In the UI, show restored messages with their timestamp so it is obvious they are old.
A6. Conversation management API and UI: list conversations (title, last message time), start a new one,
    switch, rename, archive, delete. Auto-title from the first user message (truncate, no model call needed).
    Delete is permanent and asks for confirmation. One conversation is "active" at a time per owner; each has
    its own Copilot history. Never mix history between conversations.
A7. Pending approval cards survive a reload already (app/pending.py). A restored chat message that refers to
    a card must show its CURRENT state (open / expired / sent / declined / re-quoted), never draw it as
    approvable if it is not. Only the live card component can be approved.
A8. Limits: max messages per conversation kept in the database (for example 2,000, oldest dropped with the
    UI saying so), max length per message, max conversations per owner. Configurable in app/config.py.
A9. Audit: keep audit_events as it is. Conversations are separate from the audit log; deleting a conversation
    must not delete audit rows.
A10. Tests: round trip; restart (new app object on the same database file) restores history; crash safety;
    injection placeholder survives; stale-number rule (A5); cross-conversation isolation; owner_id isolation;
    deleting; pagination; persistence failure does not break the reply; frontend reload keeps the chat.
     Mutation checks: remove the owner_id filter, remove the stale-number rule, remove the placeholder;
     confirm tests fail each time and report it.

==========================================================================================
PART B. EMAIL ALERTS
==========================================================================================

WHAT TRIGGERS AN EMAIL (a fixed list: nothing else may send mail)
 1. An ALERT rule fired (app/rules/engine.py): "Alert: INFY crossed 1,500."
 2. A rule fired and made an approval card: "A card is waiting for your approval. Nothing has been sent."
 3. An order the user approved reached a final state: filled, partly filled, cancelled, rejected, or
    UNKNOWN ("outcome not confirmed, check 021"). UNKNOWN is never worded as success.
 4. A risk event: the daily loss limit was reached, trading was paused by a cooldown, the Anchor guard
    engaged (see app/risk/).
 5. An approval card expired unapproved (optional, off by default).
 6. A test email (user presses "Send test email").

HARD CONTENT RULES (code-enforced and tested, not prompt-enforced)
 - Plain facts only: what happened, the numbers from the system, the time. No advice, no prediction, no
   "consider", no "good time", no recommended action beyond "open TradeDesk to review".
 - NEVER an approve/confirm link or button. The email only links to the desk, which requires the user to be
   there. A fired rule's email must say "Nothing has been sent to your broker."
 - Subject lines contain no account numbers, balances, or P&L. Body: keep sensitive amounts to what the
   alert is about; never include passwords, tokens, 021 session ids, or full account identifiers.
 - Templates are fixed text with fields filled from system data. The LLM does NOT write emails and is not
   called to compose them. Add a test that the module imports nothing from app.llm.
 - Include the footer: "TradeDesk gives information, not investment advice. Reply is not monitored."

REQUIREMENTS
B1. Config (app/config.py, `.env`, `.env.example` with empty values): EMAIL_ENABLED (default false),
    SMTP_HOST, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD, SMTP_STARTTLS (default true), EMAIL_FROM. Credentials
    are `repr=False` fields like the existing ones. If disabled or unconfigured, the app runs normally and the
    settings screen says "email is not configured on this server". Use the standard library (smtplib in a
    thread executor) or aiosmtplib; justify the choice. Do not add a paid email service dependency.
B2. Transactional outbox. Table `email_outbox` (id, owner_id, kind, dedupe_key UNIQUE, to_address, subject,
    body_text, body_html nullable, status PENDING|SENT|FAILED|SUPPRESSED, attempts, next_attempt_at,
    created_at, sent_at, last_error). The event handler only INSERTS a row (cheap, no network). A separate
    background worker sends. This guarantees: no email lost on restart, no email sent twice, no network call
    in the trading path. Follow the same idea as rules' `delivered` flag (app/rules).
B3. Preferences. Table `email_settings` per owner: address, address_verified, and one on/off flag per
    trigger kind above. Defaults: 1-4 and 6 on once an address is verified; 5 off.
    Verification: when the address is set, send a message with a 6-digit code (expires in 15 minutes, 5
    tries, then a cooldown); alerts are only sent to verified addresses. Store the code hashed.
    Mask the address in API responses (r***@gmail.com).
B4. Dedupe. dedupe_key is derived from the event (for example `rule-fired:{rule_id}`,
    `order-final:{client_order_id}:{status}`), so a replayed event, a restart, or a double publish yields
    exactly one email. Test it by publishing the same event twice and across a restart.
B5. Retry and failure handling. Exponential backoff (for example 1m, 5m, 30m, then FAILED after 5 tries).
    A permanent SMTP error (bad recipient) goes straight to FAILED. A failing email server must not slow
    the app: sends are time-limited (10 s) and run outside the event loop's critical path.
B6. Rate limits: at most N emails per owner per hour (configurable, default 20); beyond that, SUPPRESSED rows
    are written and ONE summary email is sent ("12 more alerts were held back; open TradeDesk"). A rule
    that fires repeatedly must not spam.
B7. Do not email during demo mode (DEMO_MODE) unless explicitly enabled, so demos never mail real people.
B8. API and UI. REST: get/set email settings, send test email, verify code, list recent outbox rows (status
    only: kind, time, SENT/FAILED, never the body), resend a FAILED one. Frontend: a small "Email alerts"
    panel (address, verify, toggles per kind, test button, last 10 deliveries with status). Plain language.
    Must work at 375 px width. In the chat, when a rule is created the assistant may say whether an alert
    email is set up (state fact: "Email alerts are off" / "on"), nothing more.
B9. Hooking events in without risking the trading path: subscribe to the existing EventHub or the rule/
    execution/risk service callbacks; each hook is wrapped so an exception in email code is caught, logged
    (without addresses or message bodies) and never propagates into order sending. Test: make the email
    module raise, place an order, confirm the order is still sent.
B10. Privacy and logging: never log email bodies, codes, SMTP passwords or full addresses. Audit-log
     "email queued/sent/failed" with kind and outbox id only.
B11. Tests: use a fake SMTP server (aiosmtpd or a stub) so no real mail is sent in tests. Cover: each trigger
     produces the right text; forbidden-words test over every template (advice/prediction vocabulary list,
     no URLs containing "approve"); dedupe; retry and backoff with a fake clock; rate limit and summary;
     unverified address gets nothing; verification code expiry and attempt limit; disabled = no sends;
     email failure does not affect an order (B9); owner_id isolation; outbox survives restart.
     Mutation checks: remove dedupe, remove the verified check, remove the try/except in the hook; confirm
     tests fail each time and report it.

DOCUMENTATION (only after it works)
 - README: describe exactly what exists (which events send email, what is never in an email, how to
   configure SMTP, the limits). FEATURE_CHECKLIST.md: tick only what is tested.

OUT OF SCOPE: SMS/WhatsApp/push notifications, letting an email approve or reject anything, email-based
login or password reset (Akash's area; leave a note), scheduled digests or market-news emails, any
LLM-written email, per-user accounts (use owner_id='local').

DEFINITION OF DONE
 - Part A: reload the page or restart the backend and the conversation is back, with the old numbers clearly
   old and never reused as current; conversations can be created, switched, renamed, archived, deleted.
 - Part B: with a real SMTP account in .env (Ishan will supply, you will not), a fired alert, a filled order
   and a daily-loss stop each send exactly one email; none contains advice, a prediction or an approve link.
 - All backend (1,170+) and frontend (95+) tests pass, `npm run build` passes, 375 px layout checked.
 - Mutation-check results reported. No secret anywhere in the repo or logs.

Work in small steps: finish and test A before B. After each numbered requirement run the tests and give a
short status. At the end produce a list of files changed, the mutation-check results, and any decision that
needs Ishan's approval.
````

---------------------------------------------------------------------------------------------------

## Part 3. How the two streams fit together (for Ishan and both of you)

- **Merge order:** Ryan's work first (it is mostly new tables and new modules), then Akash's (which touches
  every store and will convert `owner_id='local'` into the real user id). Ryan's `owner_id` columns make
  that conversion mechanical.
- **Files both may touch:** `app/main.py` (startup wiring), `app/config.py`, `frontend/src/App.tsx`,
  `README.md`, `FEATURE_CHECKLIST.md`. Make small edits there, pull often, resolve conflicts carefully.
  Do not reformat files you do not own.
- **Email address:** until Akash's accounts exist, Ryan's address comes from Email settings. After login
  exists, Akash adds the account email as the default there (a one-line change).
- **Password-reset email:** deliberately postponed. When both are merged, it can be added using Ryan's
  outbox as a new trigger kind.
- **Ishan's live checks after merge:** two browsers with two accounts; reload the chat after a restart;
  one real alert email; confirm the Approve click is the only thing that sends.
- **Order of risk if time is short:** Akash part 1+2+4 (real auth and isolation) is the minimum that makes
  "multi-user" honest; part 3 (per-user 021 credentials) can ship later. Ryan's A before B.
