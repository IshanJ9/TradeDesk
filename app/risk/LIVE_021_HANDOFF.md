# Live 021 testing handoff

The submission includes the existing LangGraph/portfolio work and Person C's risk,
goals and Discipline work. Person C's original implementation notes and ready-to-copy
README section are in [HANDOFF.md](HANDOFF.md).

Person C's feature was verified with mock data. A live 021 account was not used in
that verification. This guide is for the teammate doing sandbox integration tests.

After integrating the feature into submission main, all 924 backend tests passed
under Python 3.12.15. Frontend typecheck, all 35 frontend tests and production build
passed under Node 22.23.3. These are local/mock checks, not live 021 execution results.

## Setup

Use Python 3.12 (the integrated verification environment) and Node 22.12 or newer.
The system Python 3.10.0 on the development machine fails when importing the
current LangGraph/Pydantic dependencies. From the repository root, use the Python
3.12 interpreter to create an environment and install requirements:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Only copy the example if you do not already have a private `.env`. Fill that private
file with your own sandbox credentials. Do not commit or share it. The 021 username
is the UCC, not an email address. Use these settings for the first live pass:

```ini
BROKER=zerotwoone
ZEROTWOONE_USERNAME=YOUR_UCC
ZEROTWOONE_PASSWORD=YOUR_SANDBOX_PASSWORD
LLM_PROVIDER=rules
ORCHESTRATOR=classic
DEMO_MODE=false
DATABASE_URL=sqlite:///./live-validation.db
```

This fresh database keeps your actual test history separate from previous demo
data and saved profiles. Only run one app or script for each 021 account: a new
login revokes the previous token. Stop the backend before running the standalone
live-check script, then stop the script before starting the backend.

## First: read-only adapter check

```powershell
.\.venv\Scripts\python scripts\live_check.py
```

This existing script reads instruments, funds, holdings, positions, orders,
quotes and option data; it does not place orders. Review each printed PASS/FAIL
line, rather than treating its exit code alone as proof that every capability
passed. Report the failed capability and sanitized error, without credentials
or complete raw account responses.

## Start the app

```powershell
.\.venv\Scripts\python -m uvicorn app.main:create_app --factory --reload --port 8000
```

In a second terminal, use Node 22.12 or newer:

```powershell
cd frontend
npm ci
npm run dev
```

Open the printed Vite URL. On mobile, choose **Desk**, then **Discipline**.
Use the Vite frontend/proxy for these checks. Existing backend CORS settings do
not include PUT, which the profile and goal forms use; a separately hosted
frontend needs its deployment CORS configuration checked.

## Live test checklist

1. Confirm the account, today's orders and quotes agree with the 021 sandbox.
   GET `/api/discipline` should return a report. With this fresh database and demo
   mode off, it should show no seeded demo history or risk score before onboarding.
2. Complete onboarding, review the suggested numbers, edit and save the profile.
   Hard switches should start off. Confirm the saved values survive backend restart.
3. Prepare an order card without approving it. Check its warnings against your
   saved profile. A preview must not submit an order. Check order-size arithmetic
   uses the card's actual price and quantity and stock exposure uses current values.
4. Verify order counts include the orders from 021's own app, excluding rejected
   orders. Check partial and cancelled fills contribute only their filled quantity
   to turnover and charges. Compare available average fill prices, not limit prices,
   with the report. Record missing-price/cost-basis notes if supplied by the adapter.
5. For a hard-order test, choose a limit below the next order number, enable its
   hard switch, and verify a new preview is blocked. For an approval recheck, create
   a card while the hard switch is off, enable the crossed limit, and approve that
   existing card: expect HTTP 409 BLOCKED and no corresponding new broker order.
   Test daily-loss blocking on live data only if the account has already reached
   that chosen limit. Otherwise its boundary behavior is covered by mock tests.
6. If testing actual sandbox execution, use a test order agreed by the team and
   explicitly approve its card. Check broker order ID, status, fills and report
   totals afterward. The live-check script does not cover submission, modification
   or cancellation. Risk settings must not prevent cancellation; modifications
   receive warnings rather than profile hard blocks. Normal broker/account checks
   can still reject either action.
7. Save a goal and check its starting value matches the account snapshot. Check
   rupee inputs against integer-paise API fields, end date, weekly arithmetic and
   loss headroom. Test mindful mode and removing/replacing a goal.
8. Wait at least 20 seconds and confirm Discipline updates without a page reload.
   Confirm an edited but unsaved profile is not overwritten by a live refresh.
   If the broker is temporarily unreachable, the report should show a refresh
   error and Retry; a guard recheck timeout must send no order.
9. Restart using the same database. Profile, goal and risk-owned day snapshots
   should remain. Current shared activity history is still an in-memory store
   until Person B integrates the persistent store; risk reports retain their own
   persisted snapshots. Historical comparisons need actual past recorded days.
10. After the adapter/risk pass, optionally enable `ORCHESTRATOR=langgraph` and
    `LLM_PROVIDER=bedrock`, supplying the private Bedrock token, region and accessible
    model ID. This separately tests the agent/model integration. Keep a record of
    which provider and orchestrator were used for each result.

## Limits to communicate to judges and reviewers

- Limits are the trader's own choices; presets are editable starting values.
- P&L, charges and FIFO sequencing are estimates from cumulative order snapshots.
  Missing prices and cost bases are disclosed rather than invented. Closed-trade
  loss streaks are before charges. A stock buy warning uses gross exposure and may
  overstate exposure for a buy that closes a short.
- Goal warnings combine today's loss with decline since the goal began and disclose
  that those periods overlap. Goal progress also changes with deposits/withdrawals.
- Multi-step plan legs bypass the profile guard. Orders sent from 021's own app
  count toward today but cannot be blocked here. Check plan execution separately.
- The app currently stores one trader's profile and goal, without user/account
  scoping. Do not reuse one database for different accounts.
- Past data starts when this app records it. Demo days are synthetic, explicitly
  labelled, and not proof of live trading performance.
- Existing mobile desk/tab horizontal overflow remains an integration/layout item.
  The optional order-warning acknowledgment checkbox was not implemented.

## What to send back

Send the tested commit, provider/orchestrator, time in IST, each checklist result,
sanitized failing endpoint/error, and expected versus actual order counts, fills,
portfolio value and P&L. Screenshots should hide account identifiers. Distinguish
read-only adapter checks, mock boundary tests and actual sandbox order tests.
Do not send `.env`, API keys, passwords, session tokens or the local SQLite file.
