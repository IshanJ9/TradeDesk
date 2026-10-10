# Co-Captain implementation record

Branch: `cocaptain`. Work in progress; do not describe incomplete sections as shipped.

Ishan confirmed on 10 October 2026:

1. In the zone without an active Co-Captain: block.
2. Leaving the zone while waiting: require a fresh trader Approve click.
3. Plans: count every step and sum estimated order values, including both sides.
4. Reviewers see submitted cards and quoted zone numbers only, not the portfolio.
5. Turnover matches existing warnings: **filled turnover + proposed order/plan**,
   excluding other outstanding unfilled orders. Every zone boundary is strict `>`.

The zone predicate accepts no profile/all absent limits as unconfigured. Current
saved RiskProfile requires a daily order limit; its schema is not being redesigned.
Cancel/modify requests do not add new orders and do not require Co-Captain approval.

Real login/session work belongs to Akash. Actor integration and real-session
acceptance must be completed before this is described as production authentication.

Validation and mutation results will be recorded after each implemented portion.

## Requirement 1: zone predicate

- 15 focused tests passed. Full backend: 1,186 passed; full frontend: 95 passed;
  production build passed. Used existing Python 3.12 verification environment and
  Node 22 (the supplied `.venv`/system Node were 3.10/20 respectively).
- Mutation: changed daily comparison from `>` to `>=`; two boundary tests failed
  as expected. Original source restored byte-for-byte before committing.

## Requirement 2: pairing and identity foundation checkpoint

- Implemented persistent invite/accept/revoke with a distinct reviewer, generation
  IDs, actor-scoped notifications, actor dependency and owner-only legacy routes.
- Dev actors default off and require DEMO_MODE. This remains a single-account
  development harness pending Akash's real sessions and per-user stores.
- Audit retains its existing role enum; actual IDs are in `data.actor_id`.
  An initial full-suite failure caught the attempted enum widening; fixed before
  committing, with the original audit schema test unchanged.
- Full backend: **1,201 passed** (92.77 seconds). Frontend: **95 passed**.
  Production build and generated API types passed. No live broker calls.
- Pairing guard mutations, each independently applied and restored:
  self-invitation check removed -> 1 failed; accepting-reviewer identity check
  removed -> 1 failed; revoked-link acceptance check removed -> 1 failed;
  revoke-authority check removed -> 1 failed. Original file restored byte-for-byte.
  These do not replace the required future order-approval gate mutations.
- No two-person order gate or UI exists yet. Feature remains disabled by default.
  Read `COCAPTAIN_HANDOVER.md` for detailed continuation steps and limitations.

## Requirement 3 foundation: durable review store

- Added immutable card/account/expiry/policy/link bindings and unique per-role
  approvals. Either click order, idempotent duplicates, terminal decline,
  invalidation and restart persistence are covered by 18 focused tests.
- This store never sends orders and is not yet connected to main/approval paths.
- Full backend: **1,219 passed** (91.88 seconds); frontend **95 passed**;
  production build passed.
- Nine independent mutations each caused the expected test failure: expiry,
  request hash, owner binding, account binding, expiry binding, live-link check,
  stored decision identity, stored decision hash, stored decision policy.
  Source restored byte-for-byte. Final send re-evaluation mutation remains TODO
  until that gate exists. These are store tests, not end-to-end trading proof.
