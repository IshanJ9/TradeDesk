# Person C handoff

Validated on 9 October 2026 on `risk-goals`. README.md has not been edited.
All work and verification used the mock broker and rules provider. No live 021
orders or Bedrock calls were used for this feature's tests.

## Ready-to-copy README text

### Risk profile, goals and discipline

The Discipline tab lets one trader define their own limits. Five onboarding
answers suggest an editable conservative, balanced or aggressive starting
profile. Presets are starting values, not recommendations, and are only saved
after the trader reviews them. Profile and goal settings are stored in SQLite.

Order previews can show warnings about order count, order size, stock exposure,
daily loss after charges, consecutive-loss cooling-off windows, re-entry after
a loss, intraday settings, and the trader's goal loss allowance. Daily order
count and daily loss become blocking limits only when the trader enables the
corresponding hard switch. Those hard limits are checked again at approval.
The risk guard never sends an order. It does not block cancellations, and
modifications receive warnings only. No profile means no profile-based limits.

The report shows today's activity, estimated P&L and charges, a 0–100 score with
five visible weighted components, and comparison with up to 20 previous scored
real days. The historical comparison groups up to 30 recorded days above or at
or below the usual score, displaying net P&L and profitable-day counts. Past days
don't predict future ones. Scores and their components are stored with the day's
snapshot; they are not forecasts or recommendations.

Goals record portfolio value when saved and show the change from that baseline,
progress toward a chosen gain, days left, remaining amount per week, straight-line
pace, and maximum-loss headroom. These are arithmetic calculations, not expected
returns. Deposits and withdrawals affect progress. The UI accepts rupees and the
API uses integer paise. Mindful mode hides today's P&L and current goal progress
in the Discipline tab; historical figures remain visible.

Reports refresh through a 20-second background loop and after profile changes.
History starts when this app records it; the 021 order-book interface supplies
today's activity, not a reconstructed historical ledger. With demo mode enabled,
an empty history receives 20 deterministic synthetic weekdays. They are labelled
DEMO DATA and are used for comparisons only until real past days exist. Synthetic
charges are displayed separately. Demo history can be seeded or cleared through
the demo-only API; clearing it does not delete real history or silently reseed it.

P&L is an estimate using cumulative order fills, approximate FIFO ordering,
open-position P&L, and remaining holdings' day P&L. Delivery and intraday lots are
matched separately. Missing fill prices or older-holding cost bases are disclosed
and excluded rather than invented. Charges are estimates from the existing charge
calculator. Closed-trade loss streaks use results before charges. Stock-weight
warnings use gross current exposure plus the proposed buy; they can overstate
exposure when a buy closes a short. The goal warning sums today's loss and the
decline since the goal began, explicitly noting that those periods can overlap.

Multi-step plan legs are not checked by the profile guard. Orders placed in 021's
own app count toward today's totals but cannot be blocked by this app. The limits
belong to the trader and do not constitute investment advice.

Verification: 890 backend tests passed, including the original 675 tests and 215
new risk tests. All 18 deliberate guard mutations were caught. Frontend typecheck,
35 frontend tests, and the production build passed. A mock browser session checked
onboarding, profile editing, exact rupee-to-paise goal saving, goal removal,
mindful mode, live-event refresh, preservation of unsaved edits, and stale-report
error/retry behavior. Discipline cards were inspected at 375 px and desktop widths
in light and dark themes. Live broker behavior was not tested in this workstream.

## Integration notes

- Merge the small risk-goals additions in `app/main.py` alongside the other owners'
  work: one profile store, a ProfileGuard, a DisciplineService, one background task,
  and the risk router. Keep shutdown cancellation of the background task.
- The shared RiskGuard/RiskVerdict, ActivityStore, order schemas, approval flow,
  frontend store and DeskTabs files were not changed. The discipline service reads
  the current `app.state.history`, so Person B can replace that store.
- Real summaries are saved through `history.save_day()` and copied to risk-owned
  tables with components. Demo rows stay in risk-owned storage because the shared
  history protocol has no delete method. Real rows take precedence by date.
- API additions: profile/presets/onboarding, goal, GET `/api/discipline`, and
  POST/DELETE `/api/discipline/demo-seed` (404 outside demo mode). GET reports do not
  publish another event, avoiding event/refetch loops. Generated OpenAPI and TS types
  are included; regenerate them after other branches merge.
- A broker timeout on a report returns 503. Saving a profile still succeeds if its
  follow-up refresh fails. The background loop skips failures and tries again.
- Use a file-backed DATABASE_URL for persistence across process restarts. The test
  and temporary browser preview databases are in memory.
- The app is single-trader; profiles and goals are not scoped by user or account.
- The existing shared mobile desk/activity-tab layout has horizontal overflow.
  Discipline cards themselves fit at 375 px; resolving the shared overflow belongs
  to the layout owner. DeskTabs and account layout were left untouched.
- The optional OrderTicket “I've read this” checkbox was not added. Existing warning
  display and approval behavior remain, with hard limits enforced by the backend.
- Existing Starlette/httpx deprecation warning remains. No new dependencies were added.

## Local commits ready for the user's push

1. `2e19b2c` Profile, onboarding and goals.
2. `faf81c5` Daily broker facts.
3. `5d64063` Guard warnings and hard limits; 18/18 mutation checks.
4. `2dd673f` Scores, reports, goal progress and demo history.
5. `f053c45` Discipline UI.
6. This handoff and final validation checkpoint.

These were the original local Person C checkpoints. The feature has since been
integrated with the existing LangGraph work on the Syrus submission's `main`
branch at the user's request. See `app/risk/LIVE_021_HANDOFF.md` for the teammate's
live-testing setup and checklist. The internal TradeDesk `risk-goals` branch is
still local-ahead and can separately be published with `git push origin risk-goals`.
