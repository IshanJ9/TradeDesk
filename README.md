# TradeDesk-AI

A copilot for a 021 Trade account. The LLM can read data and draft orders; plain code owns every
money rule; only a human click on an exact order card can send an order.

## Run (backend)

    python -m venv .venv
    .venv\Scripts\python -m pip install -r requirements.txt
    copy .env.example .env
    .venv\Scripts\python -m uvicorn app.main:create_app --factory --reload

API docs at http://127.0.0.1:8000/docs. WebSocket at `ws://127.0.0.1:8000/ws`.

## Run (frontend)

    cd frontend
    npm install
    npm run dev          # http://localhost:5173, proxies /api and /ws to the backend on :8000

Start the backend first. `npm run build` makes the production bundle (`npm run preview` serves it).
The UI follows the system light/dark setting, bundles its fonts (no network needed), and works down to
phone width (an Ask / Desk switch appears below 1024px).

## Test

    .venv\Scripts\python -m pytest -q                 # backend
    cd frontend && npm test && npm run typecheck      # frontend

## Frontend types

Pydantic models are the single source of truth. After changing any model or route:

    .venv\Scripts\python scripts/gen_types.py
    cd frontend && npm run typecheck

Never edit `frontend/src/lib/types.gen.ts` by hand.

## The assistant (LLM)

The chat runs behind `app/llm/types.py::LLMClient`. `LLM_PROVIDER=rules` (the default) uses a built-in
keyword parser that makes the same tool calls a real model would, so everything works without a provider.
To add a provider such as Bedrock: write one class with `async complete(system, messages, tools) -> LLMTurn`
(translate to/from the provider's API) and add a branch in `app/llm/factory.py`. Keep keys in `.env`.

Whichever model is used, code (not the model) enforces: order cards are written by code; every number in an
answer must come from a tool result or the trader's message; no claims of having placed an order; no advice;
text from outside the app that looks like instructions is withheld from the model.

## Where things are

| Path | What |
|---|---|
| `app/schemas.py` | Shared data contract (money in integer paise) |
| `app/api_models.py` | REST and WebSocket message types |
| `app/broker/` | `BrokerAdapter` interface, `ReadOnlyView`, `MockBroker` |
| `app/orders/` | charges, hard limits, card builder, approval service, executor |
| `app/llm/` | LLM interface, tools, guards, the stand-in parser, the orchestrator |
| `frontend/src/lib/` | typed API client, live-feed reducer, formatting (tested) |
| `frontend/src/components/` | the desk: chat, approval tickets, account, activity |
| `app/audit.py`, `app/db.py` | SQLite audit log and execution write-ahead log |
| `docs/superpowers/specs/` | design notes |
