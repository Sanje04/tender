"""
Standalone MCP (Model Context Protocol) server exposing the agent's six tools
over streamable HTTP -- see specs.md Phase 8.

This is a *second process*, not a FastAPI route. `main.py`'s backend is now an
MCP *client* (see mcp_client.py) that discovers these tools at startup instead
of holding an `agent.TOOLS` literal, so adding a tool here needs no backend
code change at all.

Why the low-level `mcp.server.lowlevel.Server` API rather than `FastMCP`:
FastMCP derives each tool's `inputSchema` from the Python function signature,
which would silently change the schemas the model sees (`str | None` becomes
`anyOf: [string, null]`, and there's no way to attach `db.CATEGORIES` as an
`enum` without a `Literal` hack). The schemas below are the *same* schemas
`agent.TOOLS` declared before this phase, character for character, so the
model's behavior is unchanged by the move. Schema exactness is the whole point
of this file existing, so the API that lets us state schemas literally wins
over the one that infers them.

Two deliberate non-responsibilities:
  - **No confirmation logic.** `delete_conversation` deletes when called. The
    confirmation gate stays in agent.py, evaluated against the raw user message
    before any invocation is sent (see CLAUDE.md hard constraints / specs.md
    Phase 3). Duplicating it here would mean two sources of truth for a
    destructive action; protecting this server from *other* MCP clients is a
    separate concern (RBAC, a later phase).
  - **No conversation persistence.** `save_turn`/`get_recent_history`/
    `ensure_indexes`/`import_transactions` are deterministic, non-model-controlled
    backend paths and are deliberately not exposed here -- the model must not be
    able to reach them (specs.md Phase 8, Requirement 6.4).

`db.py` is imported, not copied: both processes share the one module and each
gets its own Motor client/connection pool against the same MongoDB URI.
"""

import contextlib
import logging
import os
from typing import Any, Awaitable, Callable

import jsonschema
from dotenv import load_dotenv
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

load_dotenv()

import db
import observability

# At import time, not inside `if __name__ == "__main__"` where the old
# basicConfig call lived: Dockerfile.mcp runs this file directly, but serving it
# as `mcp_server:app` under an external uvicorn skips __main__ entirely and used
# to leave the tool server with no log configuration at all.
observability.configure_logging("mcp")

logger = logging.getLogger(__name__)

MCP_HOST = os.environ.get("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.environ.get("MCP_PORT", "9000"))
MCP_PATH = "/mcp"

SERVER_NAME = "tender-tools"

# Defaults applied when the model omits `limit` -- previously inline in
# agent._execute_tool, moved here with the tools themselves so the backend
# holds no per-tool knowledge at all.
DEFAULT_CONVERSATION_LIMIT = 10
DEFAULT_TRANSACTION_LIMIT = 20


# ---------------------------------------------------------------------------
# Tool schemas
#
# The single source of truth for what tools exist. Moved verbatim from the old
# agent.TOOLS (minus its Ollama-specific {"type": "function", "function": {...}}
# envelope, which mcp_client.py re-adds on the way back). `category`'s enum
# reads db.CATEGORIES directly, so adding a category there updates both tools'
# declared enums with no other edit.
# ---------------------------------------------------------------------------
TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "list_conversations",
        "description": (
            "List recent saved conversations, most recently updated first. "
            "Use this when the user asks what conversations or chat history exist."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of conversations to return.",
                }
            },
            "required": [],
        },
    },
    {
        "name": "search_history",
        "description": (
            "Search past conversation messages for a keyword or phrase. Use this "
            "when the user asks what was discussed previously, e.g. "
            "'what did we talk about yesterday?'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The keyword or phrase to search for in past messages.",
                }
            },
            "required": ["query"],
        },
    },
    {
        "name": "delete_conversation",
        "description": (
            "Permanently delete the user's saved conversation history. Only call "
            "this when the user has explicitly asked to delete/clear/remove their "
            "history AND clearly confirmed they want to proceed in the same message."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "list_accounts",
        "description": (
            "List the user's financial account(s) with their current balances "
            "and names. Use this for balance questions, e.g. 'what's my "
            "balance?', or to find out what an account is called."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "search_transactions",
        "description": (
            "Look up individual transactions, optionally filtered by account, "
            "category, merchant, date range, or amount range. Use this for "
            "specific lookups, e.g. 'show me transactions from Amazon' or "
            "'what did I buy last week'. Do NOT use this to compute totals — "
            "use get_spending_summary for that."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "account": {
                    "type": "string",
                    "description": (
                        "Restrict results to this account (its id, from "
                        "list_accounts). Usually unnecessary — there's normally "
                        "just one account."
                    ),
                },
                "category": {
                    "type": "string",
                    "enum": db.CATEGORIES,
                    "description": "Restrict results to this category.",
                },
                "merchant": {
                    "type": "string",
                    "description": "Filter by merchant name (partial match), e.g. 'Amazon'.",
                },
                "start_date": {
                    "type": "string",
                    "description": "Only transactions on/after this date (YYYY-MM-DD).",
                },
                "end_date": {
                    "type": "string",
                    "description": "Only transactions on/before this date (YYYY-MM-DD).",
                },
                "min_amount": {
                    "type": "number",
                    "description": "Only transactions with amount >= this value.",
                },
                "max_amount": {
                    "type": "number",
                    "description": "Only transactions with amount <= this value.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of transactions to return (default 20).",
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_spending_summary",
        "description": (
            "Compute total spending and a category breakdown, optionally "
            "filtered by category, account, or date range. Always use this for "
            "questions asking for a total/sum — e.g. 'how much did I spend on "
            "groceries in August' — rather than adding up individual "
            "transactions yourself. Excludes transfers between the user's own "
            "accounts and income by default."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": db.CATEGORIES,
                    "description": "Restrict to this category.",
                },
                "account": {
                    "type": "string",
                    "description": (
                        "Restrict to this account (its id, from list_accounts). "
                        "Usually unnecessary — there's normally just one account."
                    ),
                },
                "start_date": {
                    "type": "string",
                    "description": "Only spending on/after this date (YYYY-MM-DD).",
                },
                "end_date": {
                    "type": "string",
                    "description": "Only spending on/before this date (YYYY-MM-DD).",
                },
            },
            "required": [],
        },
    },
]

_SCHEMAS_BY_NAME: dict[str, dict[str, Any]] = {t["name"]: t["inputSchema"] for t in TOOL_DEFINITIONS}


# ---------------------------------------------------------------------------
# Tool implementations
#
# Each returns the exact same shape agent._execute_tool returned before this
# phase -- the wrapping keys (`conversations`, `results`, `accounts`,
# `transactions`) and the two unwrapped dicts (delete_conversation,
# get_spending_summary) are part of what the model was tuned against, so they
# moved unchanged rather than being "tidied up" in transit.
# ---------------------------------------------------------------------------
async def _list_conversations(arguments: dict[str, Any]) -> dict[str, Any]:
    limit = arguments.get("limit") or DEFAULT_CONVERSATION_LIMIT
    return {"conversations": await db.list_conversations(limit=limit)}


async def _search_history(arguments: dict[str, Any]) -> dict[str, Any]:
    return {"results": await db.search_history(arguments.get("query", ""))}


async def _delete_conversation(arguments: dict[str, Any]) -> dict[str, Any]:
    # No confirmation check here by design -- see the module docstring.
    return await db.delete_conversation()


async def _list_accounts(arguments: dict[str, Any]) -> dict[str, Any]:
    return {"accounts": await db.list_accounts()}


async def _search_transactions(arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "transactions": await db.search_transactions(
            account=arguments.get("account"),
            category=arguments.get("category"),
            merchant=arguments.get("merchant"),
            start_date=arguments.get("start_date"),
            end_date=arguments.get("end_date"),
            min_amount=arguments.get("min_amount"),
            max_amount=arguments.get("max_amount"),
            limit=arguments.get("limit") or DEFAULT_TRANSACTION_LIMIT,
        )
    }


async def _get_spending_summary(arguments: dict[str, Any]) -> dict[str, Any]:
    return await db.get_spending_summary(
        category=arguments.get("category"),
        account=arguments.get("account"),
        start_date=arguments.get("start_date"),
        end_date=arguments.get("end_date"),
    )


_HANDLERS: dict[str, Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]] = {
    "list_conversations": _list_conversations,
    "search_history": _search_history,
    "delete_conversation": _delete_conversation,
    "list_accounts": _list_accounts,
    "search_transactions": _search_transactions,
    "get_spending_summary": _get_spending_summary,
}


async def dispatch_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """
    Validate `arguments` against the tool's declared schema, then run it.

    Never raises: an unknown tool, a schema violation, and a MongoDB failure
    all come back as `{"error": ...}`. That's the shape agent.py already feeds
    back to the model as a normal `tool` message, so a broken tool call stays a
    200 with a natural-language explanation rather than becoming a 5xx (see
    CLAUDE.md's "two distinct error paths, don't merge them").

    Validation is done here with `jsonschema` rather than via the SDK's
    `call_tool(validate_input=True)`, which reports rejections as an `isError`
    text blob -- doing it ourselves keeps *every* failure in the one `error`-field
    shape the model and the tests both expect.
    """
    schema = _SCHEMAS_BY_NAME.get(name)
    if schema is None:
        return {"error": f"Unknown tool: {name}"}

    try:
        jsonschema.validate(instance=arguments, schema=schema)
    except jsonschema.ValidationError as exc:
        return {"error": f"Invalid arguments for {name}: {exc.message}"}

    try:
        return await _HANDLERS[name](arguments)
    except Exception as exc:
        logger.exception("Tool %s failed", name)
        return {"error": f"Database error while executing {name}: {exc}"}


# ---------------------------------------------------------------------------
# MCP wiring
# ---------------------------------------------------------------------------
server: Server = Server(SERVER_NAME)


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(name=t["name"], description=t["description"], inputSchema=t["inputSchema"])
        for t in TOOL_DEFINITIONS
    ]


# validate_input=False because dispatch_tool() validates itself, in the
# error-field shape described above.
@server.call_tool(validate_input=False)
async def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return await dispatch_tool(name, arguments or {})


class _StreamableHTTPEndpoint:
    """
    Raw-ASGI adapter handing a request straight to the MCP session manager.

    A class rather than a plain async function on purpose: Starlette's `Route`
    wraps bare functions in its request/response helper, which would consume the
    request before the session manager (which needs the raw ASGI streams to do
    streamable HTTP at all) ever sees it. Anything that isn't a function or
    method is used as an ASGI app as-is, which is what we want.
    """

    def __init__(self, session_manager: StreamableHTTPSessionManager) -> None:
        self._session_manager = session_manager

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        # Adopt the caller's request id so this process's log lines carry the same
        # id as the /api/chat turn that triggered them. Read off the raw ASGI
        # scope rather than a Starlette Request because this endpoint deliberately
        # never builds one (see the class docstring) -- headers there are a list
        # of lowercased (bytes, bytes) pairs.
        for key, value in scope.get("headers") or []:
            if key == b"x-request-id":
                observability.set_request_id(value.decode("latin-1", "replace")[:64])
                break
        await self._session_manager.handle_request(scope, receive, send)


def build_app() -> Starlette:
    """
    Starlette app serving MCP streamable HTTP at MCP_PATH.

    `stateless=True`: every request is self-contained, so there's no session to
    lose when a container restarts and no server-side state to keep in sync --
    which also means mcp_client.py can open a fresh connection per call instead
    of sharing one long-lived session across concurrent chat requests.

    An exact `Route`, not a `Mount`: `Mount("/mcp")` only matches paths *under*
    /mcp, so a request to /mcp itself falls through to Starlette's
    redirect_slashes and costs every single MCP call an extra 307 round-trip
    to /mcp/. Verified: with a Mount, each tool invocation logged three 307s.
    """
    session_manager = StreamableHTTPSessionManager(app=server, json_response=False, stateless=True)

    async def health(_request: Any) -> JSONResponse:
        """Liveness/readiness probe for this process (k8s/mcp.yaml).

        A separate route because MCP_PATH cannot serve as one: it speaks
        streamable HTTP and rejects a bare probe GET that carries none of the
        session headers it expects, so probing it would report a healthy server
        as failing. An ordinary async function here, unlike the MCP endpoint
        below, precisely because this one *wants* Starlette's request/response
        wrapping.

        Mirrors main.py's /api/health in touching nothing: MongoDB is reachable
        or not per tool call, and failing the probe on a database outage would
        have Kubernetes restart a server that is answering correctly.
        """
        return JSONResponse({"status": "ok"})

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):
        async with session_manager.run():
            yield

    return Starlette(
        debug=False,
        routes=[
            Route("/health", endpoint=health, methods=["GET"]),
            Route(
                MCP_PATH,
                endpoint=_StreamableHTTPEndpoint(session_manager),
                methods=["GET", "POST", "DELETE"],
            ),
        ],
        lifespan=lifespan,
    )


app = build_app()


if __name__ == "__main__":
    import uvicorn

    logger.info("Serving MCP tools on http://%s:%d%s", MCP_HOST, MCP_PORT, MCP_PATH)
    uvicorn.run(app, host=MCP_HOST, port=MCP_PORT)
