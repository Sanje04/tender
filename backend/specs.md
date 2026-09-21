# Backend Design Spec

## Frameworks & Stack

- **Language**: Python 3
- **Framework**: FastAPI (lightweight, built-in request validation via Pydantic, easy CORS setup)
- **Server**: uvicorn
- **Validation**: Pydantic

---

## Project Overview

Build a **backend-only** API that:
- Exposes a single chat endpoint matching the frontend's existing contract
- Echoes back whatever message the user sends (no real chatbot logic yet)
- Runs locally for development only
- Has no persistence (nothing is stored anywhere)
- Has no authentication (MVP)

---

## Project Structure

```
backend/
├── main.py            (FastAPI app + /api/chat route)
├── requirements.txt   (pinned dependencies)
├── README.md          (setup/run instructions)
└── specs.md           (this document)
```

---

## API Contract

### `POST /api/chat`

Matches the frontend's `src/services/api.ts` contract exactly.

**Request:**

```json
{ "message": "user's text" }
```

**Response (200):**

```json
{ "response": "user's text" }
```

Behavior: the backend currently just echoes the `message` field back as `response`.

**Error response (400):**

Returned when `message` is missing, not a string, empty, or when the request body isn't valid JSON.

```json
{ "error": "Invalid request: 'message' must be a non-empty string." }
```

---

## Detailed Requirements

### 1. **Endpoint behavior**
- Single route: `POST /api/chat`
- Echoes the incoming `message` back unchanged as `response`
- No chatbot/LLM logic yet — this is a placeholder for wiring up the frontend end-to-end

### 2. **Validation & Error Handling**
- `message` must be present and a non-empty string
- Malformed JSON bodies are rejected
- All validation failures return HTTP 400 with `{ "error": "<message>" }`
- No retry logic on the backend — retries are handled client-side

### 3. **CORS**
- Enabled for all origins (`allow_origins=["*"]`), all methods, all headers
- Needed because the Vite frontend dev server runs on a different port than the backend

### 4. **Persistence**
- None. Every request is handled statelessly.
- No database, file, or in-memory store of messages (may be added later)

### 5. **Runtime**
- Local development only, no specific port required
- Run with `uvicorn main:app --reload --port 8000` (or any free port)
- No deployment/Docker/cloud setup at this stage

---

## Development Steps

1. ✅ Set up Python virtual environment and install FastAPI, uvicorn, pydantic
2. ✅ Build `POST /api/chat` route with request/response models matching frontend contract
3. ✅ Add validation for missing/empty/non-string `message`, returning 400 + error body
4. ✅ Enable CORS for local dev
5. ✅ Verify manually: valid message echoes correctly, empty message and missing field return 400
6. ⏳ Connect frontend by setting `VITE_API_URL=http://127.0.0.1:8000/api/chat` in `ui/.env`
7. ⏳ Replace echo logic with real chatbot/backend logic (future)
8. ⏳ Add persistence if/when needed (future)

---

## Important Notes

> **These notes describe Phase 1 as originally built.** Persistence arrived in
> Phase 3 and the echo logic was replaced in Phase 2 — both bullets are kept
> for the record, struck through, rather than quietly rewritten.

- **No authentication** for this MVP (still true)
- ~~**No persistence** — nothing is stored yet~~ → MongoDB auto-save, Phase 3
- ~~**Echo-only logic** — this backend does not yet do any real processing of the message~~ → Ollama agent, Phase 2
- **Contract is fixed** to match the existing frontend (`{ "message": string }` → `{ "response": string }`), so the frontend requires no changes to connect
- **Local dev only** — no production/deployment concerns addressed yet

---

## TODO
- ✅ Clarify requirements (DONE)
- ✅ Scaffold FastAPI backend with `/api/chat` endpoint
- ✅ Verify echo behavior and error handling locally
- ✅ Connect frontend to backend via `VITE_API_URL`
- ✅ Replace echo with real logic (see Phase 2 below)

---

## Phase 2: LLM Agent Integration (implemented)

### Overview
Replaced the echo logic with a real agent that forwards the user's message to a local LLM served by [Ollama](https://ollama.com), which may run on a different machine on the network.

### Stack additions
- `httpx` — async HTTP client used to call Ollama's API
- `python-dotenv` — loads `backend/.env` at startup

### Config (`backend/.env`, gitignored — see `.env.example`)
- `OLLAMA_BASE_URL` — e.g. `http://10.0.0.68:11434`
- `OLLAMA_MODEL` — e.g. `llama3.1:8b`

### Design
- `agent.py` exposes a single async function, `run(message: str) -> str`, which POSTs to `{OLLAMA_BASE_URL}/api/chat` (non-streaming) and returns the model's reply text.
- `main.py` calls `agent.run()` inside the `/api/chat` route; if the agent raises `AgentError` (Ollama unreachable or returns an error status), the route responds `502` with `{ "error": "<reason>" }` instead of crashing.
- The `/api/chat` request/response contract is unchanged — the frontend needed no changes.
- Conversation was single-turn through this phase: only the latest user message was sent, with a fixed system prompt. Bounded multi-turn context was added later — see Phase 7.

---

## Phase 3: MongoDB Persistence & Tool-Calling Agent (implemented)

### Overview
Move conversation storage from the frontend's `localStorage` into a local MongoDB instance, and give the agent **tool-calling** access to it — the model decides when to query or mutate conversation history, rather than the backend exposing a conventional REST CRUD API for it. This is the resume-facing centerpiece of the project: it demonstrates an agent that reasons about *when* to act on a database, not just a chatbot wired to one.

### Stack additions
- MongoDB Community Server, installed locally on the backend machine, default port `27017`
- `motor` — async MongoDB driver (matches FastAPI's async style; avoids blocking the event loop the way sync `pymongo` calls would)

### Config (`backend/.env`)
- `MONGODB_URI` — e.g. `mongodb://localhost:27017`
- `MONGODB_DB_NAME` — e.g. `ag_ai`

### Data model
One collection, `conversations`, with messages embedded as an array (idiomatic MongoDB — avoids a join for the common case of "load one conversation"):

```json
{
  "_id": "ObjectId(...)",
  "title": "optional, derived from first message",
  "created_at": "2026-09-04T18:00:00Z",
  "updated_at": "2026-09-04T18:05:00Z",
  "messages": [
    { "role": "user", "content": "...", "timestamp": "2026-09-04T18:00:00Z" },
    { "role": "assistant", "content": "...", "timestamp": "2026-09-04T18:00:02Z" }
  ]
}
```

### Two separate persistence paths — do not conflate them
1. **Auto-save (deterministic, not model-controlled).** Every request to `/api/chat` appends the user message and the assistant's reply to the relevant conversation document. This always happens; the LLM has no say in it. This is what makes history durable across page reloads.
2. **Agent tools (LLM-controlled).** The model is given a set of callable tools it may invoke when the user's message calls for it in natural language:
   - `list_conversations()` — return recent conversation summaries
   - `search_history(query: str)` — full-text search across past messages
   - `delete_conversation(conversation_id: str)` — delete a conversation
   
   The model decides *whether* and *which* tool to call based on the user's message (e.g. "what did we talk about yesterday?" → `search_history`); the agent executes the actual MongoDB operation via a Motor query and feeds the result back to the model to compose a natural-language reply. Ollama's `/api/chat` supports a `tools` field (function-calling) for tool-capable models, which `llama3.1` supports.

### Open questions — resolved during implementation (2026-09-06)

- ~~Whether `/api/chat` needs a `conversation_id` in the request/response contract~~ **Resolved:** no `conversation_id` in the API contract. There is exactly one continuously-growing conversation document (see below); `list_conversations`/`search_history`/`delete_conversation` all operate on it without the model or frontend needing to track an ID. Revisit once the frontend supports multiple/separate conversations (step 7).
- ~~Whether the frontend needs new REST endpoints (e.g. `GET /api/conversations`)~~ **Deferred, not part of this pass.** This pass is backend/agent-only — the frontend still reads from `localStorage`. Step 7 below remains its own future phase.
- ~~Tool-call error handling~~ **Resolved, see "Tool contracts" below.**

**Single-conversation model, confirmed:** the project keeps exactly one conversation document (matching the existing `save_turn` behavior). `delete_conversation` deletes that one document outright — no `conversation_id` argument. `list_conversations` returns at most one summary today; it takes an unused `limit` argument now only so its shape doesn't need to change if/when multi-conversation support (step 7) lands.

**Tool-loop design:** two-call loop, capped at one round (no recursive/chained tool calls):
1. Call Ollama's `/api/chat` with the user's message, the fixed system prompt, and the three tool schemas (`tools` field).
2. If the model's response includes `message.tool_calls`, execute **the first** requested tool call against MongoDB via the corresponding `db.py` function (a model could technically request more than one; only the first is executed to keep this bounded).
3. Append the assistant's tool-call message and a `role: "tool"` message (containing the JSON-serialized result) to the conversation sent to Ollama, and make a second `/api/chat` call (no `tools` field this time) so the model composes the final natural-language reply.
4. If the model's first response has no `tool_calls`, its `content` is returned directly — no second call.

This mirrors Ollama/OpenAI-style function calling and keeps the interesting behavior (deciding *whether* and *which* tool to call) with the model, while the final reply's phrasing also comes from the model rather than a backend template.

**Tool contracts:**
- `list_conversations(limit: int = 10)` → JSON array of `{id, title, created_at, updated_at, message_count}` for the current conversation (0 or 1 entries today). No DB error path beyond the shared one below.
- `search_history(query: str)` → uses a MongoDB **text index** on `messages.content` (created once at startup — see step 5) to find matching conversations via `$text`, then filters that conversation's `messages` array in Python for entries containing the query (case-insensitive substring) to build `{role, content, timestamp}` snippets, capped at 10 results. Empty result set is a normal (non-error) `[]`, not an error.
- `delete_conversation()` → **guarded by an explicit-confirmation check the backend performs itself, independent of what the model decides to call.** Before executing, the backend checks the *current* user message (case-insensitive) for a confirmation signal — the word `"confirm"`, or a `"yes"`/`"sure"`/`"please"` token combined with a delete-intent word (`"delete"`/`"remove"`/`"clear"`/`"wipe"`) in the same message. If absent, the tool is **not executed**; its result to the model is a JSON object explaining that confirmation is required, so the model asks the user to confirm explicitly in its reply. If present, it deletes the single conversation document and returns a success result. If no conversation exists yet, returns a "nothing to delete" result rather than an error. This confirmation gate is **not** relaxed by Phase 7's multi-turn context, even though the model can now see its own prior turn — it remains a backend keyword check on the *current* message only, so a genuine two-step confirmation still requires the user's *follow-up* message to itself restate both the delete intent and a confirmation word (e.g. "yes, please delete it").
- **Tool-execution DB errors** (MongoDB unreachable mid-tool-call, distinct from the deterministic auto-save path): caught per-tool and fed back to the model *as the tool result* (e.g. `{"error": "database unavailable"}`), not surfaced as an HTTP 502 — the request still completes with 200 and the model explains the failure in natural language. This is different from `agent.AgentError`, which is reserved for Ollama itself being unreachable/erroring and still produces a 502.

**Verified behavior (2026-09-07):** `list_conversations`, `search_history`, and the two-step delete-confirmation flow were all exercised live against MongoDB and the configured Ollama model (`llama3.1:8b`) — see verification log below. One consequence of the two independent persistence paths worth calling out: because `save_turn` (deterministic auto-save) always runs *after* `agent.run()` regardless of what the agent did, the very message that confirms a deletion is itself auto-saved as a brand-new single-turn conversation immediately after the old one is deleted. This is expected given the design (auto-save doesn't know or care that a delete just happened), not a bug — flagging it so it isn't mistaken for the delete having silently failed. Also observed: `llama3.1:8b` is somewhat tool-happy — it called a history tool even for at least one message that didn't warrant one (a plain factual statement with no history-related intent). The tool execution and results were still correct; this is a prompt-tuning opportunity for `SYSTEM_PROMPT` in `agent.py`, not a functional bug, and is left as-is for now.

**Environment note (2026-09-06):** MongoDB Community Server is confirmed installed and already running as a Windows service (`Get-Service MongoDB` → `Running`), reachable on `localhost:27017` — no install/start action was needed this pass despite this doc's step 1 note below having gone unverified for a while. Ollama at the configured `OLLAMA_BASE_URL` (`10.0.0.68:11434`) was confirmed reachable and `llama3.1:8b` confirmed to report `"capabilities": ["completion", "tools"]` via `GET /api/tags`, so tool-calling is actually supported by the configured model, not just assumed.

### Development steps
1. ✅ Install MongoDB Community Server locally; confirm it's reachable on `localhost:27017` (confirmed running as a Windows service — see environment note above)
2. ✅ Add `motor` to `requirements.txt`; add `db.py` with a Motor client reading `MONGODB_URI`
3. ✅ Add `MONGODB_URI` / `MONGODB_DB_NAME` to `.env.example` — dedicated `ag_ai` database, isolated from other local databases on this machine
4. ✅ Implement auto-save of every `/api/chat` turn to the `conversations` collection
5. ✅ Define the tool schemas and wire them into the Ollama request in `agent.py`; create the `messages.content` text index at FastAPI startup
6. ✅ Implement the tool-execution loop (model requests a tool call → agent runs it against MongoDB → result fed back to the model) — capped at one tool call per turn, confirmation-gated delete, DB errors surfaced as tool results rather than 502s
7. ⏳ Update the frontend to load conversation history from the backend instead of `localStorage` (may require new REST endpoints, see open questions) — **not part of this pass**

---

## Phase 4: Mock Bank Transactions & Financial Tool-Calling (implemented)

### Overview
Second, independent domain added to the same agent alongside conversation history: mock bank accounts/transactions, reasoned over via tool-calling in the same style as Phase 3 (`list_accounts`/`search_transactions`/`get_spending_summary` joining `list_conversations`/`search_history`/`delete_conversation` in one `TOOLS` list and one `SYSTEM_PROMPT`). No real bank integration exists yet — data is a deterministic, seeded mock dataset, chosen so the tool-calling pattern and aggregation logic can be built and demoed now, with real bank data able to slot in later behind the same `db.py` function signatures.

### Data model
Two new collections, following the same "denormalize to avoid a join" idiom `conversations.messages` already uses:

- **`accounts`** — exactly 3 fixed documents (`_id` ∈ `checking`/`savings`/`credit_card`, doubling as the tool-arg enum value): `{_id, name, type, current_balance}`.
- **`transactions`** — ~100+ documents: `{_id, account_id, account_name, account_type, date, amount, merchant, description, category, running_balance}`. Account name/type are denormalized onto each transaction for the common read path (list/filter transactions without a join back to `accounts`).

**Sign convention:** `amount`/`running_balance` mean "this account's balance moved by this much." For Checking/Savings that's literal cash; for Credit Card, positive reduces debt owed and negative increases it, so its balance trends *negative* as debt grows (e.g. `-450.00` = "you owe $450"). A card payment from Checking is a symmetric `Transfer` pair: `-300` on Checking, `+300` on Credit Card.

**Fixed categories (11):** `Groceries, Rent, Dining, Transport, Entertainment, Utilities, Income, Shopping, Healthcare, Transfer, Other` — see `db.CATEGORIES`.

### Mock data generation
The mock dataset's source of truth is two checked-in, human-editable CSV fixtures, not code:

- `backend/data/accounts.csv` — `account_id, name, type, opening_balance` (one row per account, 3 rows).
- `backend/data/transactions.csv` — `account_id, days_ago, category, merchant, description, amount` (~107 rows). Dates are stored as **`days_ago`, not absolute dates**, specifically so the checked-in file doesn't go stale: every time the seed script runs, `days_ago` is resolved against *that run's* "today," so the ledger always reads as "the last ~6 months up to now" — relative spacing between transactions (biweekly paychecks, monthly rent, etc.) is preserved, only the anchor moves. Editing either CSV by hand (e.g. adding a row, tweaking an amount) and re-seeding is the intended way to change the mock data now — there's no RNG to reason about.

`backend/scripts/seed_transactions.py` loads both CSVs, resolves `days_ago` to absolute dates against the run's current date, sorts each account's transactions chronologically, walks them forward from `opening_balance` to compute `running_balance` per transaction and `current_balance` per account (same calculation as before — only the data's origin changed, not this logic), then loads the result into MongoDB. Idempotent (clears both collections before inserting, so re-running is always safe). Run manually (`cd backend && .\.venv\Scripts\python.exe scripts\seed_transactions.py`) — **not** copied into `backend/Dockerfile`, which deliberately only ships `main.py agent.py db.py`; this is a one-off dev/demo step, not a runtime dependency (the CSVs in `backend/data/` aren't shipped either, for the same reason). `db.ensure_indexes()` (already called at FastAPI startup) additionally creates `transactions` indexes on `(account_id, date)` and `category`.

MongoDB remains the only store the agent's tools and `GET /api/transactions` read from — the CSVs are a seed-time input, not a runtime data path. Re-verified after this change: reseeding from the CSVs reproduced the exact same balances as the original RNG-generated data (Checking $24,960.16, Credit Card -$1,920.54, Savings $15,057.34), since the CSVs were themselves exported from that already-seeded, already-verified dataset rather than redrawn from scratch.

### Tool contracts
- `list_accounts()` → all 3 accounts with `current_balance`. Answers balance questions directly — no separate balance tool.
- `search_transactions(account?, category?, merchant?, start_date?, end_date?, min_amount?, max_amount?, limit=20)` → raw matching transactions, most recent first. For lookups only ("show me my Amazon purchases") — its description explicitly tells the model not to use it for totals.
- `get_spending_summary(category?, account?, start_date?, end_date?)` → **computes** total spend and a per-category breakdown in Python over Mongo-filtered outflow (`amount < 0`) transactions — never hands raw rows to the model to add up. **Excludes `Transfer` and `Income` by default** (unless a specific category is requested) so moving money between your own accounts, or receiving it, isn't counted as spending — verified live: a "how much have I spent in total" query returned exactly the DB-computed total ($13,860.38 in one verification run), matching `get_spending_summary()` called directly and excluding the Transfer/Income legs of the ledger.

Aggregation is done Python-side rather than via a Mongo `$group` pipeline: at this data volume there's no performance case for it, it mirrors `search_history`'s existing "Mongo narrows, Python finishes" style, and it's far easier to test hermetically (see `tests/test_db_transactions.py`'s `FakeCollection`, which understands only the small operator set `db.py` actually emits — not a general MongoDB emulator).

### Agent changes
- `SYSTEM_PROMPT` now covers both domains (conversation history and the 3 accounts/categories) in one string, and `run()` interpolates the current UTC date into the system message **at call time** (`f"{SYSTEM_PROMPT}\n\nToday's date is {today}..."`) so relative-date questions ("this month", "last week") resolve correctly — previously nothing told the model what day it was. `SYSTEM_PROMPT` itself stays the stable, docs-referenced persona constant; only the date suffix is computed per-call.
- **One-tool-per-turn cap is unchanged, explicitly accepted for this domain too.** A compound question ("what's my balance and how much did I spend on dining") only gets one half answered per turn, identical to today's behavior with the conversation-history tools — not something this phase fixes.

### New read-only endpoint: `GET /api/transactions`
Added to `main.py` for the frontend's transactions panel **display only** — a deliberate, scoped exception to the "no REST CRUD" stance, which is specific to conversation history (see CLAUDE.md); it is unrelated to that anti-pattern and doesn't reopen it. The agent itself still only reads this data via tool-calling, never via this endpoint. No request body/query params — the ~100+ row volume is small enough to return in one shot (internally calls `search_transactions(limit=500)`, a display cap comfortably above the seed's ~107 rows, not pagination — see the comment at that call site if the seed volume grows). Returns `{accounts, transactions}`; a Mongo error returns `503` with the existing `{error: string}` shape rather than an unhandled 500. Not covered by the `/api/chat`-only rate limiter (cheap read, no LLM cost).

### Verified behavior (2026-09-13)
Exercised live end-to-end against local MongoDB and the configured Ollama model (`gemma4:latest`): a balance question ("what's my checking balance?") correctly called `list_accounts` and returned the exact seeded balance; a category-spending question ("how much have I spent on groceries in total?") correctly called `get_spending_summary` and returned the exact computed figure ($448.51, 5 transactions) rather than a model-estimated one; a whole-ledger spending question correctly excluded all `Transfer`/`Income` legs, matching `get_spending_summary()` called directly; the pre-existing `list_conversations` tool still worked in the same session, confirming the dual-domain system prompt didn't regress Phase 3. `GET /api/transactions` and the frontend `TransactionsPanel` were verified in a real (headless) browser: 3 accounts and all ~107 transactions rendered, and the panel correctly stacks below chat instead of beside it under the existing 600px mobile breakpoint, with no console errors.

---

## Phase 5: CSV Transaction Import (implemented, rewritten for single dynamic account)

### Overview
Lets a user replace the mock/demo data with their own transaction history via a CSV upload in `TransactionsPanel`. Originally scoped to the app's own internal CSV shape with a fixed 3-account model (`checking`/`savings`/`credit_card`); revised so accounts are no longer fixed at all — an import creates exactly **one** account, named and typed by the user in the upload dialog, and the CSV itself is expected to look like a raw bank statement export (see below) rather than the app's own transaction shape. This is still deliberately scoped to one specific export shape, not a general multi-bank importer — that would need a column-mapping UI and per-bank sign/date-format heuristics, a materially bigger feature not built here.

### Account model
`db.ACCOUNT_TYPES` (`checking`/`savings`/`credit_card`) is now just the set of icon/type choices offered in the import dialog — it is no longer a fixed set of account identities. `db.import_transactions(csv_text, account_name, account_type, opening_balance=0.0)` takes the account's name/type/opening balance as arguments (collected by the frontend dialog, not read from the CSV); `account_name` is required (rejected with `ValueError` if blank after stripping) and `account_type` must be one of `ACCOUNT_TYPES`. The account's Mongo `_id` is a slug of the name (`db._slugify_account_name`, e.g. "My Card" → `my_card`). A successful import replaces the `accounts` collection outright (`delete_many({})` + `insert_one`) rather than upserting into a fixed id, since the new account may have a different name/id than whatever was there before — this now matches `scripts/seed_transactions.py`'s own full-wipe-and-reload pattern instead of diverging from it as the old upsert-per-fixed-id approach did.

### CSV format: raw bank statement
No fixed header shape is assumed beyond containing "Transaction Type" and "Date Posted" columns (case-insensitive) somewhere in the file — `db._find_statement_header` scans for the header row by content, tolerating a free-text preamble line above it (real exports include one, e.g. "Following data is valid as of ..."), and `db._find_column` locates the type/date/amount/description columns by case-insensitive substring match rather than fixed position. An identifying column (e.g. card number) is allowed and ignored — the account it belongs to is named by the user, not read from the file.

Per row: `Transaction Type` must be `DEBIT` or `CREDIT` — this, not the amount column's own sign, decides the stored sign (`-abs(amount)` for DEBIT, `abs(amount)` for CREDIT), because some exports emit all-positive amounts and carry direction only in this column; trusting the amount's sign as-given would silently turn every debit into income on such a file. `Date Posted` is `YYYYMMDD` (absolute, like the old format — not the seed CSV's relative `days_ago`, since imported data is real history that shouldn't shift on re-import). `merchant` is derived from `Description` by stripping a leading bracket tag (e.g. `[PR]`) and truncating at the first run of 2+ spaces (banks pad the merchant column out to a fixed width before a location column) — `description` keeps the full raw text.

There's no category column, so `db._classify_import_category(description, txn_type)` derives one. It checks three things in order: `_TRANSFER_DESCRIPTION_MARKERS` (a description containing `ETRNSFR` or `] TF ` is a `Transfer`, so both legs of an e-transfer are excluded from `get_spending_summary()` by the existing `NON_SPENDING_CATEGORIES` filter rather than inflating "spending"); then `_CATEGORY_KEYWORDS`, a merchant-keyword table mapping to the existing `CATEGORIES` members; then, for a credit that matched neither, `Income` — unless it matched `_REFUND_MARKERS`, since a returned purchase isn't earnings. Anything unmatched stays `DEFAULT_IMPORT_CATEGORY` ("Other") rather than being guessed at.

The keyword table is derived from the distinct merchants in a real 220-row statement plus common chains, not invented — a keyword no statement contains is dead weight, and a wrong one is worse than "Other". Every value is an existing `CATEGORIES` member: the MCP tool schemas declare that list as an enum (`test_mcp_server_tools.py` asserts they track it) and `ui/src/utils/categoryColors.ts` pins a fixed hue per category, so adding a category is a three-file change.

This replaced an earlier version that matched only the two transfer markers and never assigned `Income`, under which a freshly imported statement showed a single gray slice. It remains a heuristic, not a general categorizer — an unfamiliar merchant set will still produce a large "Other" — and there is **no backfill**: import is full-replace with no PATCH endpoint, so data imported before this change keeps its old categories until re-imported.

A prerequisite fix landed with it. `db._derive_merchant` assumed every row uses the fixed-width layout described above, but `[OP]` (online purchase) rows don't — they embed the posting date between a channel label and the vendor, space-padding single-digit days to two characters: `[OP]RECURRING PYMNT  5JUN2026SPOTIFY P433484C66` versus `[OP]RECURRING PYMNT 17AUG2026GOOGLE`. That padding put the same vendor on either side of the 2+-space split depending on the day of the month, so a monthly subscription presented as a different merchant every cycle — which meant the dashboard's recurring-payment detection could never see one, and the top-merchants card counted date-stamped duplicates as distinct vendors. `_ONLINE_PREFIX_RE` strips the label and date, and trailing province codes and per-payment references (`P433484C66` → `P4446E30CA` monthly) are dropped so one vendor yields one string.

### Validation and replace semantics
`db._parse_import_csv` (pure, unit-tested directly — no Mongo needed) validates every row before anything is written; the first invalid row raises `ValueError` with a 1-indexed *original file* line number (blank lines and the preamble don't throw off the count), and `import_transactions` never touches Mongo in that case. Deliberately fail-fast rather than skipping bad rows or coercing them to a default — a silently-miscategorized transaction is worse than a rejected upload the user can fix and retry.

### New endpoint: `POST /api/transactions/import`
Multipart form on `main.py`: `file` (the CSV) plus `account_name`, `account_type`, and optional `opening_balance` form fields (`python-multipart` in `requirements.txt` — FastAPI's `UploadFile`/`File(...)`/`Form(...)` don't work without it). Adding these form fields doesn't touch the fixed `POST /api/chat` contract in CLAUDE.md — that pin is scoped to `/api/chat` only. Same three response shapes as before: `200` with `{imported_count, accounts}`; `400` with `{error}` on a validation failure (blank name, unknown type, a bad CSV row) or non-UTF-8 file; `503` with `{error}` on an unexpected DB error. A fourth was added when the endpoint was first exposed publicly: `404` with `{error}` when `IMPORT_ENABLED` is false — see "Gating it on a public URL" below. Frontend-triggered only — the agent never calls this. The frontend's import dialog (collecting name/type/opening balance) itself carries the "this replaces all existing data" warning, replacing the old bare `window.confirm()` now that there's a real form to fill in first.

### Gating it on a public URL (added 2026-09-21)
The replace semantics above are correct for a local tool and dangerous for a public one: the endpoint takes no credential, and one request drops every account and transaction. `main.py` reads `IMPORT_ENABLED` (default `true`, so local dev and the tests above are unchanged) and returns `404` when it is false. `404` rather than `403` so a deployment with import closed looks like it never had the route.

A shared-secret header was considered and rejected. The caller is the browser (`ui/src/services/transactions.ts`), so the secret would have to reach the frontend as a `VITE_` variable, which Vite inlines into the bundle at build time — readable in devtools, therefore authenticating nobody. The real fix is a passphrase typed into the import dialog, keeping the secret out of the bundle; that is deferred, because a public deployment seeds its data from the operator's machine (`scripts/seed_transactions.py` pointed at the deployment's database) rather than importing through the app. `infra/backend-app.yaml.template` and `.env.production.example` both ship `false`.

### Verified behavior (2026-09-13, statement-format rewrite)
Hermetic tests (`tests/test_import_transactions.py`, rewritten for the new format) cover: balance math with the sign taken from `Transaction Type` rather than the (all-positive, in the sample export) amount column; fail-fast rejection of an unrecognized transaction type; the required-account-name check; merchant extraction from `[OP]` rows; and the keyword categorizer across a spread of categories.

### Verified behavior (2026-09-16, live against real MongoDB)
The single-account shape had never been exercised outside hermetic tests. It now has. A real 220-row bank statement was imported via `db.import_transactions` into a scratch database on a live local MongoDB: 220 rows written, one account created with a computed closing balance, and the result read back **through `mcp_server.dispatch_tool`** — the agent's actual path, not a direct `db` call. `get_spending_summary` returned $3,534.30 over 184 spending transactions across eight categories (Rent $1,300.00, Dining $975.27, Other $667.93, Shopping $220.30, Entertainment $206.82, Groceries $80.93, Healthcare $73.05, Transport $10.00), matching a direct computation over the same file exactly; a `category="Dining"` filter returned $975.27/125, and `search_transactions(category="Rent")` returned the two expected rows. The transfer legs (27 rows) and credits were correctly excluded from spending.

Still **not** verified live: the browser half (Upload → Preview → Dashboard against a running backend), and Phase 7's multi-turn context, which needs a reachable Ollama — the configured host was down at the time of writing.

---

## Phase 6: Docker Deployment (implemented, self-hosted production target)

### Overview
Phase 1's "no deployment/Docker/cloud setup at this stage" note (above) described that phase accurately; it's superseded, not corrected, by this one — same pattern as Phase 4/5 superseding the original fixed-3-account model. `docker-compose.yml` builds and runs two containers, `backend/Dockerfile` and `ui/Dockerfile`, for a self-hosted deployment. MongoDB and Ollama stay external services, unchanged in role from local dev (see `CLAUDE.md`) — this phase does not containerize either. See `DEPLOYMENT.md` (repo root) for the operator-facing setup steps; this section covers what changed at the code level and why.

### MongoDB: Atlas, not a container
`backend/.env.production` (gitignored; `.env.production.example` is the tracked template) sets `MONGODB_URI` to a MongoDB Atlas connection string rather than a compose-managed `mongo` service. Chosen to avoid owning volume backups/upgrades for a self-hosted single-user deployment. `main.py`'s existing `on_startup` handler already tolerates a Mongo outage at boot (catches and logs rather than crashing), which covers an unreachable Atlas cluster the same way it already covered a down local Mongo.

### Ollama: unchanged, reached over the LAN
`OLLAMA_BASE_URL` in `.env.production` points at the same LAN Ollama instance local dev uses. `host.docker.internal` (used for this and, optionally, a local-testing MongoDB — see `DEPLOYMENT.md` §1) works natively under Docker Desktop; `docker-compose.yml`'s `extra_hosts: host.docker.internal:host-gateway` on the backend service makes it resolve on Linux too.

### No seed data in the image
`backend/Dockerfile` copies only `main.py agent.py db.py` — `scripts/seed_transactions.py` and `backend/data/*.csv` are dev/demo-only and deliberately not shipped. A fresh production deployment therefore starts with zero accounts/transactions; the only way to populate one is the CSV import feature from this phase's own Phase 5 (Transactions panel → Import CSV) — the phases are complementary, not independent.

### Fixes made when finishing this phase (2026-09-13)
- `ui/Dockerfile` was baking in `VITE_API_URL` as a build arg but not `VITE_TRANSACTIONS_API_URL`; since the two must be set together (`CLAUDE.md`), the built frontend's Transactions panel would silently run against mock data in this deployment even though the backend fully supports it. Fixed by adding a matching `ARG`/`ENV` pair, defaulting to the same `/api/...` relative-path pattern `ui/nginx.conf` already proxies.
- `main.py`'s `rate_limit_middleware` keys on `request.client.host`, which is nginx's container IP for every request once behind `ui/nginx.conf`'s `proxy_pass` — collapsing the per-client-IP design into one shared bucket for all real users. Fixed by adding `--proxy-headers --forwarded-allow-ips=*` to `backend/Dockerfile`'s `CMD`, so uvicorn trusts nginx's `X-Forwarded-For` (`nginx.conf` already sets it). Trusting `*` is safe specifically because `docker-compose.yml` `expose`s (not `ports:`-publishes) port 8000 — nginx is the only possible caller.

### Not yet verified live
Like Phase 5's statement-format rewrite above, this phase has not been exercised with a real `docker compose up --build` against a real Atlas cluster and a real Cloudflare-Tunnel-style reverse proxy — verify by actually running it before relying on this for a real deployment.

---

## Phase 7: Multi-turn Conversation Context (implemented)

### Overview
Replaces Phase 2's single-turn behavior: `agent.run()` now replays a bounded window of the most recent conversation turns as context on every call, instead of only the latest user message. Bounded by a fixed turn count (not a token/char budget) — deliberately simple, matching this project's existing "narrow, not general" bias (see Phase 5's import heuristics) and its lack of any tokenizer dependency (`MAX_MESSAGE_LENGTH` is a plain character cap too).

### Config (`backend/.env`)
- `MAX_HISTORY_TURNS` — number of most recent user/assistant turn pairs replayed to the model. Default 5 (10 messages). `0` (or any non-positive value) disables history entirely — see `db.get_recent_history`'s explicit guard against the Mongo/Python "`$slice: 0` / `list[-0:]` means everything" footgun, which would otherwise silently replay the *entire* unbounded conversation. An invalid (non-integer) value logs a warning and falls back to 5 rather than crashing the app at import time, matching this repo's existing posture that a config hiccup shouldn't take down the API.

### Design
- `db.get_recent_history(max_turns=MAX_HISTORY_TURNS)` — a third, deterministic, non-model-controlled path (alongside `save_turn`), fetching the last `max_turns * 2` messages of the single conversation document via a MongoDB `$slice` projection (avoids pulling the whole, unboundedly-growing array over the wire on every request), mapped to bare `{role, content}` dicts (`timestamp` stripped). Returns `[]` if no conversation document exists yet.
- `agent.run(message: str, history: list[dict[str, str]] | None = None)` — `history` is spliced in as `[system, *history, user]`, before the existing one-round tool-calling loop, which needed **no changes**: it already operates on whatever `messages` list it's given, so a seeded history flows through both the first and (if a tool is called) second Ollama call automatically.
- Only the final `{role, content}` pairs already persisted by `save_turn` are ever replayed — intermediate tool-call/tool-result messages from past turns are never persisted in the first place, so there's nothing extra to filter out.
- `main.py` fetches history via `db.get_recent_history()` before calling `agent.run()`, and `save_turn` still runs afterward, unchanged — the current turn isn't yet in Mongo when history is fetched, so it's never duplicated into the next turn's context.
- **Graceful degradation:** if `db.get_recent_history()` raises (MongoDB unreachable), `main.py` logs (`logger.exception`, matching `save_turn`'s and `ensure_indexes`'s existing pattern) and proceeds with `history=[]` for that one request — it never fails the HTTP response.
- No `conversation_id` added to the `/api/chat` contract — history is still fetched server-side against the single conversation document, never sent by the frontend.
- The Phase 3 `delete_conversation` confirmation gate is deliberately **not** relaxed to trust the model's memory of having asked — see the updated note in Phase 3 above.

### Development steps
1. ✅ Add `MAX_HISTORY_TURNS` to `.env.example`, read once at import in `db.py` with a safe fallback on an invalid value
2. ✅ Add `db.get_recent_history()` — bounded, `$slice`-projected read of the single conversation document
3. ✅ Thread `history` through `agent.run()`, inserted before the existing tool-calling loop
4. ✅ Wire `main.py` to fetch history before `agent.run()`, degrading to `[]` on a MongoDB error without failing the request
5. ✅ Hermetic tests: `tests/test_db_conversations.py` (new), `tests/test_agent_run.py` (new — first dedicated test file for `agent.run()`), two new cases in `tests/test_chat.py`

### Not yet verified live
Only exercised via hermetic pytest (mocked `_call_ollama`/`conversations` collection) as of this writing — not yet run against a real Ollama model and a real MongoDB instance. Two specific things to check when it is:
- **Context window.** Worst case, the prompt is now `system` (persona + all 6 tool schemas) + up to 10 history messages (each up to `MAX_MESSAGE_LENGTH` = 4000 chars) + the current message — meaningfully larger than Phase 2's two-message prompt. Ollama silently truncates a prompt that exceeds the model's effective `num_ctx` rather than erroring, which could drop the system message (and with it the tool schemas and the interpolated date) with no visible error. Before relying on this in practice, confirm the configured model's effective context window (`ollama show <model>` / `GET /api/show`) comfortably covers the worst case above; if it doesn't, the fix is a `"options": {"num_ctx": N}` addition to `_call_ollama`'s payload — don't add it speculatively before confirming it's actually needed.
- **Delete-confirmation interaction.** With history now replayed, the model sees its own "do you want me to delete?" turn, so a bare "yes" reply is more likely to trigger a `delete_conversation` tool call than before — which `_is_delete_confirmed` still rejects (no delete-intent word in "yes" alone), producing a visible ask → "yes" → re-ask loop that didn't happen when the agent was memoryless (see the Phase 3 confirmation gate above — deliberately unchanged). Confirm this live; if the loop is disruptive, the fix is `SYSTEM_PROMPT` wording (tell the model what a valid confirmation reply looks like), not relaxing the backend gate.

---

## Phase 8: MCP Tool Server (implemented)

### Overview
Moves the agent's six tools out of the FastAPI backend into a standalone **MCP (Model Context Protocol) server** running as its own process, and turns the backend into an MCP **client** that discovers those tools at startup instead of hardcoding them. Supersedes Phase 3/4's "one `agent.TOOLS` list, one `agent._execute_tool` dispatch" arrangement; the tools themselves, their schemas, and their MongoDB queries are unchanged in behavior.

What this buys, concretely: adding a tool is now a one-file change in `mcp_server.py` with **no backend edit at all** — the backend learns about it on the next boot. What it costs: a second process to run, and a network hop per tool call. The `POST /api/chat` contract, the one-tool-per-turn cap, the deterministic delete guard, and the existing two error paths are all preserved, so **the frontend required zero changes.**

RAG and RBAC over this server are deliberately out of scope — separate, later specs.

### Process split
```
ui ──POST /api/chat──▶ backend (main.py, agent.py) ──MCP streamable HTTP──▶ mcp (mcp_server.py)
                              │                                                    │
                              │ save_turn / get_recent_history                     │ six tools
                              │ ensure_indexes / import_transactions               │
                              ▼                                                    ▼
                          MongoDB  ◀───── one shared db.py, one Motor pool per process ─────▶  MongoDB
```

`db.py` stays **one module imported by both processes**, not copied or reimplemented. Each process constructs its own Motor client and connection pool against the same `MONGODB_URI`/`MONGODB_DB_NAME`. `list_accounts` and `search_transactions` are called by both sides — the MCP server for the agent's tool calls, the backend for the display-only `GET /api/transactions` — and that's fine; they're reads.

The split is by *who controls the call*, matching the four-paths rule in `CLAUDE.md`:

| Path | Owner | Why |
|---|---|---|
| `ensure_indexes`, `save_turn`, `get_recent_history`, `import_transactions` | **Backend**, direct `db.py` calls | Deterministic and non-model-controlled. Exposing them over MCP would put them within the model's reach, which is exactly what Phase 3's auto-save design exists to prevent. |
| The six Tool_Set functions | **MCP server** | Model-controlled. |
| `GET /api/transactions` | **Backend**, direct `db.py` calls | Display-only, not tool-calling — unchanged from Phase 4, and independent of MCP server state. |

`agent.py` now contains no `db` call and no dispatch branch for any tool. It imports `db` for exactly one thing: the `CATEGORIES` constant interpolated into `SYSTEM_PROMPT`, which is built at import time, before any discovery has happened.

### Design
- **`mcp_server.py`** — a standalone Starlette/uvicorn process serving MCP streamable HTTP at `MCP_PATH` (`/mcp`).
  - Built on the SDK's **low-level `mcp.server.lowlevel.Server`, not `FastMCP`**. `FastMCP` derives each tool's `inputSchema` from the Python function signature, which would silently change the schemas the model sees (`str | None` becomes `anyOf: [string, null]`) and offers no clean way to attach `db.CATEGORIES` as an `enum`. `TOOL_DEFINITIONS` states all six schemas literally, moved from the old `agent.TOOLS` unchanged, so the model's behavior is identical to Phase 4's. Schema exactness is the point of the file, so the API that lets us write schemas beats the one that infers them.
  - `category`'s enum reads `db.CATEGORIES` directly in both tools that declare it, so adding a category updates both wire schemas with no other edit (asserted in `tests/test_mcp_server_tools.py`).
  - `dispatch_tool(name, arguments)` validates arguments against the declared schema with `jsonschema`, then runs the handler. **It never raises.** An unknown tool, a schema violation (missing `required`, wrong JSON type, out-of-enum `category`), and a MongoDB failure all return `{"error": ...}` — the shape `agent.py` already feeds back to the model. Validation is done here rather than via the SDK's `call_tool(validate_input=True)` because that reports rejections as an `isError` text blob; doing it ourselves keeps every failure in the one `error`-field shape.
  - `stateless=True` on the session manager: every request is self-contained, so a container restart loses no session and `mcp_client` can open a fresh connection per call.
  - Served via an exact `Route`, **not a `Mount`**. `Mount("/mcp")` only matches paths *under* `/mcp`, so a request to `/mcp` itself falls through to Starlette's `redirect_slashes` and costs every MCP call an extra 307 round-trip — observed in the access log during this phase's build, and fixed. The route's endpoint is a small callable *class* (`_StreamableHTTPEndpoint`) because Starlette wraps bare async functions in its request/response helper, which would consume the request before the session manager gets the raw ASGI streams it needs.
  - Does **not** call `ensure_indexes`, `save_turn`, `get_recent_history`, or `import_transactions`, and implements **no confirmation logic** — it deletes when `delete_conversation` is called (see below).
- **`mcp_client.py`** — the backend's half. Replaces `agent.TOOLS` entirely.
  - `discover_tools()` lists tools over MCP within a 10s timeout and caches them for the process lifetime. `_to_ollama_schema` is the single place the MCP `{name, description, inputSchema}` shape is translated into Ollama's `{"type": "function", "function": {name, description, parameters}}` shape. A tool with no name, or an `inputSchema` that isn't a JSON Schema object, is logged and excluded — discovery keeps the tools it *can* use rather than failing wholesale on one bad entry.
  - `ensure_tools()` returns the cache, retrying discovery once per request while it's empty. This is what makes a failed *startup* discovery recoverable without restarting the backend.
  - `call_tool()` invokes over MCP with a 15s bound and, like `dispatch_tool`, **never raises** — unreachable server, timeout, `isError` result, and unparseable payload all become `{"error": ...}`.
  - A fresh connection per operation rather than one long-lived `ClientSession`: the server is stateless, and sharing one session across concurrent `/api/chat` requests would need locking around a single duplex stream. Costs a round-trip, not worth the complexity at this scale.
- **`agent.py`** — `_execute_tool` shrank to routing. It applies the delete gate, refuses any tool the server didn't advertise, then hands off to `mcp_client.call_tool`. `run()` calls `mcp_client.ensure_tools()` and passes the result to Ollama; an empty list means the model is called with **no tools** and answers directly. The one-round loop is otherwise untouched.
- **`main.py`** — `on_startup` awaits `mcp_client.discover_tools()` after `ensure_indexes()`, in the same fail-soft posture: it can't raise, so a tool server that isn't up yet leaves the cache empty and startup completes anyway.

### The delete gate stays in the client, on purpose
`_is_delete_confirmed` is **not** moved to `mcp_server.py` alongside the tool it guards. It has to be a pure function of the raw current-turn user message, and the MCP server never sees that message — it only receives the arguments the model chose. So the check runs in `agent._execute_tool` *before* any invocation is sent: an unconfirmed delete produces zero MCP traffic. `mcp_server.py` deliberately implements no confirmation logic of its own; two sources of truth for a destructive action is worse than one. Protecting the MCP server from *other* MCP clients is a different problem (RBAC, a later phase) and is out of scope here.

### Error paths: MCP failure is tool-level, never a gateway failure
The two error paths from `CLAUDE.md` stay two, and MCP joins the existing tool-error one rather than adding a third:
- **Ollama unreachable/erroring** → `agent.AgentError` → **HTTP 502** with `{"error": string}`. Unchanged, and still the *only* thing that produces a 502.
- **MCP server unreachable, timing out, erroring, or returning something unparseable** → an `{"error": ...}` tool result, fed to the model as the `tool` message of the existing single round-trip → **HTTP 200** with `{"response": string}`. Same shape a MongoDB tool error already used in Phase 4, because from the model's point of view it *is* the same event: the lookup didn't work, explain that to the user.
- **`GET /api/transactions`** → **HTTP 503** on a database error only, unchanged and independent of MCP server state.

The 15s invocation bound exists so a hung MCP server can't push a turn past `_call_ollama`'s 170s budget.

### Config (`backend/.env`, `backend/.env.production`)
- `MCP_SERVER_URL` — the **one** variable the backend reads to find the MCP server. Unset or empty falls back to `http://mcp:9000/mcp`, addressing `docker-compose.yml`'s service by name, so the compose path needs nothing set alongside it. Local dev outside Docker sets `http://127.0.0.1:9000/mcp`.
- `MCP_HOST` / `MCP_PORT` — what `mcp_server.py` binds. `127.0.0.1`/`9000` for local dev; `0.0.0.0` in `.env.production` so the backend container can reach the mcp container.

### Deployment (`docker-compose.yml`)
A third service, `mcp`, matching `backend`'s posture exactly: built from `./backend` (via `Dockerfile.mcp`), same `env_file`, same `extra_hosts`, `restart: unless-stopped`, and `expose: 9000` with **no host port published** — nothing outside the compose network should reach it.

`Dockerfile.mcp` builds from the same `./backend` context and the same `requirements.txt` rather than a directory of its own: `db.py` must stay one shared module, and a second requirements file would be a second place to keep the `mcp`/`starlette` pins in sync. It copies a deliberately narrower set than `backend/Dockerfile` — `mcp_server.py db.py`, with no `main.py` (this process serves no API of its own) and no `agent.py` (Ollama is the backend's dependency, not this one's).

`backend` declares `depends_on: [mcp]` with **no healthcheck or readiness condition**, on purpose: readiness is handled by the fail-soft startup discovery plus the per-request retry described above, not by container ordering. Getting this wrong in the other direction would mean a slow-starting `mcp` container could block the whole API from serving.

### Dependency pins (`requirements.txt`)
`mcp` is held at **1.12.4** deliberately: 1.13+ requires `pydantic >= 2.11` and a `starlette` that `fastapi` 0.115 rejects, so moving up means bumping FastAPI and Pydantic in the same change. Two transitive pins exist for the same reason and are **not incidental** — remove either and `pip` breaks the backend:
- `starlette==0.38.6` — unpinned, pip resolves 1.x for `mcp`, and `fastapi` 0.115 requires `<0.39`.
- `sse-starlette==2.1.3` — 3.x requires `starlette >= 0.49.1`.

`jsonschema==4.26.0` comes in with `mcp` anyway, pinned explicitly because `mcp_server.py` imports it directly.

### Development steps
1. ✅ `mcp_server.py` — six tool schemas moved verbatim from `agent.TOOLS`, `dispatch_tool` with `jsonschema` validation, stateless streamable-HTTP app on an exact route
2. ✅ `mcp_client.py` — timeout-bounded discovery with an in-memory cache, MCP→Ollama schema translation, never-raising `call_tool`
3. ✅ `agent.py` — `TOOLS` and the `db` dispatch deleted; `_execute_tool` reduced to the delete gate + membership check + MCP hand-off; `run()` sources tools from the cache
4. ✅ `main.py` — startup discovery, fail-soft, after `ensure_indexes()`
5. ✅ `docker-compose.yml` `mcp` service + `backend/Dockerfile.mcp`; `MCP_SERVER_URL`/`MCP_HOST`/`MCP_PORT` added to `.env.example` and `.env.production(.example)`
6. ✅ Tests: `tests/test_agent_tools.py` retargeted to `mcp_server.dispatch_tool` as `tests/test_mcp_server_tools.py` (the Phase 4 assertions moved with the code, plus new schema-rejection cases); `tests/test_mcp_tool_calling.py` (new) covers the three Phase 8 behaviors; autouse MCP stubs added to `tests/test_chat.py` and `tests/test_agent_run.py` to keep them hermetic

### Verified behavior (2026-09-16)
Unlike Phases 5–7 above, this phase **was** exercised live, against a real `mcp_server.py` process, a real local Ollama (`gemma4:latest`), and a real local MongoDB holding the seeded demo data. Confirmed live:
- **Discovery and schema parity.** All six tools discovered over streamable HTTP; every discovered `parameters` object compared equal to the corresponding `TOOL_DEFINITIONS[...]["inputSchema"]`, and the `category` enum arrived over the wire as `db.CATEGORIES` in order.
- **End-to-end tool call.** `POST /api/chat` "how much did I spend on Dining?" → Ollama requested `get_spending_summary` → invoked over MCP → MongoDB → `$108.59 / 3 transactions`, matching a direct MCP invocation of the same tool byte for byte.
- **All four rejection paths**, each returning `{"error": ...}` with no DB call: out-of-enum `category`, missing `required` property, wrong JSON type, unknown tool name.
- **MCP server unreachable → HTTP 200.** With the `mcp` process killed mid-session, a balance question returned 200 and a natural-language "couldn't retrieve that right now" reply, not a 502.
- **Fail-soft startup + recovery without restart.** Backend booted with the MCP server down: startup completed, `/api/chat` served a normal reply with no tools. The MCP server was then started and the *next* request discovered the tools and answered a Groceries total correctly — no backend restart.
- **Delete gate.** "Delete my conversation history." (intent, no confirmation token) produced a re-ask reply and **zero** `CallToolRequest` entries in the MCP server's log for that turn.
- **The 307 redirect fix**, by observing the access log before and after the `Mount`→`Route` change.

Confirmed hermetically only (`pytest -m "not live_llm"`, 39 passed, no Ollama/MongoDB/MCP process needed):
- `dispatch_tool`'s argument pass-through, `limit` default, and DB-error-to-error-result contract.
- The `role: "tool"` message content handed to the second Ollama call, and that the second call is made with `tools=None`.
- That an unconfirmed `delete_conversation` reaches `mcp_client` zero times (the live check above confirmed zero requests server-side; this confirms zero *calls* client-side).

**Verified since:** the `docker compose build` + `up -d` path now runs live. All three containers build and start, `host.docker.internal` resolves from the backend, `mcp` service-name DNS resolves, all six tools are discovered over MCP, `GET /api/transactions` returns 200, and a tool-using `/api/chat` turn answers from real Mongo data (the MCP server's own Motor client, not the backend's).

One gotcha that path surfaced, which bare-process dev cannot: **if `MONGODB_URI` points at a local `mongod` running as a replica set, it needs `/?directConnection=true`.** The driver otherwise reads the replica set's advertised member list, discards the seed host, and dials `127.0.0.1:27017` — inside a container that is the container itself, so every `db.py` call fails with `ServerSelectionTimeoutError` and the transactions endpoint 503s. It hits both processes at once, since `backend` and `mcp` share one `env_file` and one `db.py`. See `DEPLOYMENT.md` §1. Not applicable to the Atlas `mongodb+srv://` string, which must not carry the option.

---

## Phase 10: Observability — structured logs and metrics (implemented)

Numbered 10, not 9: `docs/specs.md` reserves Phase 9 for the Ollama↔Claude provider switch, which remains spec-only.

### Overview
Both processes now emit one JSON object per log line and expose their own counters. Before this phase there was no measurement of any kind in the repo — no timing, no counters, no coverage — which made every performance or reliability claim about it unfalsifiable.

It also fixes a live defect. `main.py` created a module logger and called `logger.exception(...)` in each of its five fail-soft handlers, but **never configured logging**. With no root handler installed those records fell through to `logging.lastResort` — stderr, WARNING and above, no timestamp, no context — and every `logger.info` in the API process was discarded outright. The repo's only `basicConfig` sat inside `mcp_server.py`'s `if __name__ == "__main__"` block, so it covered the tool server and nothing else.

### Design
One new module, `backend/observability.py`, imported by both processes. It depends on nothing else in the package (stdlib + `prometheus_client` only), which is what lets `agent.py` and `mcp_client.py` import it without a cycle.

- **`configure_logging(service)`** installs a single stdout handler on the root logger, marked with a sentinel attribute so repeated calls don't stack duplicate handlers. `LOG_FORMAT=text` gives a human-readable formatter for local dev; JSON is the default because that is the deployed case. Uvicorn's three loggers are emptied and set to propagate, since uvicorn installs its own handlers with `propagate=False` and would otherwise put a second log shape on the same stdout. `httpx` is raised to WARNING because its per-request INFO line is a strictly poorer duplicate of `agent.py`'s own "Ollama call finished" record.
- **Request IDs** live in a `ContextVar`, not a threaded parameter: it follows the task across `await`, so `agent.py` and `mcp_client.py` stamp their lines with the originating request's id without `run()` or `call_tool()` growing an argument they have no other use for. The API adopts an inbound `X-Request-ID` when present and mints one otherwise, echoes it on the response, and forwards it to the tool server, which adopts it off the raw ASGI scope. **That is the whole cross-process trace**: one chat turn produces correlated lines in both processes.
- **Metrics** are module-level singletons (`prometheus_client` registers on construction and raises on a duplicate name). HTTP requests are labelled by *route template*, never raw path, so an unmatched or probing request cannot mint unbounded series. Histogram buckets are hand-picked per metric: the library default tops out at 10s, which would put nearly every Ollama observation in `+Inf` and make the histogram useless for exactly the case worth measuring.

### What is instrumented
| Metric | Labels | Notes |
|---|---|---|
| `tender_http_requests_total` | method, route, status class | Includes the rate limiter's 429s — the middleware is added last, so Starlette runs it outermost. |
| `tender_http_request_duration_seconds` | method, route | |
| `tender_ollama_call_duration_seconds` | phase, outcome | `phase` is `with_tools`/`no_tools`, not first/second: the tool-bearing call carries six schemas in its prompt and the follow-up does not, so splitting them keeps one slow shape from hiding in the other's average. Observed in a `finally`, so a timeout — the most diagnostic duration there is — is recorded too. |
| `tender_mcp_call_duration_seconds` | tool, outcome | |
| `tender_mcp_call_failures_total` | tool, reason | `timeout` / `unreachable` / `tool_error`. |
| `tender_tool_invocations_total` | tool, outcome | Includes `not_confirmed`, which is invisible from the server side because a blocked delete sends no invocation at all. |

### Two new endpoints, both dependency-free
- **`GET /metrics`** on the API. Deliberately not under `/api/`: `ui/nginx.conf.template` proxies only `/api/` and the Azure backend app has internal ingress, so it is unreachable from the public frontend hostname. Outside the rate limiter by the same construction as `/api/health`, and excluded from its own histogram so scrape traffic never reads as application load.
- **`GET /health`** on the MCP server. A separate route because `MCP_PATH` cannot serve as one: **verified — a bare probe `GET /mcp` returns 406**, so pointing a probe at it would report a healthy server as failing. This is why `k8s/mcp.yaml` had no probes while `backend.yaml` had two; it now has both.

Both mirror `/api/health` in touching no external dependency. Failing a probe on a MongoDB outage would have the platform restart a process that is answering correctly, which contradicts the fail-soft posture of Phase 8.

### Config (`backend/.env`)
`LOG_LEVEL` (default `INFO`) and `LOG_FORMAT` (`json` default, `text` for local dev).

### Verified behavior (2026-09-21)
Hermetically, via `TestClient` (`pytest -m "not live_llm"`, 50 passed):
- A served request produces a JSON line carrying `request_id`, `route`, `status` and `duration_ms`, and `/metrics` returns Prometheus exposition labelled with the route template.
- An inbound `X-Request-ID` is adopted rather than replaced, and echoed on the response.
- Scrapes do not appear in the app's own histogram.
- A 400 is counted, confirming the middleware sits outside the validation paths.
- `JsonFormatter` collapses a traceback into a single `exc` field on one line.
- `GET /health` on the MCP server returns 200 while `GET /mcp` returns 406 — the observation the separate route exists for.

**Not yet verified live:** the cross-process request-id correlation has been exercised only in-process. Confirming it end to end needs a running `mcp_server.py` with a real tool call, and the p50/p95 latency figures the histograms are there to produce need a load run against a deployed instance.

---

## Retrieval Architecture: what "RAG" already means here (documentation of existing behavior — no code change)

### Why this section exists
The README roadmap's open item — "RAG over the transaction/conversation data, exposed as an MCP tool" — reads as though this project has no retrieval-augmented generation at all. That's wrong, and the undersell is worth correcting: Phases 3, 4, and 8 already implement **structured RAG** end to end. What's actually missing is the *vector/semantic* half. This section names the distinction so the roadmap item can be scoped honestly, and so nobody "adds RAG" by duplicating a retrieval path that already exists.

Nothing here describes new code. It's a read of `agent.py`, `mcp_server.py`, and `db.py` as they stand after Phase 8.

### The retrieve-then-generate loop, as built
Trace "how much did I spend on groceries in August" through `agent.run()`:

1. **Query understanding.** Ollama is called with the message, the bounded history window (Phase 7), and the tool schemas discovered from the MCP server (`mcp_client.ensure_tools()`).
2. **Retrieval planning.** The model emits a structured call — `get_spending_summary({category: "Groceries", start_date: ..., end_date: ...})`. Natural language has become a machine-executable query specification. The interpolated current date (see Phase 4) is what lets "August" resolve to a range at all.
3. **Retrieval.** `mcp_server.dispatch_tool` validates those arguments against the declared JSON Schema, then `db._build_transaction_filter` compiles them into a MongoDB query and runs it.
4. **Augmentation.** The result is appended as a `{"role": "tool", "content": json.dumps(result)}` message — the retrieved context injected into the prompt.
5. **Grounded generation.** A second Ollama call, deliberately with `tools=None` (Phase 3), composes the final natural-language answer from that tool result.

That is the RAG pattern. The only structural difference from the textbook diagram is that retrieval is a schema-validated function call over structured records instead of a similarity search over text chunks.

Two distinct retrieval modes exist today, and they're worth keeping straight:

| Mode | Implementation | Corpus | Analogue |
|---|---|---|---|
| **Structured / parameterized** | `search_transactions`, `get_spending_summary` → `db._build_transaction_filter` | `transactions`, `accounts` | Text-to-query over structured data |
| **Lexical full-text** | `search_history` → MongoDB `$text` against the `messages.content` index from `ensure_indexes()` | `conversations.messages` | Keyword/BM25-style retrieval |
| **Dense / semantic** | *not implemented* | — | Embeddings + vector similarity |

`search_transactions`'s `limit` (default 20, see `mcp_server.DEFAULT_TRANSACTION_LIMIT`) and `search_history`'s `limit` are functionally the top-k bound of this system — retrieval is capped so a broad query can't flood the context window, the same reason a vector store returns k results rather than all of them.

### What this is *not*: the model doesn't author queries
The model fills in **parameters** on a fixed, enumerated filter surface. It does not write the query language. `category` is constrained by `enum: db.CATEGORIES`, dates and amounts by JSON type, and `db.py` compiles the actual MongoDB filter. Free-form text-to-SQL — where the model emits the query itself — is a different thing, and this project deliberately doesn't do it.

The safety properties that buys are load-bearing, not incidental:
- No injection surface. Nothing the model produces is ever concatenated into a query language; `merchant` is even `re.escape`d before becoming a `$regex`.
- No syntactically invalid query can reach Mongo — an out-of-enum `category`, a missing `required` field, or a wrong JSON type is rejected by `jsonschema` at the MCP boundary and returned as `{"error": ...}` before any DB call (see Phase 8's rejection-path verification).
- No unbounded scan: every retrieval path is `limit`-capped or an indexed aggregation over a filtered set.
- A rejected argument fails *loudly* as an error result, rather than silently becoming a filter that matches nothing — the specific reason validation lives in `dispatch_tool` rather than being left to Mongo.

The cost is **generality**, and that's the honest argument for doing more work here (below).

### What's genuinely missing
Three separate gaps, often conflated under "add RAG":

1. **Semantic retrieval.** There is no way to answer "did I buy anything car-related last month?" Nothing matches: no such category exists, and `search_transactions`'s `merchant` filter is an escaped substring regex, not a meaning match. This is the gap the roadmap item is really about, and it needs embeddings — plausibly from Ollama itself (`nomic-embed-text` or similar), since Ollama is already a hard dependency and no new provider or cloud cost would be involved.
2. **Arbitrary aggregation — the N-tools problem.** `get_spending_summary` groups by `category` and nothing else. No grouping by merchant, no time bucketing, no period-over-period comparison. So "which merchant did I spend the most at?", "what's my average weekly grocery spend?", and "compare August to September" have no path today — and the model can't derive them from `search_transactions` output either, because `SYSTEM_PROMPT` explicitly forbids it from adding up rows itself (correctly — that's the whole reason `get_spending_summary` computes server-side). Each new question shape currently means hand-writing another tool. Model-authored-but-validated query specs are what would collapse that into one.
3. **Category quality on imported data — largely addressed, see Phase 5.** `db._classify_import_category` used to assign `Transfer` on two description markers and `"Other"` to everything else, so on a real imported statement nearly every row was `Other` and `get_spending_summary`'s `by_category` degenerated to a single bucket. It now matches a merchant-keyword table and takes the transaction type; on the 220-row statement it was derived from, the breakdown spans eight categories with `Other` at 19% of spending. What remains is the harder half: the table is a hand-built heuristic tuned on one statement, so an unfamiliar merchant set still lands in `Other`, and there is no backfill for data imported before the change. An LLM categorization pass over unmatched rows, or merchant-level aggregation, is the natural next step — but category-driven retrieval is no longer blocked on it.

### Options for a future phase — not yet decided
Recorded so the roadmap item can be scoped, deliberately **not** specified as a design:
- **Semantic layer alongside the structured one.** Embeddings over merchant/description (and/or `messages.content`), a 7th MCP tool. Structured filters keep answering "how much", vector answers "anything car-related" — complementary, not overlapping. Storage fork matters: local MongoDB Community Server has no `$vectorSearch` (Atlas-only), so this is either brute-force cosine in Python over embeddings stored as a document field — which mirrors `get_spending_summary`'s existing "Mongo narrows, Python finishes" style and fits this data volume — or a move to Atlas / `mongodb/mongodb-atlas-local` in dev.
- **Generated structured queries.** A tool taking a constrained, model-authored aggregation spec (`{group_by, metric, filters, bucket}`) that the MCP server validates against an allowlist and compiles to a Mongo pipeline. Fixes gap 2 above and keeps every safety property in this section, since no query language crosses the wire.
- **Actual text-to-SQL.** Mirror `transactions` into SQLite/Postgres and have the model write `SELECT`s against a shown schema, executed read-only with row and statement-time caps. The most literal reading of "text-to-SQL", and the only option that introduces a second store for the same data.

Whichever is chosen, the Phase 8 boundary means it lands in `mcp_server.py` as a new entry in `TOOL_DEFINITIONS` plus its handler, with **no `agent.py` or `main.py` change** — the backend discovers it on the next boot. Embedding *generation* is a separate question from retrieval: doing it inside `save_turn`/`import_transactions` would put an Ollama call on two deterministic, non-model-controlled paths (see `CLAUDE.md`'s four-paths rule), so a `scripts/`-style backfill in the manner of `seed_transactions.py` is the lower-risk default.

### Terminology to use in docs going forward
Say **structured RAG** (or "tool-based/parameterized retrieval") for what exists, and **vector/semantic RAG** for what doesn't. The roadmap's bare "RAG" item should be read as the latter. Describing this system as having no retrieval augmentation is inaccurate; describing it as having vector search would be too.
