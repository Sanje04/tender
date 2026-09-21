# Tender — Self-Hosted AI Finance Assistant

[![CI](https://github.com/Sanje04/tender/actions/workflows/ci.yml/badge.svg)](https://github.com/Sanje04/tender/actions/workflows/ci.yml)

Import a bank statement, get a spending dashboard, and ask an AI assistant questions about your own money — with the model running on **your hardware via Ollama**, not a paid third-party API. React/TypeScript frontend, Python/FastAPI backend, MongoDB, and a separate tool server the agent talks to over the Model Context Protocol.

The interesting part isn't the chat box. It's that the assistant **decides when to call a tool** — six of them, served by a second process it discovers at runtime — and that the system keeps answering when that process, or MongoDB, or Ollama itself is down.

## Why this project

Most chatbot demos wire a frontend straight to a hosted API (OpenAI, Anthropic, etc.) and call it done. This project is deliberately built the other way: a clean frontend/backend split with a fixed API contract between them, so the backend's internals can change — echo logic → LLM-backed agent → agent with persistent memory and tool-calling — without touching the UI at all. It's a small, concrete demonstration of:

- Designing a stable API contract and building both sides of it independently
- Structuring a React + TypeScript app with typed state, persistence, and error handling
- Building a FastAPI backend with request validation and clear error responses
- Integrating a self-hosted LLM (Ollama) running on separate hardware from the app server
- Durable, deterministic persistence of every conversation turn to MongoDB, independent of the LLM
- Giving the agent tool-calling access to that database, so *it* decides when to query/search/delete history rather than the backend exposing a conventional REST CRUD API for it — see [Agent tool-calling over conversation history](#agent-tool-calling-over-conversation-history) below
- A second, independent domain the same agent reasons about: mock bank accounts/transactions, queried and summed the same tool-calling way — see [Mock bank transactions](#mock-bank-transactions) below
- Serving those tools from a standalone **MCP (Model Context Protocol)** server the backend discovers at runtime, so the agent's capabilities are a separate service with a discoverable contract rather than a function table compiled into it — see [Tools served over MCP](#tools-served-over-mcp) below

The centerpiece is that last point: not a chatbot with a database bolted on, but an agent that reasons about *when* to act on one.

## Architecture

```
┌─────────────────────┐       POST /api/chat        ┌──────────────────────┐       HTTP       ┌───────────────────────┐
│   React + TS UI      │ ────────────────────────────▶│  FastAPI Backend      │ ────────────────▶│  Ollama (remote host) │
│  (Vite, localStorage)│◀──────────────────────────── │  (validation, agent,  │◀─────────────────│  local LLM inference   │
│  + spending dashboard│      GET /api/transactions   │   MCP client)         │   model output   └───────────────────────┘
└─────────────────────┘◀──────────────────────────── └───────┬───────┬──────┘
                          { accounts, transactions }          │       │
       auto-save every turn (deterministic, never the model's │       │  MCP streamable HTTP
        decision) ─────────────────────────────────────────── ┘       │  (tool discovery + invocation)
                                                              │       ▼
                                                              │  ┌──────────────────────┐
                                                              │  │   MCP Tool Server     │
                                                              │  │  (own process/container│
                                                              │  │   six agent tools)     │
                                                              │  └───────────┬──────────┘
                                                              ▼              ▼
                                                        ┌──────────────────────────────┐
                                                        │   MongoDB (local / Atlas)     │
                                                        │  conversations,               │
                                                        │  accounts, transactions       │
                                                        └──────────────────────────────┘
```

- **Frontend and backend communicate over one fixed JSON contract**, so either side can be rebuilt independently.
- **The backend talks to Ollama over the network**, not in-process — the model can run on a separate, more powerful machine (e.g. one with a GPU) while the backend and UI run anywhere.
- **Every turn is auto-saved to MongoDB** by the backend, deterministically, regardless of what the model does — this always happens and doesn't depend on the LLM.
- **The agent's tools live in a separate MCP server process**, and the backend *discovers* them at startup over the Model Context Protocol rather than hardcoding a tool table — see [Tools served over MCP](#tools-served-over-mcp) below.
- **The agent has tool-calling access** to the MongoDB store (list/search/delete history, plus mock account/transaction lookups) so the model itself decides when to act on it — see [Agent tool-calling over conversation history](#agent-tool-calling-over-conversation-history) and [Mock bank transactions](#mock-bank-transactions).
- **`GET /api/transactions`** is a separate, read-only path used only by the frontend's dashboard for display — the agent never calls it; the agent's own access to the same data is tool-calling only. Every dashboard aggregate (summary stats, time series, top merchants, recurring detection, anomalies) is computed client-side from this one payload, so none of it needed new endpoints.

## Tech stack

| Layer     | Technology |
|-----------|------------|
| Frontend  | React 18, TypeScript (strict mode), Vite, plain CSS |
| Backend   | Python 3, FastAPI, Pydantic, Uvicorn |
| Agent     | Python, `httpx` async client calling Ollama's `/api/chat` |
| LLM       | Ollama, running locally/on a LAN host — no cloud API costs |
| Persistence (client) | Browser `localStorage` — what the UI reads from today |
| Persistence (server, current) | MongoDB (local instance), via Motor — conversations auto-saved on every turn and readable/searchable/deletable by the agent's tools (not yet read back by the UI); a separate seeded `accounts`/`transactions` mock dataset readable by the agent's tools and, read-only, by the UI's dashboard |
| Agent tool-calling | Ollama `tools` field, two-call loop in `agent.py` — see [Agent tool-calling over conversation history](#agent-tool-calling-over-conversation-history) |
| Tool serving | Model Context Protocol (`mcp` 1.12, streamable HTTP) — the six tools run in their own server process that the backend discovers at startup; see [Tools served over MCP](#tools-served-over-mcp) |

## Features

- Chat interface with message history, loading states, and error handling — a neutral fintech-dashboard visual design with light/dark theme support (system-aware, manually toggleable)
- Chat history persisted client-side in `localStorage` and restored on page load
- Backend agent that forwards messages to a local LLM (Ollama) and returns real model-generated replies, with a `502` returned if Ollama is unreachable
- Every `/api/chat` turn durably auto-saved to MongoDB by the backend (async, via Motor), independent of `localStorage` and independent of the model
- Agent tool-calling over that same MongoDB store — the model can list, full-text search, and (with explicit confirmation) delete conversation history in natural language, via `list_conversations`/`search_history`/`delete_conversation`
- All six of the agent's tools served by a standalone MCP server process that the backend discovers at startup — schema-validated on the wire, with the backend degrading to a tool-free reply (still `200`, never a `502`) when the tool server is unavailable
- A second, mock bank accounts/transactions domain the same agent can reason about — `list_accounts`/`search_transactions`/`get_spending_summary`, the last of which computes real totals server-side rather than letting the model guess — plus a read-only `GET /api/transactions` endpoint powering a **spending dashboard** — summary cards, a spend/income trend chart, a category donut, top merchants, recurring-payment and anomaly detection, and a transaction table, all filterable by time range and by clicking into a category or merchant — with the assistant alongside it in a collapsible rail
- Strictly-typed API contract shared between frontend and backend
- Backend request validation with descriptive 400 errors on malformed input
- Frontend works standalone with a built-in mock bot when no backend is configured
- CORS-enabled FastAPI backend for local cross-port development

## Project structure

```
tender/
├── ui/                    React + TypeScript frontend (Vite)
│   ├── src/
│   │   ├── screens/       UploadScreen, PreviewScreen, DashboardScreen (chosen from data state)
│   │   ├── components/    AppHeader, AssistantRail, MessageList, MessageItem, InputField,
│   │   │                  CategorySpendingChart, AccountSummary, cards/ (the dashboard cards)
│   │   ├── hooks/         useTransactions (fetch + sort), useCsvImport (import lifecycle)
│   │   ├── services/      api.ts (chat), transactions.ts (transactions) — HTTP clients with mock fallbacks
│   │   ├── utils/         spending/pattern aggregates, date + currency helpers, localStorage
│   │   └── types/         shared TypeScript interfaces
│   └── README.md
└── backend/               FastAPI backend + MCP tool server (two processes, one directory)
    ├── main.py            POST /api/chat + GET /api/transactions routes, request validation
    ├── agent.py           Calls the local LLM (Ollama), tool-calling loop, returns its reply
    ├── mcp_server.py      Standalone MCP server: the six agent tools + their schemas
    ├── mcp_client.py      Backend's MCP client: tool discovery, schema translation, invocation
    ├── db.py              Motor client + auto-save of each turn + mock account/transaction queries
    ├── data/               accounts.csv, transactions.csv — checked-in, human-editable mock data source
    ├── scripts/           seed_transactions.py — loads data/*.csv into MongoDB (not run by the app itself)
    ├── Dockerfile         backend container
    ├── Dockerfile.mcp     MCP tool server container (same build context, shares db.py)
    ├── requirements.txt
    └── README.md

Dockerfile                 All-in-one image: frontend + backend + MCP server in one container
├── deploy/nginx.conf           its nginx config (static — everything is loopback)
└── deploy/supervisord.conf     supervises the three processes
                           Build from the repo root: `docker build -t tender .`

infra/                     Azure Container Apps deployment (built — see AZURE_DEPLOYMENT.md)
├── deploy.ps1             Idempotent deploy: resource group, environment, three container apps
├── deploy.sh              Bash equivalent of the above
├── backend-app.yaml.template   The backend app + its Tailscale sidecar (two containers, so YAML)
└── deploy.env.example     Config template (real values gitignored)

docs/                      Design/spec documents for proposed, not-yet-built work
├── README.md              Index — which doc covers what
├── design.md              Local Minikube cluster + Jenkins CI/CD pipeline (design only)
└── specs.md               Phase 9: Ollama ↔ Claude API provider switch (spec only)
```

## Deployment

Three topologies, in increasing order of reach:

| Where | Doc | Status |
|---|---|---|
| One host, `docker compose` | [DEPLOYMENT.md](DEPLOYMENT.md) | Built |
| Azure Container Apps, three containers | [AZURE_DEPLOYMENT.md](AZURE_DEPLOYMENT.md) | Built |
| Azure, single all-in-one container (root `Dockerfile`) | [AZURE_DEPLOYMENT.md](AZURE_DEPLOYMENT.md#two-shapes-three-containers-or-one) | Built |
| Single-node k3s cluster with Argo CD | [KUBERNETES_DEPLOYMENT.md](KUBERNETES_DEPLOYMENT.md) | Plan only |

The Azure one is the interesting case: the app runs in a datacentre while the LLM
stays on a home machine, reached over a Tailscale tunnel. That needed no application
code — `agent.py` calls Ollama through a plain `httpx.AsyncClient()`, which honours
proxy environment variables, so a `tailscaled` sidecar plus `HTTP_PROXY` is the whole
integration. Inference stays free and self-hosted; the tradeoff is that the public URL
is only live while that machine is.

## Getting started

### Prerequisites
- Node.js 20+
- Python 3.11+
- [Ollama](https://ollama.com) installed somewhere on your network (can be the same machine)

### 1. Backend

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env
.\.venv\Scripts\python.exe -m uvicorn main:app --reload --port 8000
```

The API is now available at `http://127.0.0.1:8000`.

### 1b. MCP tool server

The agent's tools run in a second process. In another terminal:

```powershell
cd backend
.\.venv\Scripts\python.exe mcp_server.py
```

It serves MCP streamable HTTP on `http://127.0.0.1:9000/mcp` (configurable via `MCP_HOST`/`MCP_PORT`; the backend finds it via `MCP_SERVER_URL`). Start it before the backend to have tools available from the first request — but the order doesn't actually matter: if it isn't up yet the backend still serves, retries discovery on each chat request, and picks the tools up as soon as it appears. See [Tools served over MCP](#tools-served-over-mcp).

### 2. Frontend

```powershell
cd ui
npm install
copy .env.example .env
npm run dev
```

By default `.env` points `VITE_API_URL` at the local backend above. Open the printed local URL in your browser.

> Without a backend running, the UI still works end-to-end using a built-in mock bot — see [`ui/README.md`](ui/README.md).

## API contract

```
POST /api/chat
Content-Type: application/json

{ "message": "user's text" }
```

```json
{ "response": "bot's reply text" }
```

```json
{ "error": "Invalid request: 'message' must be a non-empty string." }
```

## Agent tool-calling over conversation history

MongoDB persistence is live and, as of this pass, the **agent itself** has tool-calling access to that same store, instead of a conventional REST CRUD API bolted onto it:

- **Auto-save (deterministic):** every user/assistant message pair is written to MongoDB by the backend as part of handling `/api/chat`, unconditionally — independent of whatever the model does.
- **Agent tools (LLM-driven, implemented):** the model is given three callable tools — `list_conversations`, `search_history`, `delete_conversation` — so it can act on history when the user asks for it in natural language ("what did we talk about yesterday?", "delete this conversation"). The LLM decides which tool to call and with what arguments; the agent executes the actual MongoDB operation and feeds the result back to the model in a second call so *it* composes the final natural-language reply (a two-call loop, capped at one tool round-trip per turn). `delete_conversation` additionally requires the backend to detect an explicit confirmation phrase in the user's message before it will actually execute, independent of the model's own judgment.
- **Storage:** a local MongoDB Community Server instance, with a `conversations` collection (currently one continuously-growing document, messages embedded as an array) accessed via Motor, plus a text index on `messages.content` for `search_history`.

This is a deliberate choice over a plain REST CRUD API: it demonstrates the agentic tool-use pattern (model reasoning about *when* to query/mutate a database) rather than just wiring a database behind a fixed set of endpoints. See [`backend/specs.md`](backend/specs.md) (Phase 3) for the detailed design, verification notes, and known behavior quirks (e.g. the model is occasionally tool-happy on messages that don't need a tool).

## Mock bank transactions

A second, independent domain the same agent reasons about, in the same tool-calling style as conversation history above — no real bank integration yet, but a seeded mock dataset (3 accounts: Checking, Savings, Credit Card; ~100+ transactions over 6 months) so the pattern can be built and demoed now:

- **Agent tools (LLM-driven):** `list_accounts` (balances), `search_transactions` (filtered lookups by account/category/merchant/date/amount), and `get_spending_summary`, which **computes** totals and a category breakdown server-side rather than handing the model raw rows to add up — and excludes transfers between the user's own accounts and income from spending totals by default, so paying off a credit card doesn't get counted as "spending."
- **Storage:** two new MongoDB collections, `accounts` (3 fixed documents) and `transactions` (~100+ documents), loaded by a one-off, idempotent script (`backend/scripts/seed_transactions.py`) from two checked-in, human-editable CSV fixtures (`backend/data/accounts.csv`, `backend/data/transactions.csv`) — not part of the running app. Transaction dates in the CSV are relative (`days_ago`), so the data always reads as "the last ~6 months" no matter when you seed.
- **`GET /api/transactions`:** a read-only endpoint, separate from the tool-calling path above, used only by the frontend's dashboard to display the same data alongside the assistant. The agent itself never calls this endpoint — its access is tool-calling only, matching the philosophy above.
- Both domains share one `SYSTEM_PROMPT` and one tool-calling loop (still capped at one tool call per turn — see `backend/specs.md` Phase 4): the same assistant handles "what did we talk about yesterday?" and "how much did I spend on groceries?" in one chat.

See [`backend/specs.md`](backend/specs.md) (Phase 4) for the data model, tool contracts, and verification notes.

## Tools served over MCP

The six tools above no longer live inside the backend. They run in a **standalone MCP (Model Context Protocol) server** — its own process, its own container — and the backend is an **MCP client** that asks it "what tools do you have?" at startup instead of holding a hardcoded tool table:

```
backend/mcp_server.py   the six tools + their JSON Schemas, served over MCP streamable HTTP
backend/mcp_client.py   discovery, schema translation (MCP → Ollama), invocation
backend/agent.py        no tool schemas, no database calls — just the Ollama loop and the delete guard
```

- **Tools are discovered, not declared.** `agent.py` used to carry a ~200-line `TOOLS` literal. It now carries none: adding, removing, or re-describing a tool is a one-file change in `mcp_server.py`, and the backend picks it up on the next boot with no code edit. `mcp_client.py` translates each tool's JSON Schema into the shape Ollama's `tools` field wants, which is the only place that mapping exists.
- **Schemas are the wire contract, and they're enforced.** The MCP server validates every invocation against the tool's declared schema before touching MongoDB, so a missing required field, a wrong type, or a `category` outside the known list is rejected up front rather than passed through as a filter that quietly matches nothing.
- **A dead tool server degrades chat, it doesn't break it.** Discovery failing at startup is not fatal — the API comes up, and the next chat request retries. If the tool server is down mid-conversation, the failure is handed back to the model as an ordinary tool result and it explains the problem in plain language. `POST /api/chat` still returns `200`; `502` stays reserved for Ollama itself being unreachable.
- **The delete guard deliberately stayed behind.** `delete_conversation`'s confirmation check runs in the backend, against the user's raw message, *before* any invocation is sent — the MCP server never sees the user's words, only the arguments the model chose, so it isn't the right place to judge consent. An unconfirmed delete produces zero MCP traffic.
- **One shared data layer, two connection pools.** `db.py` is imported by both processes rather than duplicated; each opens its own Motor pool against the same database. The deterministic paths (auto-save, history fetch, CSV import) stay in the backend and are deliberately *not* exposed over MCP — the model must not be able to reach them.

The point is the protocol boundary: tools become a service with a discoverable, versionable contract instead of a function table compiled into the agent. See [`backend/specs.md`](backend/specs.md) (Phase 8) for the design, the dependency-pin reasoning, and what was and wasn't verified live.

## What this project demonstrates

- End-to-end ownership of a stable API contract across an independently-typed frontend and backend
- Integrating a self-hosted LLM (Ollama) as a network dependency, not an in-process library call — including handling its failure modes (`502` on unreachable/erroring model)
- Separating deterministic persistence (every turn auto-saved) from model-driven behavior (planned tool-calling), instead of conflating "the backend stores things" with "the model decides to store things"
- Async Python I/O throughout the backend (`httpx` to Ollama, Motor to MongoDB) so one slow dependency doesn't block the event loop
- Designing for the agentic tool-use pattern specifically — a model that chooses *when* to invoke a capability — as distinct from a standard CRUD API
- Serving those capabilities over an open protocol (MCP) as a separate process the agent discovers at runtime, including the failure-mode design that makes a two-process split safe: the tool server can be down, come up late, or reject a malformed call, and chat still answers

## Status & roadmap

- [x] Frontend chat UI with localStorage persistence
- [x] FastAPI backend with validated `/api/chat` contract
- [x] Backend agent layer that calls a local LLM served by Ollama on a separate machine
- [x] MongoDB-backed conversation storage (auto-save every turn)
- [x] Agent tool-calling for history operations (list/search/delete conversations)
- [x] Mock bank accounts/transactions domain with agent tool-calling (list/search/summarize) and a read-only spending dashboard in the UI
- [ ] Frontend updated to load history from the backend instead of `localStorage`
- [x] Multi-turn conversation context passed to the model, bounded to a configurable number of recent turns
- [x] Tools extracted into a standalone MCP server the backend discovers at startup, instead of a hardcoded tool table
- [ ] Deployed to Azure Container Apps on a public HTTPS URL, free tier, with the LLM still self-hosted behind a Tailscale tunnel. **The deployment is written but not currently running:** `infra/deploy.ps1`/`deploy.sh`, the backend + `tailscaled` sidecar container-app definition, and the CI deploy job (pinned to the commit SHA) are all complete and idempotent, but no instance is provisioned, so there is no live URL to link. See [AZURE_DEPLOYMENT.md](AZURE_DEPLOYMENT.md)
- [x] Local Kubernetes (Minikube) deployment driven by a Jenkins pipeline — tests, three image builds, rollout, smoke test, and automatic `kubectl rollout undo` on failure — see [docs/design.md](docs/design.md) and [docs/local-deployment-tutorial.md](docs/local-deployment-tutorial.md)
- [x] Structured JSON logging with cross-process request-id correlation, and Prometheus metrics at `/metrics` — see [backend/specs.md](backend/specs.md) Phase 10
- [ ] RAG over the transaction/conversation data, exposed as an MCP tool
- [ ] Per-tool authorization (RBAC) on the MCP server, so a tool server is safe to expose to clients other than this backend

See [`backend/specs.md`](backend/specs.md) and [`ui/specs.md`](ui/specs.md) for the original design specs each side was built from.
