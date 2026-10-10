# TradeDesk feature checklist

Updated: 10 October 2026 (IST).

Publication note: the user subsequently requested this internal TradeDesk
checkpoint be merged into `main`. The local/unpushed repository descriptions
below are the original checkpoint inventory; CHECKPOINT_HANDOVER.md records the
publication context and outstanding verification. Submission and video folders
are not part of this publication.

This list combines the original LangGraph/voice/risk plan, the later goal and
aggressive-stop additions, the current repository implementation, and yesterday's
unpushed work. `[x]` means implemented in the stated location. `[ ]` means TODO.
An implemented feature is not automatically a completed live acceptance test.

## Repository state

| Folder | Current branch | Remote baseline | Local additions |
|---|---|---|---|
| `TradeDesk` | `core-analytics` | Internal `origin/main`, `dea6a33` | Yesterday's stops/chart preserved here; new analytics, warnings, risk chat and language hints are local and unpushed |
| `Syrus7_Syrus_Core` | `discipline-core-completion` | Submission `origin/main`, `dea6a33` | Yesterday's uncommitted stops/chart, their tests, generated types and handoff |

Both working branches contain the latest remote main after conflict-free
fast-forwards. Internal implementation now continues on `core-analytics`. Nothing was pushed in this task.
Yesterday's 15 local files were backed up outside the repositories and verified
byte-for-byte after pulling. Private `.env` files were not part of the backup or
the feature-list output.

`Syrus-video` is the separate recording worktree. Its earlier pull had already
advanced it to `dea6a33` before the user corrected the target. No further changes
were made there after that correction, and this checklist is not written there.

## Completed in the remote baseline — available in both repos

### Account, broker and order flow

- [x] Account reads: holdings, today's positions, today's/overall P&L, cash estimate and orders; stock search, quotes, option expiries and a read-only option chain. Evidence: `app/llm/tools.py`, `app/broker/zerotwoone/adapter.py`.
- [x] Real 021 sandbox adapter: login, instrument master, REST order operations, binary market-price feed and reconnect/resubscription. Cash is estimated because the integration has no funds endpoint.
- [x] Equity buy/sell drafts, modify/cancel drafts, stop-limit orders, DAY/IOC validity and quantity/rupee/fraction-based sizing in code. F&O execution is not included.
- [x] Exact approval cards with price, quantity, estimated charges, fingerprint and expiry; only the human Approve HTTP route can send an order. Evidence: `app/orders/`, `app/api.py`.
- [x] Approval-time checks for expiry, fingerprint, duplicate clicks, locks, instrument constraints, price drift and opted-in profile limits.
- [x] Write-ahead duplicate-send protection and reconciliation after ambiguous network failures. Unknown order writes are not blindly resent; partial/open/rejected/unknown outcomes remain distinct.
- [x] Multi-step plans with one plan approval, code-based sizing, proceeds dependencies, fill reports and configured halt/continue behavior. Interrupted running plans halt on recovery rather than replaying unsent steps.
- [x] Risk checks for each plan leg at preview and approval; earlier plan legs count toward the daily order allowance.
- [x] Standing price-triggered alerts and conditional order drafts; rule listing/cancellation and persisted rule recovery. A fired order rule prepares an approval card rather than executing automatically.
- [x] Whole-portfolio draft tools: exit losing intraday positions and trim stocks above a trader-specified weight. Code chooses quantities; each request is bounded to six plan legs and discloses that limit.
- [x] Alerts when current holdings fall by a specified daily percentage. Membership is captured from holdings at creation; future holdings are not automatically added.

### Assistant and graph

- [x] Bedrock provider, GPT-OSS configuration and built-in rules provider behind a factory; backend-only credentials. Evidence: `app/llm/bedrock.py`, `factory.py`, `rules.py`.
- [x] In-process LangGraph behind `ORCHESTRATOR=classic|langgraph`, with input guard, router, model/tools loop and output guard. Classic remains selectable; this is not an automatic failover claim.
- [x] Five-route graph (G1, 10 October): router -> read/risk/order/rule/plan nodes, each fixing its tool allowlist in code; out-of-route calls refused; trace, how-it-works diagram and PROMPT.md updated. Keyword routing only: no model classification fallback was added. Real-model eval with five routes on LangGraph, 10 October: 32 prompts, 23 pass, 9 reviewed correct, 0 fail (classic orchestrator not re-run).
- [x] Rule-override refusal, untrusted-text screening, grounded numbers, rupee/quantity ambiguity checks, no unsupported execution claims, no advice, and a bounded tool loop. Evidence: `app/llm/copilot.py`, `grounding.py`, `injection.py`, `app/agent/graph.py`.
- [x] Websocket trace events, Assistant activity view and visible live pipeline with node/tool/guard timing and failures.
- [x] Generated `PROMPT.md` containing the system prompt, tools, settings, fixed code replies and graph description, with a test for code/document consistency.

### Risk profile, goals and discipline

- [x] Five-question onboarding, editable conservative/balanced/aggressive starting presets, explicit review/save and persistent single-trader profile.
- [x] Editable daily order count, single-order portfolio percentage, stock concentration percentage, daily loss percentage, loss-streak threshold, cooling-off duration, re-entry duration and intraday allowance.
- [x] Warnings for those declared limits and behavioral patterns, including same-stock re-entry after a loss. Order size/concentration, intraday allowance and re-entry are warnings rather than forced stops.
- [x] Opt-in hard stops for daily order count and daily loss, checked again at approval. Cancellation bypasses profile stops; modifications receive warnings only.
- [x] Own-limit warning acknowledgment checkbox on both order and plan tickets. Typed confirmation remains TODO.
- [x] Today's arithmetic includes broker orders from app/external sources, filled turnover, estimated charges, estimated realized/unrealized P&L, closed-trade streaks and re-entry/cooling breaches. Rejected orders are excluded from the daily count; fill sequencing uses approximate FIFO.
- [x] Weighted daily risk score (0–100), visible component weights and comparison with the trader's recent usual risk.
- [x] Goal target in rupees or percentage, saved portfolio baseline, end date and maximum acceptable loss; progress, target percentage, days left, weekly amount needed, straight-line pace, headroom and status.
- [x] Goal warning when today's loss plus baseline decline reaches 80% of the allowance; it discloses the possible overlap between those periods.
- [x] Mindful mode hides today's P&L and goal values that could reveal it; Buffett-mode account behavior is separately supported.
- [x] “Was it worth it?” risk-versus-net-P&L scatter plot, above-usual versus other-day totals, profitable-day counts and daily table for up to 30 recorded past days.
- [x] Charges meter: today's estimated charges, share of filled turnover and recorded charges over the last 30 calendar days. A configurable warning threshold is still TODO.
- [x] Clearly labelled deterministic demo history; real history takes precedence for comparisons and demo amounts remain separately identified.

### Voice, external activity, persistence and interface

- [x] Browser microphone recording → backend Groq Whisper transcription → editable chat text with number highlighting. Trader must review and Send; voice cannot approve or execute.
- [x] Voice size/type/time/rate limits, safe errors and backend-only key handling; no retained audio or transcription persistence in the voice service.
- [x] External order discovery through five-second REST polling using the same broker session, app/external source attribution, external-activity UI and shared risk counting. This is not the unimplemented dedicated orders websocket.
- [x] SQLite order activity/daily history, profile/goals, pending cards, plans/reports, rules, audit and execution state. Historical coverage starts when the app records it; prior broker history is not reconstructed automatically.
- [x] Audit view and downloadable NDJSON compliance export.
- [x] Mock/demo controls for price drift, ambiguous sends, broker outages, hostile names, losing positions, external orders and simulated locks. They require mock broker plus demo mode.
- [x] Responsive desk, landing page, how-it-works page with playable graph diagram, login/signup presentation, bundled fonts and system/light/dark themes. Login/signup is UI only; real authentication is TODO.
- [x] Simulated Anchor/Co-Captain lock behavior. Actual second-person approval is TODO; the placeholders do not constitute it.

## Completed yesterday — preserved locally in both implementation repos

- [x] **D1: Opt-in forced cooling-off stop.** `hard_cooling_off` blocks new orders during an observed qualifying pause. The deadline is stored in SQLite, survives restart/day rollover and appears in IST. A reset loss streak does not clear an active pause. Switching it off clears it; editing duration cannot shorten an existing pause. Exact expiry permits new orders. Evidence: `app/risk/store.py`, `engine.py`, `service.py`, `DisciplineForms.tsx`, `DisciplinePanel.tsx`.
- [x] **D2: Opt-in goal maximum-loss stop.** `hard_stop_on_goal_loss` blocks new orders at or above the decline allowed from the saved portfolio baseline, during the goal's date window. It does not add daily loss again. Deposits/withdrawals affect portfolio value. Evidence: `app/risk/engine.py`, `models.py`, profile form.
- [x] **D1/D2 integration:** default off for legacy profiles and presets; checked for individual cards and plan legs at preview and approval. Cancellation remains available; modifications warn only. They cannot block orders placed directly in 021.
- [x] **D3: Risk and reward over time.** Backend-calculated cumulative net P&L line plus daily risk bars; chronological up-to-30-day window, exact-value table, real/demo isolation and missing-score handling. Baseline is zero before the displayed recorded window, not lifetime account P&L. Evidence: `app/risk/report.py`, `models.py`, `frontend/src/components/DisciplineTrend.tsx`.
- [x] Automated boundary, approval, plan, legacy-profile, restart, day-rollover, expiry and timeline tests; regenerated API types; implementation and teammate handoff documents.

Recorded validation on 9 October: **1,071 backend tests**, **80 frontend tests**,
TypeScript checking, production build, and desktop/375px browser checks passed
for that pre-video implementation. These counts are historical, not a claim that
the newly pulled combined tree was fully tested today. Human manual acceptance
and live stop verification remain TODO.

## TODO — integration of the completed local additions

- [ ] **I1:** Manually review D1–D3 in the updated UI, including stop switches, warning/blocked messages, deadline, charts and mindful mode.
- [ ] **I2:** Run the merged-tree regression/type/build checks after the pull; ensure the generated prompt/API artifacts still match the code.
- [ ] **I3:** Commit the preserved local additions and publish them through the intended branch/merge workflow. They are not on either remote main yet.
- [x] **I4:** Copied the preserved D1–D3 implementation into internal TradeDesk before starting this batch. The recording worktree was untouched.

## TODO — remaining core implementation from the agreed plan

### Added on 10 October: daily analytics and trading patterns

- [x] **A1:** Five-minute observations, daily average/latest risk, coverage and bounded 365-day sample retention. Refreshes within a bucket add no weight.
- [x] **A2:** Average daily estimated net P&L, profitable-day rate, weekday and risk-band comparisons with coverage counts. Only identified 021 records enter the new analytics; mock/demo/unknown-origin legacy records are excluded.
- [x] **A3:** Observed-period percentage = change in estimated day net P&L / first recorded portfolio value. Requires positive baseline and at least five minutes; partial periods are labelled. No baseline means unavailable. This is not cash-flow-adjusted or necessarily a full-day return.
- [x] **A4:** Recorded symbol/hour patterns, filled turnover, intraday share, re-entries, cooling breaches, last losing streak and 20-minute pace versus covered historical clock windows. No inference about motives or future performance.
- [x] **A5:** Analytics charts/tables, read-only profile/discipline chat tools and automated coverage tests. Real multi-day acceptance is still TODO; no product history was manufactured.

Implemented locally in this batch: **R1, R2, R3, R4, G2 and the V2 language-hint flow** below.
R1/R2/R4 add optional soft warnings, not new forced stops. R3 requires typing
`I UNDERSTAND` on own-limit order/plan warnings; changing the card/fingerprint/warnings
invalidates acknowledgment. V2 still needs manual microphone testing with English
and Hindi speech. Remaining core implementation: **G1, G3, V1, S1**.

| ID | Feature to implement | Current foundation and remaining work |
|---|---|---|
| R1 | Editable daily turnover allowance in rupees | Filled turnover exists. Add optional trader-selected allowance, profile/form field, preview warning arithmetic and boundary tests. Hard behavior must be explicit and opt-in. |
| R2 | Configurable charges-versus-turnover warning | The ratio meter exists. Add a trader-selected threshold, card/plan warnings and zero-turnover handling; no invented recommended threshold. |
| R3 | Extra typed confirmation for high-friction warnings | Both tickets currently use a checkbox. Add the required typed interaction, define affected warning cases, reset acknowledgment when the approved content changes and test both tickets. |
| R4 | Short-window order pace versus usual | Daily counts/usual risk exist. Add comparable timestamp windows, persisted real-order baseline, current burst facts, warning and UI. Include external orders; disclose insufficient/unrecorded history. |
| G1 | Distinct read/order/rule/plan/risk graph routes/nodes | **Done 10 October** (`app/agent/router.py`, `graph.py`, `tests/test_agent_graph.py`). Keyword rules with a fixed precedence; the optional model classification for unclear messages was not added (a misrouted drafting call is refused and the model asks the trader to rephrase). |
| G2 | Read-only profile/discipline chat tools | REST/UI exist; tools do not expose these reports. Add tools and grounded rendering, mindful-mode behavior and risk-question routing. Profile mutations must stay explicitly reviewed. |
| G3 | Durable conversation state with a SQLite graph checkpointer | Current chat history is in memory and `compile()` has no checkpointer. Add conversation/thread identity, restart recovery, isolation and retention handling. Existing durable cards/plans remain separate; never replay sends. |
| V1 | Local faster-whisper provider and VOICE_PROVIDER selection/fallback | **Done 10 October.** `VOICE_PROVIDER=local` (faster-whisper small, CPU int8), optional `requirements-voice-local.txt`, explicit `scripts/download_voice_model.py` (no runtime download), one clip at a time, 20 s cap, 35 s max audio, `VOICE_FALLBACK=groq|none` with the fallback disclosed in the chat, `/api/voice/status`. Verified with a real recording (WAV and WebM/Opus). Hindi speech not yet tested with a real recording. |
| V2 | English/Hindi language hints | Add language selection/hint through microphone UI, transcription route and providers; verify editable transcript flow with English/Hindi recordings. Auto-detection alone is not completion of this requirement. |
| S1 | Dedicated 021 orders websocket for live fills/external sync | **Done 10 October** against the official 021 API Guide (Orders socket: TC 4 NSE 46 B, TC 8 BSE 32 B, statuses 1-9, no replay). `orders_feed.py` decodes; events and reconnects only wake REST reads (watcher, external sync, reconcile); UCC filter; text never logged; polling kept. Fake-socket and whole-app tests, 2/2 mutations caught. **Live sandbox acceptance still TODO** (needs the coordinated 021 session). |

## TODO — final verification and handoff

- [ ] **T1:** Fresh real Bedrock model parity for the current eval cases under both classic and LangGraph at the final combined commit; retain model ID, commit and results. Script currently has 32 cases; README still says 31.
- [ ] **T2:** Reproducible mutation checks covering every newly introduced guard across risk, plan, graph, voice and sync. Earlier 18/18 risk and 4/4 durability results do not cover all later changes.
- [ ] **T3:** Actual external order created through 021's own app appearing in the external feed with correct attribution and risk counting.
- [ ] **T4:** An actual sell fill and correct resulting report; accepted/cancelled after-hours orders are not proof of a fill.
- [ ] **T5:** Actual sandbox rate-limit behavior (429), recovery and truthful UI results.
- [ ] **T6:** Live acceptance for applicable opted-in limits, without manufacturing real losses to demonstrate them; keep mock boundary checks as separate evidence.
- [ ] **T7:** End-to-end voice/language acceptance and any available orders-stream acceptance after those features are implemented.
- [ ] **T8:** Update README, generated prompt documentation and handoff to match the final merged behavior, test counts and live evidence.

This implementation does not execute live orders, rerun
billable model suites, or claim fresh live validation. The 9 October successful
read-only account/Bedrock checks and the observed 021 401 session failures are
historical operational results, not evidence that all workflows are verified.

## TODO — expanded scope (now included by user request)

- [ ] Real authentication, account/user isolation and permissions; current login/signup is presentation only.
- [~] Co-Captain second-user review with **both approvals required only in the overtrading zone** (10 October: built and tested with demo-only test identities; **needs real accounts**, and whole-plan two-person approval is not built, a plan past the limit is refused instead). See README "Co-Captain". Original spec: Use configured daily order, turnover and 20-minute activity limits; evaluate again at approval, bind both approvals to exact content/expiry, and never override hard stops. Requires authenticated distinct users first. Outside that zone, normal trader approval is enough.
- [ ] Calendar/time-scheduled orders and generalized GTT workflow through the same approval boundary; existing price-trigger rules are only a foundation.
- [ ] Comprehensive before/after cash and stock-concentration impact preview on cards.
- [ ] **Email/SMTP rule notifications** using an existing account (no additional paid notification service). Disabled until configured; durable delivery/retry status and private credentials required. Provider sending quotas still apply. WhatsApp is not the selected first channel.
- [ ] Spoken replies/text-to-speech.
- [ ] Broader regional-language experience/localized UI; explicit en/hi transcription hints are tracked as core V2 above.
- [ ] Installable/offline-capable phone PWA.
- [x] Repository CI (`.github/workflows/ci.yml`): backend pytest, frontend typecheck/test/build, and generated-types drift check on every PR and push to main. Not yet seen running on GitHub. The real-model eval (`scripts/model_eval.py`) is deliberately not in CI: it needs AWS credentials and costs money.
- [ ] Read/propose-only MCP server; internal model tools are not an MCP server.
- [ ] LangGraph-native observability integration; current in-app trace alone does not provide it.
- [ ] Optional AgentCore hosting, only if explicitly prioritized; in-process graph is implemented.
- [x] Option orders (10 October): buy calls/puts in whole lots and sell what is held. Contract from 021's file, NSEFO/NRML on the wire, F&O rows read back, 021 Options charges, factual premium-loss and SEBI notice with typed acknowledgment. Mock, fake-021 and browser tests. **Live sandbox order still TODO.**
- [x] Futures and option writing (10 October), at the user's request: whole lots from 021's file (NIFTY lot 65), 021's Futures charges, new exposure capped per order (`MAX_FO_LOTS_PER_ORDER`, default 2) while closing is never capped, the typed `I UNDERSTAND` **enforced by the server** (`409 ACK_REQUIRED`), refused for dictated messages, rules and plans, paused while past a limit the trader set (at the card and again at the click), factual notices only, and "margin is not reported by 021, so not checked" stated on the card. Mock, fake-021 and browser tests, 13 mutation checks. **No live sandbox order yet; margin rejections from 021 are untested.**
- [ ] Per-trade risk-to-reward ratio with explicit trader-defined downside/upside inputs; never infer these from a generic market order.

Compliance audit export was originally future scope but is already completed
above; do not re-add it as TODO. A human Approve click outside the graph already
provides the intended approval boundary; LangGraph `interrupt()` is not a missing
replacement requirement unless a separate plan-pause feature is requested.

## Sources used

- Original attached “Plan: LangGraph, voice, live trace, risk profiles, three parallel chats,” read on 10 October.
- User's later goal/aggressive-stop/risk-over-time requirements and agreement to complete core first.
- Latest tracked code/README/PROMPT at `dea6a33` in both remotes.
- Preserved local code, tests, `IMPLEMENTATION_PLAN.md` and `app/risk/CORE_COMPLETION_HANDOFF.md` in `Syrus7_Syrus_Core`.

The IDE attachment `d41c79be-6513-42b7-9619-63b12a8498a8/Pasted text.txt` is not
available at its given path. This list uses the readable original plan, the
requirements explicitly pasted in chat, and the existing implementation plan;
it does not claim to incorporate unseen attachment-only requirements.
