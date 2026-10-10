# Discipline additions: first core-completion batch

Base: Syrus main `0e3a834`. Working branch: `discipline-core-completion`.
The full remaining checklist is in the repository root `IMPLEMENTATION_PLAN.md`.

Verified: 1,071 backend tests, 80 frontend tests, TypeScript checking, production build,
and desktop/mobile browser checks. No live 021 or model calls were made.

## Added behavior

- `hard_cooling_off` defaults false. When enabled, an observed qualifying losing streak blocks new order cards and approval until the configured deadline. The deadline is stored in `risk_cooldown`, survives restart/day rollover, and appears in Discipline in IST. A reset streak does not remove an active pause. Turning the switch off clears it. Editing the duration cannot shorten an active pause.
- `hard_stop_on_goal_loss` defaults false. During the saved goal's date window, new orders stop when baseline portfolio value minus current portfolio value reaches the maximum loss. This does not add today's loss again. Removing/replacing the goal or changing the setting changes the applicable limit.
- Both flags are checked for individual orders and plan legs at preview and approval. Cancellations bypass these checks; modifications retain warnings only. They do not block orders entered directly in 021.
- The trend chart uses a backend-generated chronological timeline of up to 30 recorded past days. Cumulative net P&L starts at zero before that window. Risk scores appear as bars; missing scores omit bars. Real history replaces demo history for comparison. Demo data is labelled. The expandable table exposes exact values.

## Teammate setup and checks

1. Pull this branch after it is published, install the existing requirements, and restart the backend. No new package or secret is required. Use a file-backed `DATABASE_URL` to retain cooldowns across restarts.
2. Open Desk > Discipline > Edit profile. Both new switches should initially be unchecked for an existing profile. Enable only the stops you intend to test, then save.
3. In the mock environment, validate a qualifying losing streak and check the pause deadline. Both a single order and a plan must be blocked. Restart before expiry: the pause should remain. At expiry it must stop blocking; switching it off also clears it.
4. Set a goal, enable its stop, and test just below and exactly at the maximum baseline decline in the mock environment. Approval must reload current facts, including when a card was created before the loss threshold was reached.
5. Check Risk and reward over time, its DEMO label when applicable, and View trend figures. Daily net amounts are estimates after charges, not realized account returns or a forecast.
6. Live 021 acceptance remains separate. Do not create real losses to demonstrate a guard. Use the existing live handoff for authorized broker checks; this batch was verified with mock data and made no live model or broker calls.

## Limits

The app remains single-trader. Cooldowns can only latch losses the app observes. A today-only broker ledger cannot reconstruct all losses that happened while the app was offline. FIFO closing-trade results and charges are estimates. Deposits/withdrawals affect the goal's portfolio baseline calculation. An already approved plan is governed by the existing plan execution lifecycle; this change does not add a continuous kill switch during execution.

The older `HANDOFF.md` records the original Person C implementation. Its historical statements about missing plan guards, checkboxes and mobile overflow have since been superseded by integrated teammate work. This file documents only this new increment.
