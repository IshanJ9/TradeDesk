# Core completion plan

Historical plan from the earlier stops/trend batch. For current completed work,
expanded scope and publication context, use CHECKPOINT_HANDOVER.md and
FEATURE_CHECKLIST.md; the statuses below were recorded before the analytics batch.

Scope agreed: complete the core plan; future scope remains backlog.
Base: submission repository main 0e3a834. Working branch: discipline-core-completion.
Do not send real orders during verification. Keep credentials outside version control.

## Delivery checklist

- [x] D1 Forced opt-in cooldown after configured consecutive losses. Exact expiry permits orders; preview and approval checks, including plan legs. Cancellation bypass; modifications warn. Existing profiles default off.
- [x] D2 Opt-in goal maximum-loss stop. Compare current portfolio against saved goal baseline during goal dates, without double counting daily loss. Exact threshold blocks. Existing profiles default off.
- [x] D3 Cumulative net P&L and daily risk chart. Chronological last 30 recorded past days; real/demo isolation; null-score handling; accessible figures; clearly disclose window baseline and gaps.
- [ ] R1 Optional daily turnover allowance in rupees, with warnings and boundary tests.
- [ ] R2 Optional charges/filled-turnover warning threshold, with zero-turnover handling and UI.
- [ ] R3 Typed confirmation for own-limit warnings on both order and plan tickets.
- [ ] R4 Short-window order pace compared with recorded historical windows; disclose insufficient data and include external orders.
- [ ] G2 Read-only profile and discipline chat tools, grounded responses and mindful-mode handling.
- [ ] G1 Distinct graph read/order/rule/plan/risk routes; preserve approval boundary and safe routing.
- [ ] G3 Durable conversation/thread state and restart/isolation tests; never replay approvals.
- [ ] V1 Explicit voice provider selection and optional local faster-whisper provider; bounded work, no audio retention.
- [ ] V2 English/Hindi language hint from microphone UI through route/provider, editable transcript retained.
- [ ] S1 Verify actual 021 orders websocket protocol before implementing parsing/reconnect; keep REST reconciliation. Protocol availability is a dependency, not grounds for inventing a wire format.
- [ ] T2 Regression and guard mutation checks with reproducible evidence.
- [ ] T1 Real-model parity and live 021 checks: record exact model/commit/results separately from mock verification. Requires configured credentials and live environment.

## Verification and handoff

For each completed item record implementation, tests and limits below. Regenerate API types for model changes. Run backend regression, frontend tests/typecheck/build and browser checks for UI changes. Live financial actions stay with the teammate running the 021 acceptance checklist.

## First implementation batch

Validation on 9 October 2026: 1,071 backend tests passed; 80 frontend tests passed;
TypeScript checking and production build passed. Browser checks covered both switches
defaulting off and saving, 20 labelled demo chart points/bars, desktop rendering and
375px mobile rendering without horizontal overflow or runtime exceptions. Restart,
day rollover, exact expiry and approval-time rejection are covered by backend tests.
No fresh mutation run or live-model/live-broker gate is claimed.

Implemented D1-D3. An observed cooldown is latched in SQLite and survives restarts and day rollover. Its deadline is displayed in IST. Switching it off clears the pause; editing duration does not shorten an existing pause. Reports and approval checks observe qualifying streaks. Loss estimates depend on available broker snapshots: losses while the app is offline cannot always be reconstructed from a today-only broker ledger.

The goal stop uses the decline from the saved baseline with an inclusive threshold and inclusive goal dates. Deposits/withdrawals affect this value; it is not a cash-flow-adjusted return. The existing 80% warning still uses its separate conservative combined-loss metric and discloses overlap.

The timeline sums backend integer-paise net figures, starting at zero before the first of up to 30 recorded past days. Real and demo days never mix. Unscored days contribute P&L without a risk bar. An accessible table provides exact figures.

Both flags default false for presets and existing profiles. No credentials, broker selection or LLM provider settings were changed. Work is in the Syrus branch; internal TradeDesk and remote main have not changed in this batch. Regenerated API types also synchronize existing demo endpoints that were missing from the previous generated files.

### Remaining core execution order

1. R1/R2: optional trader-entered turnover and charges thresholds, with warnings and boundary tests.
2. R3/R4: typed acknowledgment for both ticket types; short-window pace using recorded real order timestamps and a disclosed baseline.
3. G2, then G1/G3: read-only risk chat tools, graph route separation and durable isolated conversation state. Broker execution stays outside graph nodes.
4. V1/V2: optional local transcription and language hints; preserve edit-before-send and bounded processing.
5. S1: verify the actual 021 orders-stream interface before implementing sample-frame parsing and reconnection. Retain REST reconciliation.
6. T2/T1: mutation, real-model parity and teammate live acceptance evidence. Mock tests cannot close live checks.

## Audit reference and deferred backlog

# TradeDesk plan-to-repository audit

Audited 9 October 2026 against `Syrus7_Syrus_Core`, `main`, commit `0e3a834`.
Source plan: the attached `Pasted text.txt` beginning “Before I plan around those pieces…”.
This is a requirements comparison, not an instruction to change the implementation.
Future-scope items are listed separately from the main hackathon build.

## Verification performed in this audit

- Backend: **1,037 tests passed** using Python 3.12.
- Frontend: **80 tests passed** across 5 test files; TypeScript checking passed.
- No live Bedrock, Groq or 021 calls were made by this audit.
- README's live-test statements are reports from prior team work, not independently
  repeated here. `scripts/model_eval.py` currently contains **32 cases**; README
  still mentions 31 prompts. The original plan mentioned 29.
- Application code and README were not changed. This audit file is outside both repos.

## Main-plan gaps

| ID | Requirement | Status and evidence | Work still needed |
|---|---|---|---|
| R1 | Editable daily turnover limit in rupees | Missing. `app/risk/models.py::RiskProfile` has no turnover-limit field. `today.py` computes filled turnover, but `engine.py` does not apply a turnover rule. | Add setting, form field, preview arithmetic/warning, any explicitly chosen hard behavior, and boundary tests. |
| R2 | Charges-versus-turnover as a guard limit | Partial. The charges meter and ratio exist in `risk/service.py` and DisciplinePanel. No declared charges threshold or card-warning rule exists. | Decide a trader-chosen threshold, then add the field, UI and warning tests. |
| R3 | Extra typed confirmation for high-friction cases | Partial. OrderTicket and PlanTicket require an “I've read this” checkbox for own-limit warnings. No text entry/typed confirmation. | Implement the typed interaction if the original requirement remains binding; define which warnings require it. |
| R4 | Pace today versus usual, including short-window activity | Partial. Daily order count, daily risk score and usual-risk comparison exist. No “7 orders in 20 minutes versus usual 2” calculation, burst metric or pace view. | Define comparable time windows, derive them from persisted orders, and add facts/warnings/view. |
| V1 | Local faster-whisper fallback and VOICE_PROVIDER switch | Missing. `voice/service.py::Transcriber` calls Groq only; config has GROQ_API_KEY and VOICE_MODEL but no VOICE_PROVIDER. | Implement a local provider, explicit selection/fallback policy, packaging/model setup, and tests. |
| V2 | Explicit en/hi voice language hint | Missing as an interface. The route accepts audio and the Groq request has no language field. The underlying model may auto-detect multilingual speech; that does not implement a selectable language hint or prove Hindi workflow quality. | Add language selection/hint through UI, route and provider; test English/Hindi audio and transcript review. |
| S1 | Binary orders websocket for external order/fill sync | Partial. `sync/external.py` polls REST every 5 seconds, uses the existing login, persists orders and emits external_order events. The binary market-price websocket is different. No dedicated orders websocket. | Build its protocol parsing, reconnect/re-subscription, source attribution and fake-socket tests; retain REST reconciliation. Validate against 021's actual available interface before committing to it. |
| G1 | Distinct read/order/rule/plan/risk graph routes and nodes | Partial/deviation. The actual graph is input_guard -> router -> model/tools -> output_guard. Router has only read/act choices, with no model routing fallback and no distinct risk node. | Decide whether the simpler implementation is accepted; otherwise add the named routes/nodes without moving financial decisions into the model. |
| G2 | Profile/discipline accessible as chat tools | Missing. `llm/tools.py::build_tools` has account/order/rule/plan/portfolio tools but no read-profile/read-discipline tool. REST endpoints and the UI exist. | Add read-only tools and grounded rendering; route risk questions to them. Keep profile changes explicitly reviewed. |
| G3 | SQLite LangGraph conversation checkpointer | Missing. `graph.py` calls `g.compile()` without a checkpointer; conversation history comes from Copilot's in-memory history. | Add persistence and a conversation/thread identity; confirm recovery and isolation. Cards/plans are already stored separately and do not need the graph checkpointer for durability. |
| T1 | Fresh real-model parity gates on both orchestrators after final merges | Needs verification. Model-eval tooling exists with 32 cases. README reports earlier real-model runs; this audit did not repeat them at `0e3a834`. | Run the current 32 cases with Bedrock under classic and langgraph, retain results/model ID/commit, and update the README count. Unit tests are not a substitute for that gate. |
| T2 | Mutation checks for every new guard across all workstreams | Partially evidenced. Person C's earlier 18/18 risk mutations and four durability mutations are documented. That does not establish fresh mutation coverage of every later graph/plan/voice/sync guard. | Inventory new guards, retain a reproducible mutation result per guard, and report any not checked. This audit did not introduce mutations. |

## Implemented main-plan features

| Requirement | Evidence and practical limits |
|---|---|
| In-process LangGraph and classic fallback switch | `app/agent/graph.py`, `router.py`, `main.py`, `ORCHESTRATOR=classic|langgraph`. AgentCore hosting is optional and not needed for this implementation. |
| Human approval outside the graph | Order/plan approval HTTP routes remain separate; graph tools read or draft. Guard checks and financial calculations are code. |
| Input/output protection | Override refusal, number/rupee checks, grounding, advice/execution-claim filters, bounded tool loop; graph uses existing Copilot guard methods. |
| Live trace websocket and UI | `app/trace.py`, TraceEvent, AssistantTrace and frontend trace utilities; real graph steps carry kind/status/detail/timing. Trace is a session view, not durable LangGraph observability. |
| L4 exit losing intraday positions | `llm/portfolio_tools.py::exit_losing_positions`; long/short exits derived in code and draft one card or plan. At most six legs at a time, disclosed. |
| L4 trim oversized holdings | `trim_to_max_weight`; sell-only sizing to trader's supplied percentage, based on current prices; no automatic buys. At most six legs at a time. |
| Any-holding daily drop alert | `alert_on_holdings`; creates one alert per current holding using previous close. It is a snapshot of current holdings, not a permanently dynamic membership rule. |
| Groq voice recording and transcription | MediaRecorder/MicButton -> POST `/api/voice/transcribe` -> Groq Whisper; bounded size/rate/time and errors. No audio persistence or automatic order action. |
| Editable transcript and highlighted numbers | ChatPanel places transcript in the editor; the trader must Send. It then takes the normal typed-chat path and guards. |
| External activity and shared account story | ExternalOrderSync, ExternalOrderEvent, ExternalOrders, SQLite history. Polling gives eventual visibility; no separate second login is introduced by sync. |
| Editable profiles and onboarding | Five answers plus a separate goal form; conservative/balanced/aggressive presets labelled starting values. Original plan said “about six questions”; the final UI uses five. |
| Order count, order size, concentration, loss, cooling-off, re-entry, intraday guards | `risk/engine.py` and forms. Soft warnings; explicit hard switches only for daily order count and daily loss. Hard checks repeated at approval. Cancellation bypasses profile guards; modification warnings only. |
| Risk checks on plan legs | `plans/service.py` checks each leg at preview and approval, with earlier legs counted through extra_orders. This was missing at the earlier Person C handoff but has since been added. |
| Own/external orders counted together | Daily facts read broker orders from all sources; sync stores source-tagged history. Rejected orders excluded. |
| Charges, goal progress, mindful mode, usual risk, history comparison | DisciplinePanel and risk report/service. P&L and charges remain estimates; goal pace is arithmetic, not a forecast. |
| Labelled seeded history | Deterministic synthetic weekdays in demo mode with explicit tags; real past days take precedence. |
| Durable order/trade history | `history/sqlite_store.py`, wired into main and sync. Historical data starts when recorded; no old 021 ledger reconstruction. |
| Durable pending cards, plans, requests and reports | `pending.py`, `plans/store.py`, backed by SQLite. Startup recovery halts interrupted plans rather than resuming unsent steps. |
| Co-Captain/Anchor lock placeholders | Lock models and rejection behavior exist, but this is not a real second-user approval workflow. |
| Compliance audit export | GET `/api/audit/export`, downloadable NDJSON. Already implemented even though the plan listed it as future scope. |
| Bedrock integration | Backend provider/factory and Converse tools/history; local app currently uses rules rather than Bedrock. Keys remain private. |

## Future-scope requirements from section 6

| Requirement | Status |
|---|---|
| Co-Captain co-approval and second-user risk report | Missing. Simulated locks only; no authenticated second-person approval/report workflow. |
| Scheduled/GTT-style orders | Partial. Price-triggered standing rules already prepare approval cards. No calendar/time scheduler, durable scheduling UI or generalized GTT workflow. |
| Full cash/concentration impact preview | Partial. Cards show estimated cost/proceeds and charges; stock-weight warnings compute approximate exposure. No comprehensive before/after cash and concentration panel. |
| Email or WhatsApp rule notifications | Missing. Current notifications are in-app events. No outbound providers/delivery/retry configuration. |
| Spoken replies/text-to-speech | Missing. Voice is input/transcription only. |
| Regional-language experience | Partial. Hindi/Hinglish prompts occur in the model-eval cases and Groq may auto-detect languages, but no explicit voice language control, localized UI or verified end-to-end regional-language feature. |
| Installable/offline-capable phone PWA | Missing. Responsive browser UI exists; no PWA manifest/service worker/install/offline implementation. |
| Eval suite on every change | Partial. pytest, frontend tests and model_eval exist. No tracked .github workflow or other repository CI configuration automatically running them was found. |
| Read/propose-only MCP server | Missing. Internal tools are not exposed as an MCP server. |
| LangGraph-native observability | Missing. In-app trace exists, but no LangSmith/LangGraph-specific configured tracing/observability integration. Installing a transitive LangSmith dependency does not configure it. |
| AgentCore hosting | Optional and unimplemented. In-process execution is already the planned primary approach. |

## Live checks still pending according to the current README

- External order created in 021's own application becoming visible in the live external feed.
- An actual sell fill: earlier reported live checks accepted/cancelled orders outside market hours.
- Actual sandbox 429/rate-limit behavior.
- Hard profile-limit rejection on the live account: README says these are mock-tested.
- The final merged 32-case real-model gate on both orchestrators, with stored results.
- Any additional live coverage required by the new voice/provider or orders-websocket features if built.

Prior reported live checks include read-only adapter data, Groq transcription,
placing/partial orders, modifying/cancelling, same-day delivery sale acceptance,
exit-loser drafting, risk profile/goal/restart behavior, and four ambiguous network
failure cases. They should not be presented as fresh results from this audit.

## Suggested order for the remaining core work

1. Decide which exact original requirements remain mandatory versus accepted simplifications.
2. Complete turnover/charges settings, the pace view and typed confirmation if mandatory.
3. Add profile/discipline chat tools and conversation persistence.
4. Implement local STT/language hints and dedicated orders websocket only if still required.
5. Run final model parity/live checks; capture evidence and align README counts/claims.
6. Treat section 6 items as a separate backlog unless explicitly promoted into hackathon scope.

This file can be extended when the user provides more requirements.
