# Co-Captain handover — 10 October 2026

## Read this first

Workspace `D:\syrus7\TradeDesk`, branch `cocaptain`, baseline `2b7de11`.
This feature is **unfinished**. Pairing is not an order-authorization gate.
Do not enable it and claim two-person approval works yet. Feature defaults off.
Leave `Syrus-video` alone. Do not push to `syrus`; only Ishan does that.
Do not push or merge this branch without a new instruction. Never force push.
Do not read, print, commit or copy private `.env` values into this document.

The user requested completion of the whole Co-Captain brief, but also explicitly
requested a checkpoint handover before this chat exhausts its allowance.
The earlier broader project inventory is `D:\syrus7\REPO_SYNC_STATUS.md`.
Use it instead of re-auditing unrelated features.

## Confirmed decisions (do not ask again)

1. Entering the zone without an active reviewer blocks the order.
2. Leaving the zone while waiting requires a fresh trader Approve click;
   a reviewer click must never auto-send it.
3. Plans count all new-order steps and the sum of estimated gross order values,
   including both buy and sell sides.
4. Reviewer sees only submitted cards and the numbers quoted in zone reasons.
5. Turnover matches existing warnings: filled turnover plus proposal, excluding
   outstanding unfilled orders. Threshold comparisons are strictly `>`.
6. No configured limits means no zone. Existing saved RiskProfile requires a
   daily order limit; do not redesign that schema without a reason.
7. The predicate currently excludes cancel/modify actions from new-order totals.

## Implemented

### Zone — committed as a072b91

`app/cocaptain/zone.py`: pure `in_overtrading_zone(profile, facts, proposed)`;
`ZoneLimits`, `ZoneDecision`, `proposal_totals`, policy `zone-v1`.
Uses existing TodayFacts: orders_today, turnover, recent_orders. Counts proposal.
Supports single order and whole plan; facts-only reasons; absent limits ignored.
`tests/test_cocaptain_zone.py`: 15 passing tests.
Full verification at that commit: 1,186 backend tests, 95 frontend tests, build.
Mutation daily `>` to `>=` caused two boundary failures; restored original bytes.

### Pairing / identity foundation — check git log for checkpoint commit

- `app/cocaptain/actors.py`: Actor(id, display_name), ActorDirectory protocol,
  dev directory, one current_actor dependency for HTTP/WS; optional injected
  app.state.actor_resolver for Akash's integration. No passwords or sessions.
- Flags in `app/config.py`: COCAPTAIN_ENABLED and COCAPTAIN_DEV_ACTORS default
  false; COCAPTAIN_ACCOUNT_OWNER_ID explicit; COCAPTAIN_DEV_USERS JSON fake
  directory. Dev identities require DEMO_MODE; refused otherwise at startup.
- This is a **single-account dev harness**, not per-user production data:
  the configured owner owns the existing singleton broker/stores. Reviewers
  are denied legacy account/chat/orders/risk/audit/etc APIs by router dependency.
- `app/cocaptain/pairing.py`: durable cocaptain_links, distinct users, one link
  per owner, invite by registered email, reviewer accepts, either party revokes.
  Re-invite after revoke creates a new link ID (approval generation).
  `on_revoke` hook exists but is not connected to approvals yet.
- `app/cocaptain/api.py`: config/settings GETs, invite/accept/revoke POSTs,
  actor-scoped WS. Incoming WS text never approves anything.
- `app/cocaptain/events.py`: bounded actor-specific ReviewHub; no portfolio data.
- `app/main.py`: initializes directory/pairing/hub, enforces configuration,
  registers routes and owner guard on legacy routers.
- `AuditKind.COCAPTAIN` added in schemas. Preserve AuditEvent.actor's existing
  role enum; actual human ID is `data.actor_id`, with owner/reviewer IDs/action.
- API schema generated in frontend/openapi.json and src/lib/types.gen.ts.
- `tests/test_cocaptain_pairing.py`: 15 tests for pairing, identities, privacy,
  GET safety, audit IDs, scoped notifications, persistence/restart.

No settings UI, inbox, approval rows, waiting state, executor gate or plan gate
has been built. Existing approval services are unchanged. README/checklist have
not been marked complete. `.env` has not been edited. No servers started.

## Next implementation sequence

### 1. Durable review object and approval store

Create approvals table with UNIQUE(card_id, role), TRADER/CO_CAPTAIN,
APPROVE/DECLINE, actor ID, exact order_hash, policy version, decided_at.
Bind account/owner, expiry and link generation as well (separate review metadata
table is appropriate). Distinguish order versus plan IDs. Persist across restart.
Requotes create fresh review objects and invalidate old approvals.
Register cards from shared creation paths so rules and LLM drafts cannot bypass
the gate. Registration is not approval. Reviewers may approve first, so discover
in-zone draft cards before the trader's click too.

Potential hook: PendingStore.put / PlanStore.put on first creation, covering
requotes as well as CardService/PlanService. Define ownership explicitly; never
guess a legacy unbound card's owner when real authentication arrives.

### 2. Order gate and final validation

Add AWAITING_CO_APPROVAL to PendingState and PlanState; update transitions,
awaiting selectors, API response types and generated TypeScript types.
Integrate into public ApprovalService.approve, not just a bypassable HTTP wrapper.
Pass Actor explicitly from dependency. Keep default-disabled behavior unchanged.
No Co-Captain means CO_APPROVAL_REQUIRED in-zone. Record trader click, wait with
original expiry. Reviewer must be active reviewer, never owner; exact bindings.
Decline rejects. Duplicate clicks idempotent. Both approval orders must work.

Existing `app/orders/approval.py` claims PENDING -> APPROVED before its first
await, then `_recheck`, then executor. Preserve this concurrency guarantee or
replace it with an equally tested claim protocol; do not introduce an await gap.
Avoid recursive approve -> gate -> approve designs. An internal authorized path
and explicit final fence can reuse checks/executor without HTTP bypass tokens.

After both clicks, re-evaluate zone, expiry, hash/policy/link, locks, risk, market,
drift, fresh price band and funds. Existing `_recheck` does locks/instrument/risk/
target state/drift; funds and price-band coverage needs explicit attention.
Hard stops/locks win and VOID waiting cards with a reason. If out of zone on the
reviewer click, require fresh trader click; never silently send.

Use a final synchronous valid-link/bindings/expiry fence immediately before the
existing executor call, after all awaited broker checks. A revoke is synchronous
and must take effect immediately; do not hold a lock across network waits that
delays revocation. Test ordering of actual broker-send initiation versus revoke.
Per-card async serialization can prevent duplicate clicks; SQLite constraints
and existing execution WAL remain the durable safeguards.

`app/orders/executor.py`: `_begin` writes unique client_order_id before await;
reuse unchanged. UNKNOWN is not success. Do not introduce automatic retries of
unknown submissions. Audit both sent/not-sent outcomes with actor IDs.

### 3. Whole-plan gate

`app/plans/service.py`: approve claims, `_recheck`, then launches `_run` task.
Bind both approvals to plan_hash and total steps/value, not individual leg clicks.
Check link validity immediately before every leg, including after awaited checks.
Revoke halts remaining legs even if failure policy says CONTINUE. Existing
`_recheck_leg` checks locks/suspension; extend without weakening those checks.
Update `_NOT_RUN`/report state handling. Restart keeps awaiting plans; existing
APPROVED/RUNNING recovery halts them. Requotes start entirely fresh.

### 4. Reviewer API / events / rule and tool safety

Add owner-scoped review views and reviewer inbox with exact readback, reasons,
requester, time, expiry and explicit Approve/Decline POSTs. GET must not mutate;
project expired status on reads or use existing background expiration mechanisms.
Do not call mutating legacy report reconciliation from review GET handlers.
Notify only relevant actor via ReviewHub. Email hook only if Akash's feature is
merged; no approval links, no amounts in subject. Rules only draft cards.
Test that app/llm/tools.py cannot approve, decline or invite.

### 5. Frontend

Settings: invite/accept/revoke, status, two factual explanatory sentences,
unconfigured-limit notice. Reviewer inbox full readback/reasons/expiry/buttons.
Trader tickets: preapproval hint, Waiting for [name], Cancel. Support plans too.
375px usable layout. Update describe.ts for every new state and UNKNOWN safely.
Current outcomeOf defaults unrecognized outcomes to sent: fix before introducing
a waiting ExecutionResult outcome. Ticket fallback wording also needs review.

No login UI implementation: use dev actor chooser ONLY if backend advertises
dev mode. API headers use x-tradedesk-actor. Browser WS dev subprotocol pair is
['tradedesk-cocaptain', actorId]. Reviewer WS supports this already; legacy /ws
accept must negotiate the offered protocol if frontend uses it for owner feed.
All owner API calls including voice must carry dev identity consistently.
Real auth should replace current_actor and ActorDirectory, then per-user store
contexts from Akash; do not claim singleton dev harness is real sessions.

### 6. Simulated locks, validation and documentation

Reword app/orders/limits.py and App/AccountPanel/describe simulated Co-Captain
locks so they are clearly demo controls independent of human approval. Remove
the stale 'isn't available yet' only when real gate works. Never bypass the lock.

Tests still required: both click orders, no reviewer, decline, self/wrong/revoked
reviewer, duplicate click, expiry, requote bindings, restart; hard stop, Anchor,
price band, funds after both approvals; simultaneous reviews, review vs revoke,
review vs expiry; plan totals/revocation between legs; GET/tool safety/audit;
frontend behavior/mobile. Existing tests must remain green.

Mutation checks remaining: remove self check, final re-evaluation, hash binding,
revoked-link fence, and every other new safety guard per the brief. For each,
actually mutate code, run test expecting failure, restore source in finally,
record exact result. Zone boundary mutation already done; no need to redo it.

Only after code/test proof update README and FEATURE_CHECKLIST. Explain own
TradeDesk feature (021 has no Co-Captain API), own-limit zone, overrides nothing.
Leave real-session integration unticked until Akash merges and it is retested.
Record final changed files, mutations, remaining decisions in final report.

## Verification commands and constraints

Use PowerShell. Repo .venv is Python3.10 and incompatible with current LangGraph;
working interpreter is `D:\syrus7\.verify-syrus\Scripts\python.exe` (3.12).

Backend: `& D:\syrus7\.verify-syrus\Scripts\python.exe -m pytest -q`
Types: same interpreter `scripts/gen_types.py` with Node22 in PATH.
Frontend cwd `frontend`: npm test, then npm run build, using Node22:
`C:\Users\Rachael Chakraborty\AppData\Local\npm-cache\_npx\52027bd8fc0022aa\node_modules\node\bin\node.exe`
and npm CLI `C:\Program Files\nodejs\node_modules\npm\bin\npm-cli.js`.
Prepend that Node directory to process PATH for child scripts. System Node is20.

Full backend takes about90–100 seconds; report progress while waiting.
Run full backend/frontend/build before EACH commit, focused tests while editing.
One concern per commit; no Co-Authored-By trailer. Use git per-command
`-c safe.directory=D:/syrus7/TradeDesk` if needed. No live broker sends for tests.
If servers are needed use8810/5180; never kill Ishan's8000/5173.
docs/ is ignored; this root handover is intentionally trackable.

## Latest verification

The first pairing full-suite run found one audit-role regression (1 failed,
1,200 passed). Fixed by preserving the role enum and using data.actor_id.
Final rerun results and the checkpoint commit are recorded in
COCAPTAIN_IMPLEMENTATION.md; inspect git status for any later uncommitted work.
