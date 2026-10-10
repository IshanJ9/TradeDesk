# Analytics implementation and live acceptance

10 October 2026. This checkpoint was prepared on TradeDesk `core-analytics` and
is being merged into internal `main` at the user's request. See
CHECKPOINT_HANDOVER.md for verification limitations and FEATURE_CHECKLIST.md for
the remaining scope. The submission and `Syrus-video` folders are separate.

## Implemented in this batch

- Yesterday's opt-in cooling-off and goal-loss stops and cumulative trend are
  preserved in the internal repo.
- Recorded daily average risk, estimated net P&L/observed returns, weekday/risk
  bands, winning-day counts, timing/symbol patterns and behavior tables.
- Soft profile warnings for turnover, charges/turnover and 20-minute order count.
  Own-limit warnings on order and plan cards require `I UNDERSTAND`; changing
  content invalidates that acknowledgment. Existing hard stops still apply.
- Read-only `get_risk_profile` and `get_discipline` tools; ask “Show my average
  risk and trading patterns” or “Show my risk profile”. Mindful mode suppresses
  today's P&L in the risk reply.
- Auto/English/Hindi microphone hints sent to Groq; editable transcript still
  requires Send. This does not add local Whisper, speech output or translated UI.

## Data rules

The new analytics accept only identified 021 observations. Mock/demo data and
legacy records without provenance are excluded. Nothing automatically creates
demo history. Explicit mock demo controls remain separate. Old records are kept,
not relabelled as verified history. Order provenance is added by an additive
SQLite migration and becomes verified only when actually observed from 021.

Risk observations retain one sample per five-minute bucket for 365 days; average
risk is an average of those observed samples, not a complete trading-session
time average. Day summaries persist separately. Missing time is never filled.

Observed return is `(latest estimated day net P&L - first snapshot day net P&L)
/ first snapshot portfolio value`. It needs a positive baseline and at least
five minutes of coverage. It can cover only part of a day and is not compounded,
annualized or adjusted for cash flows. Charges/P&L retain existing estimates.
The historical average uses up to 30 recorded past days, excluding today.

Order timing and symbol counts exclude rejected and unverified orders. A usual
pace needs observations across the corresponding 20-minute window on prior
days. Unknown fill prices contribute no filled turnover. Loss streaks use the
existing approximate FIFO before charges; behavior counts do not infer motives.

## Manual 021 acceptance — still required

1. Keep one backend per UCC. Stop the other recording/demo backend before starting
   the updated app with the private 021 `.env`; a second login revokes its token.
2. Open Discipline and save/review your profile. With no eligible history, expect
   empty comparisons; do not seed data to fill them.
3. Leave the app connected for at least five minutes. Confirm first/last snapshot
   times, sample count, risk average and observed-return coverage. Refreshing
   repeatedly inside a bucket must not increase its risk sample count.
4. Use the intended manual 021 sandbox order workflow. Confirm actual orders and
   fills against the 021 order book. Do not fabricate losses to test stops;
   isolated automated boundary tests cover those cases separately.
5. On subsequent recorded days, compare historical daily values, symbol/hour
   counts, external attribution, net P&L and coverage to the source records.
   Same-clock-window pace may remain unavailable until enough history exists.
6. Review typed confirmation on an individual draft and a plan with an own-limit
   warning; modifying/replacing the card must require confirmation again.
7. Try English and Hindi microphone input, edit the transcript, then Send.
   Voice must never approve an order.

No live broker orders, emails, real model evals or account history were generated
by this implementation work. Automated fixtures are confined to test databases.

## Next dependencies

The expanded scope is included in the TODO, not claimed complete. Email via an
existing SMTP account is the selected notification channel; quotas depend on
the provider. Co-Captain requires two authenticated distinct users and both
approvals only in the configured overtrading zone. Authentication/account
isolation must precede that enforcement; simulated locks are not co-approval.
Hard stops cannot be overridden by either person.
