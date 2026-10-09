# TradeDesk-AI foundation design (2026-10-08)

Status: decisions agreed in chat; written for the record. Not committed (no git until the hackathon).

## Goal
A thin, real end-to-end MVP of the five PS requirements on a mock broker, built in one night, as the base for the 021 adapter and extras tomorrow.

## Principle
The LLM understands language and can only read data or draft orders. Plain code owns every money rule. Only `POST /api/approvals/{id}/approve`, with the exact `order_hash`, can send an order.

## Decisions
- **Scope:** equity orders on NSE/BSE only. Option chain is read-only. F&O orders are rejected (`SEGMENT_NOT_ALLOWED`).
- **Money:** integer paise everywhere. Quantities are integer share units.
- **Stack:** Python + FastAPI + Pydantic v2; React + Vite + Tailwind; SQLite for rules and audit.
- **Contract:** Pydantic is the single source of truth. OpenAPI is exported and TypeScript types are generated for the frontend.
- **Safety defaults:** approval TTL 60 s; price-drift re-quote above 1% from `ref_ltp`; hard limits 1,00,000 units and Rs 1 crore per order (021's published limits).
- **LLM provider:** undecided. Sits behind an interface; a keyword parser stands in until chosen.
- **Approval binds to `order_hash`**, a hash of the exact order fields (not LTP, expiry or state). Any change to instrument, side, qty, prices, product, validity or client order id voids an old approval.
- **Standing rules:** when a rule fires it creates a fresh `PendingOrder` for approval (no auto-place in the MVP). `client_order_id` is derived from the rule id so a double fire cannot place two orders. `heads_up_pct` is reserved, built later; wording stays factual (no price predictions).
- **Plans:** approved as a whole via `plan_hash`. A buy funded by a sell is sized from actual proceeds, bounded by an approved `max_quantity`/`max_spend`. Default failure policy is HALT.

## Build order (tonight)
1. Skeleton + schemas + `order_hash` + tests
2. BrokerAdapter + MockBroker
3. Backend API (`/account`, `/pending`, `/ws`)
4. Safety core + orders (approval, executor, reconcile, limits, audit)
5. Chat + LLM module (read-only tools, propose_order, ambiguity, injection guard)
6. Standing rules (SQLite, dedupe, fire-once, restart recovery)
7. Plans (per-leg report)
8. Web UI, built incrementally after steps 3-7

## Deferred to tomorrow
Real 021 adapter, chaos panel, injection red-team, Anchor/Buffett/Co-Captain screens, full charge math, heads-up warnings, polish.

## Open items
- LLM provider and key.
- Wireframes.
- Kickoff questions for 021 (auth, idempotency tag, status updates, instrument master, GTT/basket, algo-ID rules).
