# TradeDesk checkpoint handover

**Implementation stopped at the user's request on 10 October 2026. The whole
roadmap is not finished. This checkpoint is the change set being merged into
internal TradeDesk `main` under the user's subsequent publication request.**

The local/uncommitted/unpushed descriptions below record the original handover
state before that publication request. They describe differences from baseline
`dea6a33`; consult Git for the current publication state. The submission repo and
recording worktree are separate and are not updated by this publication.

Read this file first, then `FEATURE_CHECKLIST.md` for the complete baseline and
TODO inventory, and `ANALYTICS_HANDOFF.md` for calculation details/manual testing.
`IMPLEMENTATION_PLAN.md` and `app/risk/CORE_COMPLETION_HANDOFF.md` describe the
earlier stops/trend batch; they are historical and do not supersede this document.

## 1. Repositories and what is not in main

| Folder | Role | Checkpoint state |
|---|---|---|
| `D:\syrus7\TradeDesk` | Internal repo, `IshanJ9/TradeDesk` | **Continue here**, branch `core-analytics`. Contains yesterday's preserved changes plus today's new work below. |
| `D:\syrus7\Syrus7_Syrus_Core` | Submission repo, `CMPN-CODECELL/Syrus7_Syrus_Core` | Earlier stops/trend changes remain local on `discipline-core-completion`; today's analytics batch has **not** been copied here. |
| `D:\syrus7\Syrus-video` | Separate recording worktree | Intentionally untouched during this implementation. Do not treat it as the development source. |

At checkpoint inspection, both implementation repositories' HEAD and local
`origin/main` references were **`dea6a330c8f9d14b394e8d3e366e86c5f6474f8f`**.
This is a comparison with the existing fetched references, not a fresh remote
fetch. No commits, merges or pushes were made in this implementation task.
All changes below are working-tree changes relative to that baseline; the
remote main branches do not receive them until they are committed and pushed.

**A branch name alone will not transfer these changes to another computer.**
Use the checkpoint backup/files manifest or carefully commit the changed source
files on the current branch, then publish that branch after review. Do not use
`reset --hard`, overwrite the working tree, or blindly stage every untracked file.
Preserve private `.env` and real SQLite databases. The untracked `Microsoft/`
and `frontend/Microsoft/` directories are outside this implementation; exclude
them from the handoff/commit. Never include secrets, tokens, caches or venvs.

Source-only checkpoint backup: `D:\syrus7\.task-backups\2026-10-10-handover-checkpoint`.
It contains the modified tracked files and explicitly selected new feature/docs
files from both implementation repos, plus a SHA-256 manifest. It excludes
private configuration, databases, dependencies and the recording worktree.

## 2. Implemented locally, not part of the recorded main baseline

These are implementation statements, **not claims of live 021 acceptance**.

| ID | Implemented behavior | Principal files | Location |
|---|---|---|---|
| D1 | Opt-in forced cooling-off after a qualifying observed loss streak; persisted deadline, restart/day-rollover survival, exact expiry, default off. Active duration cannot be shortened by editing settings; switching off clears it. | `app/risk/store.py`, `engine.py`, `service.py`, profile UI | Both implementation repos |
| D2 | Opt-in block at the goal's maximum portfolio decline, within the goal date window. Does not add the day's loss again. | `app/risk/engine.py`, `models.py`, forms | Both implementation repos |
| D1/D2 | Preview and fresh approval checks on orders and each plan leg. Cancellation bypasses profile stops; modifications still warn. External 021 orders cannot be blocked by this app. | Risk guard integration, `tests/test_plan_risk.py` | Both implementation repos |
| D3 | Cumulative estimated net P&L line and daily risk bars for the displayed past-day window, exact table and mindful behavior. This is window P&L, not lifetime returns. | `app/risk/report.py`, `DisciplineTrend.tsx` | Both implementation repos |
| A1 | Persist first successful risk observation in each five-minute bucket; average observed risk, sample count, first/latest timestamp, baseline and latest risk. Samples retained for 365 days. Refresh frequency adds no weight inside a bucket. | `app/risk/report_store.py`, `models.py`, `service.py` | Internal only |
| A2 | Up to 30 recorded past-day average net P&L, profitable-day counts/rate, weekday and average-risk-band comparisons, explicit coverage counts. Today is separate. | `app/risk/analytics.py`, `DisciplineAnalytics.tsx` | Internal only |
| A3 | Observed-period percentage return from change in estimated day net P&L divided by the first portfolio snapshot. Positive baseline and five minutes required. No baseline means unavailable; no synthetic backfill. | Analytics/store/UI | Internal only |
| A4 | Symbol/hour order patterns, external-order counts, known filled turnover, intraday share, re-entry/cooling-breach counts and last losing streak. Historical coverage is shown. | Analytics/UI, activity stores | Internal only |
| A4/R4 | Current 20-minute order count and same-clock-window historical comparison; prior windows require five-minute observation coverage. Includes verified external records; rejected orders excluded. | `analytics.py`, `today.py` | Internal only |
| Provenance | New analytics exclude mock/demo and unknown-origin legacy records. Order provenance migration defaults old rows to unknown; a new broker observation identifies the source. Observation buckets are isolated by source. Automatic demo seeding removed. | `app/history/store.py`, `sqlite_store.py`, `app/risk/report_store.py`, `main.py` | Internal only |
| R1 | Optional editable daily filled-turnover allowance with proposed-order soft warning. Other unfilled orders and other plan steps are not falsely represented as filled turnover. | `models.py`, `engine.py`, `DisciplineForms.tsx` | Internal only |
| R2 | Optional charges/filled-turnover warning threshold, with zero-turnover handling. | Same risk files | Internal only |
| R3 | Type `I UNDERSTAND` on own-limit order/plan warnings. The acknowledgment is tied to card ID, fingerprint and warnings, and resets when those change. | `OrderTicket.tsx`, `PlanTicket.tsx`, `lib/limits.ts` | Internal only |
| R4 | Optional explicit 20-minute order-count soft warning, including proposed order/plan preceding legs. | Risk models/engine/today/forms | Internal only |
| G2 | Read-only `get_risk_profile` and `get_discipline` tools with code-written numbers and keyword routing. Risk report replies respect mindful mode for today's P&L. No settings mutations or execution tools added. | `app/llm/tools.py`, `rules.py`, `main.py` | Internal only |
| V2 | Auto/English/Hindi language hint from microphone UI to transcription API and Groq request. Transcript remains editable and Send is required. | `MicButton.tsx`, `lib/api.ts`, `app/voice/api.py`, `service.py` | Internal only |
| Supporting | Tests, generated OpenAPI/TypeScript and prompt documentation, full feature checklist and handoff. | `tests/`, `frontend/openapi.json`, `types.gen.ts`, `PROMPT.md` | Latest artifacts internal only |

The generic risk report/legacy charts still exist alongside the stricter new
analytics. Explicit mock/demo controls are retained. No real history was created
by testing; automated fixtures are confined to isolated test databases.

## 3. Important calculation and behavior limits

- Average risk is an observed-sample average, not a continuously recorded full
  session average. Missing intervals are not filled. Settings can change between
  days, so risk-band comparisons are descriptive, not causal conclusions.
- Percentage returns may cover partial days; arithmetic averaging is not
  compounding or annualization. These are not cash-flow-adjusted investment
  returns. Estimated charges/P&L and approximate FIFO retain baseline limitations.
- New analytics intentionally exclude historical rows whose origin cannot be
  established. Do not mark old records verified just to populate charts.
- Individual fills with unknown average price add no known filled turnover.
  Absent historical order records mean missing coverage, not proof of no trades.
- Existing single-account storage is not an authenticated multi-user system.
  Separate accounts must not share a database. Account isolation is a TODO.
- R1/R2/R4 are warnings. R3 is frontend friction, not a server-side identity or
  dual-approval mechanism. Neither replaces hard-stop enforcement.
- Voice language hints are implemented, but local Whisper, translated UI and
  spoken replies are not. Manual microphone acceptance is outstanding.

## 4. Verification at this checkpoint

- Frontend: **88 tests passed**, TypeScript check passed, production build passed
  after the provenance/type updates.
- An earlier full backend run finished with **1,090 passing and one failing**:
  the tool allowlist test had not been updated for the two new read-only tools.
  That expectation was fixed. This run preceded the final provenance edits.
- The subsequent full backend run progressed without assertion failures until
  it timed out inside `tests/test_prompt_md.py` while launching the prompt
  generator subprocess. **Do not claim a full final backend green run.**
- Changed-area backend run: **382 passed, one failed** because the new legacy
  migration test fixture serialized a computed field. Fixed the fixture to use
  the production `round_trip=True` format; reran the complete history-store
  module: **21 passed**. No production change was needed for that failure.
  The changed-area command covered analytics, history, risk engine/report/today,
  plan risk, voice and chat. This is not a substitute for a full final suite run.
- Generated API/types and `PROMPT.md` were regenerated. The prompt subprocess
  timeout still needs a clean rerun; generation success is not full-suite proof.
  A separate direct render/equality check **passed** on the final prompt content.
- Isolated memory-only mock backend `/api/discipline` and frontend returned HTTP
  200 on temporary ports 8803/5183. No 021 login was used for these checks.
  Both temporary servers were stopped before handover.
- New UI browser/manual acceptance is **not completed**. The previous browser
  debug session was unavailable; no new screenshot claims are made.
- No live orders, real model evals, emails or real historical data generation were
  performed. Prior 9 October test counts/live observations are historical only.
- `git diff --check` passed; Git only reported its existing line-ending notices.

The targeted command used was:

```powershell
& D:\syrus7\.verify-syrus\Scripts\python.exe -m pytest tests/test_risk_analytics.py tests/test_history_store.py tests/test_risk_engine.py tests/test_risk_report.py tests/test_risk_today.py tests/test_plan_risk.py tests/test_voice.py tests/test_chat.py -q --timeout=120
```

## 5. How the next developer should resume

1. Read this document, `git status`, and the saved diff/files manifest. Keep
   `Syrus-video` and private credentials unchanged. Continue in **TradeDesk**.
2. Inspect the pending changes and rerun the relevant tests below. Finish the
   full regression and browser checks before calling this ready to merge.
3. Review and commit the complete source set, including new untracked feature
   files; generated types must travel with the backend changes. Exclude local
   databases, `.env`, module caches and temporary previews.
4. Coordinate the 021 session with the teammate. Only one backend per UCC: another
   login revokes the earlier token. Generate data manually through actual 021
   sandbox activity, then use `ANALYTICS_HANDOFF.md` acceptance steps.
5. Only after review, transfer the same reviewed change to the submission repo
   and reconcile its earlier D1-D3 edits. Do not copy the whole working directory
   over another repo. Do not claim current analytics exist there already.

Environment used:

```powershell
Set-Location D:\syrus7\TradeDesk
$py = 'D:\syrus7\.verify-syrus\Scripts\python.exe' # Python 3.12
& $py -m pytest -q
& $py scripts/gen_prompt_md.py --check
& $py scripts/gen_types.py
```

Do not use the system Python 3.10 for the current LangGraph stack. Backend
dependencies already exist in `.verify-syrus`; frontend dependencies were
installed with `npm ci`. Use Node 22, not the system Node 20:

```powershell
$node22 = 'C:\Users\Rachael Chakraborty\AppData\Local\npm-cache\_npx\52027bd8fc0022aa\node_modules\node\bin\node.exe'
Set-Location D:\syrus7\TradeDesk\frontend
& $node22 node_modules/typescript/bin/tsc -p .
& $node22 node_modules/vitest/vitest.mjs run
& $node22 node_modules/vite/bin/vite.js build
```

For another machine install the equivalent supported Python/Node versions; the
paths above are this workstation's paths. Git may need a per-command
`-c safe.directory=D:/syrus7/TradeDesk`. Do not print or paste `.env` contents.

## 6. Remaining build plan, in dependency order

Everything here is **TODO**, unless explicitly marked as a verification task.
The user expanded scope to include formerly deferred items. No promises about
completion time should substitute for acceptance evidence.

### Phase 0 — stabilize and accept the current batch

- Finish final backend regression, prompt consistency check and mobile/desktop
  checks at 375px and desktop widths. Exercise forms, typed acknowledgment on
  orders/plans, stale-card reset, empty/sparse analytics and mindful mode.
- Verify provenance migration on a copied database, source switching, restart,
  absent baselines and zero portfolio. Never alter the real database for tests.
- Collect actual 021 orders/observations with the user. Test external attribution
  and real fills. Multi-day charts necessarily need real recorded days.
- Reconcile legacy labels/docs (checkbox/automatic demo seeding descriptions are
  historical baseline descriptions, not the new implementation).

### Phase 1 — remaining core graph and voice work

**G1: five graph routes.** Extend `app/agent/router.py` and `graph.py` from
read/act to read/order/rule/plan/risk. Use per-route tool allowlists enforced in
code, not only model prompts. Add bounded model classification only for unclear
messages, validate its enum result, and use a safe clarification/read fallback.
Keep quantities, money, card wording and approval decisions in existing code.
Update traces, visible pipeline and generated prompt diagram. Test every route,
unclear requests, forbidden tool calls, hostile prompts and classic parity.

**G3: durable conversations/checkpointing.** Define conversation IDs, ownership,
retention/deletion and restart behavior. Store serializable messages/state in
SQLite; current graph state includes live service/tracer/context objects and
cannot be persisted blindly. Rehydrate dependencies per request. Separate chat
recovery from execution recovery: never replay a broker send or resume a halted
plan implicitly. Test restarts, concurrent conversations, deletion, stale cards
and isolation. Authenticated ownership depends on Phase 2.

**V1: optional local faster-whisper.** Introduce a provider interface and explicit
`VOICE_PROVIDER` configuration with local model setup documented. Make heavy
dependencies optional. Bound audio size, duration, concurrency and processing
time; support actual cancellation and cleanup. Define fallback consent so local
audio is not silently uploaded to an external service. No audio retention. Keep
language hints and editable-text-only behavior. Test provider failures, timeout,
no automatic download surprises and no voice execution/approval.

**V2 acceptance:** test actual English/Hindi recordings and language selection;
verify transcript correction, highlighted numbers, Cancel and keyboard use.

**S1: 021 orders websocket.** First obtain the actual current protocol/docs and
sanitized frames. Existing binary market socket is not an orders socket. If an
orders stream is available, share the broker session, parse status/partial fills,
deduplicate out-of-order updates, reconnect/resubscribe and reconcile with REST.
If unavailable, document that dependency instead of inventing frames or an API.
Test fake sockets; then user-coordinated real order acceptance.

### Phase 2 — identity, access and conditional Co-Captain

**Authentication/account isolation first.** Login/signup presently only present
UI. Implement authenticated sessions, secure password/identity handling, expiry,
logout and server authorization on every REST and websocket path. Scope broker
credentials/session, profiles, goals, activity, cards, plans, rules, chat and audit
by trader/account. Existing singleton stores require migration/design work.
Add invite acceptance/revocation and distinct trader/reviewer identities. Test
cross-account reads/writes, guessed IDs, revoked sessions and websocket leakage.

**User decision: both approvals only in the overtrading zone.** Define a shared
deterministic predicate from the trader's configured daily order count, daily
turnover allowance and 20-minute activity allowance. Align exact boundaries
with existing warnings, and include the proposed new order/plan step in preview.
Specify treatment of missing thresholds and expose why the zone was entered.
This definition was the implementation assumption stated to the user; the user
explicitly required conditional dual approval rather than approval on every order.

Outside the zone, normal trader approval suffices. Inside it, persist approvals
from **two distinct authenticated users** over the exact card/plan fingerprint,
account, expiry and policy version. Recompute facts at execution. Changed content,
expiry, reviewer revocation or entering the zone invalidates insufficient
approval. Plan legs need the same guarantee; halt for fresh review if conditions
change. Both approvals never override hard stops, locks or broker constraints.
Test self-approval, duplicated clicks, changed cards, zone transitions, races,
restart, missing reviewer and safe cancellation. No approval links should place
orders simply by being fetched; the reviewer must deliberately act in the app.

### Phase 3 — notifications and broader order workflows

**Email notifications (selected channel).** Use existing SMTP credentials in
private environment configuration, disabled until configured. No extra paid
notification service is required; provider quotas still apply. Attach to fired
rules/alerts through a durable outbox with deduplication, bounded retries/backoff,
timeout, safe error status and visible delivery state. Delivery failure cannot
block trading or cause a repeated order. Validate recipients and avoid secrets
or unnecessary account information in email. Test with a fake SMTP transport;
send a real test only to an authorized recipient with user direction.

**Scheduled/time/GTT drafts.** Extend persisted rule conditions to time/calendar
and expiration with explicit IST display. Define missed-fire/restart policy and
single-fire semantics. A trigger creates a fresh reviewable draft; scheduling is
not permission for an unreviewed future order. Reuse limits, TTL, drift checks,
Co-Captain gating and idempotency. Test expired triggers, restart, duplicate ticks,
market closures and unavailable broker reads. Do not claim native broker GTT
support until the actual 021 API is verified.

**Impact preview.** Calculate before/after cash estimate and stock concentration
in code using the existing owned-position/order-sizing logic. Include charges,
pending commitments, plan dependencies and uncertainty. Show estimated versus
known funds clearly. Recompute/reconfirm stale previews; test partial fills,
oversells, insufficient funds and multi-leg proceeds.

**Per-trade risk-to-reward.** Require explicit trader-supplied entry/stop/target
and size. Calculate directional risk/reward and net estimates in code; validate
zero/negative distances and unsupported instruments. Label scenario assumptions;
do not generate targets, imply guaranteed stops or infer ratios for generic orders.

**F&O execution.** Separately verify broker support for instrument identifiers,
lots, quantity multiples, expiries, tick sizes, product/margin and order types.
Current option tools are read-only. Extend sizing/limits/charges/approval guards
only with actual contract evidence. Test invalid lots/expired instruments and
coordinate actual sandbox acceptance. Do not substitute equity sizing rules.

### Phase 4 — experience and integration

**Spoken replies:** opt-in browser/server TTS as explicitly chosen, Stop/mute,
accessible controls, language selection and mindful-mode filtering. No spoken
commands may approve. Test unavailable voices and interrupted replies.

**Regional UI:** introduce translated strings and locale-aware display while
retaining unambiguous tickers, rupee units, dates/IST and exact approval details.
Hindi transcription alone is not localized UI. Test long text/mobile layout and
number/date ambiguity; do not translate internal identifiers.

**PWA:** manifest/icons/install flow and versioned static-shell caching. Do not
cache credentials, private account responses or approval POSTs. Offline mode
must visibly disable sends; never queue an order for later background execution.
Test update activation, offline recovery and stale card rejection.

**Read/propose-only MCP:** authenticated, account-scoped reads/drafts reusing
existing tools/guards. Never expose approve/send/execute. Carry card expiry and
review boundary through responses. Test scope failures, malicious tool input and
cross-account access. Internal tools alone are not an MCP server.

**Graph observability:** add optional native tracing with correlation IDs,
latency/failure metadata and retention controls. Redact secrets and account data;
keep existing in-app trace. Test no-credentials/no-network operation. External
trace ingestion is a separate deployment/configuration choice.

**AgentCore hosting (optional deployment item):** only after core correctness,
identity and explicit hosting priority. Establish deployment/IAM/secrets/storage,
network, cost/rollback and one-session broker constraints. In-process LangGraph
does not require AgentCore; this remains an optional item, not a deployed service.

### Phase 5 — CI, evidence and release

- Add CI for supported Python/Node, backend tests, frontend tests/typecheck/build,
  generated API/prompt consistency and isolated risk guard mutation checks.
- Keep ordinary CI free of real keys, broker logins, orders or billed LLM calls.
  Real-model parity evals should be an explicit protected/manual workflow.
- Final acceptance: real model ID + commit + classic/graph results, actual
  external-order attribution, genuine sell fill/report, real rate-limit recovery,
  applicable opted-in limits and voice/orders-stream checks. Do not manufacture
  losses or describe mocked 429s as live broker evidence.
- Update README, full checklist, generated prompt/API docs and release handoff
  to match final implementation and evidence. Current model-eval script has 32
  cases while older README prose says 31; reconcile when validating.
- Publish reviewed source to the intended branch, then merge/release to both
  intended mains with teammate coordination. The recording folder stays separate.

## 7. Suggested resume prompt

> Continue in D:\syrus7\TradeDesk on core-analytics. Read CHECKPOINT_HANDOVER.md,
> FEATURE_CHECKLIST.md and ANALYTICS_HANDOFF.md first. Preserve all uncommitted
> changes, .env and real databases; do not update Syrus-video. Finish checkpoint
> verification before new features. Use only actual 021 data for product
> analytics; isolated test fixtures are fine. All expanded-scope items remain in
> scope. Email/SMTP is the first notification channel; Co-Captain requires both
> authenticated users only in the configured overtrading zone, never overriding
> hard stops. Do not start another 021 session without coordinating the current
> one. Report exactly what is implemented, tested and still outstanding.
