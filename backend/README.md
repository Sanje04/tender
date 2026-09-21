# Tender Chatbot Backend

FastAPI backend for the Tender finance assistant. It forwards each message to a
local LLM served by Ollama, replays a bounded window of recent turns as context,
and lets the model call tools -- which live in a second process and are reached
over MCP (`mcp_server.py`, see `specs.md` Phase 8).

It is designed to degrade rather than fail: MongoDB, Ollama and the MCP tool
server can each be down without taking the API with them. Only Ollama being
unreachable produces a 502.

`specs.md` is the full phase-by-phase account; this file is just how to run it.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Run

Two processes. The API:

```powershell
.\.venv\Scripts\python.exe -m uvicorn main:app --reload --port 8000
```

And, in a second terminal, the MCP tool server that hosts the agent's six tools
(see `specs.md` Phase 8):

```powershell
.\.venv\Scripts\python.exe mcp_server.py
```

The API will be available at `http://127.0.0.1:8000`, the tool server at
`http://127.0.0.1:9000/mcp` (`MCP_HOST`/`MCP_PORT`; the API finds it via
`MCP_SERVER_URL` — all three in `.env.example`).

Startup order doesn't matter. If the tool server isn't up, the API still serves;
it retries discovery on each `/api/chat` request and picks up the tools as soon
as the server appears. Until then the model is called with no tools and answers
directly, so chat works but can't look anything up.

To connect the frontend, set `VITE_API_URL=http://127.0.0.1:8000/api/chat` in
`ui/.env` (or `ui/.env.local`).

## API

### `POST /api/chat`

Request:

```json
{ "message": "How much did I spend on groceries last month?" }
```

Response (200):

```json
{ "response": "You spent **$412.88** on Groceries last month, across 14 transactions." }
```

Error response (400) — `message` missing, not a string, empty, or over 4000 characters:

```json
{ "error": "Invalid request: 'message' must be a non-empty string of at most 4000 characters." }
```

A 502 means Ollama was unreachable or returned an error. A tool failure is *not*
a 502: it comes back to the model as a normal tool result and the request still
returns 200, with the model explaining it couldn't look something up.

### Other endpoints

| Endpoint | Purpose |
|---|---|
| `GET /api/health` | Liveness probe. Touches no dependency on purpose — it reports "this process is up", not "the system is healthy" |
| `GET /api/transactions` | Read-only, for the frontend's dashboard. Not used by the agent |
| `POST /api/transactions/import` | Replaces all account/transaction data from an uploaded bank-statement CSV. All-or-nothing; one bad row rejects the file |
| `GET /metrics` | Prometheus exposition (`specs.md` Phase 10). Not under `/api/`, so nginx does not proxy it |

## Testing

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest
```

One test (`test_valid_message_returns_real_reply`, marked `live_llm`) calls this
machine's local Ollama directly (`127.0.0.1:11434`, model `gemma4:latest`) instead of
mocking it — make sure Ollama is running locally with that model pulled, or skip it:

```powershell
.\.venv\Scripts\python.exe -m pytest -m "not live_llm"
```

All other tests mock Ollama, MongoDB, and the MCP tool server, so they need none
of the three running.

## Notes

- CORS defaults to all origins for local dev; a deployment pins `ALLOWED_ORIGINS`
  to the one frontend hostname.
- Both processes log one JSON object per line and share a request id, so a chat
  turn can be traced across the API/MCP boundary. `LOG_FORMAT=text` gives a
  readable format when tailing locally.
- Every `/api/chat` turn is persisted to MongoDB deterministically (see `db.save_turn`),
  and a bounded window of recent turns is replayed as context (`db.get_recent_history`).
- The agent's tools are not implemented here — they live in `mcp_server.py` and are
  reached over MCP. `agent.py` holds no tool schemas and makes no database calls.
