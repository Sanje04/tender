"""
Agent that sends a user message to a local LLM served by Ollama and returns its reply.

Ollama may be running on a different machine on the network (see OLLAMA_BASE_URL).

The agent gives the model tool-calling access to two independent domains in MongoDB:
  - Conversation history (list_conversations, search_history, delete_conversation —
    see specs.md Phase 3).
  - Mock bank accounts/transactions (list_accounts, search_transactions,
    get_spending_summary — see specs.md Phase 4).

As of specs.md Phase 8 this module no longer *implements* those tools, and holds no
tool schemas of its own. Both the schemas and the execution live in a separate MCP
server process (mcp_server.py); this module asks mcp_client for whatever tools were
discovered at startup and dispatches every call over MCP. The one exception is the
delete-confirmation gate below, which stays here on purpose.

Both domains share one tool-calling loop, capped at one tool round-trip per turn:
  1. Call Ollama with the message + the discovered tool schemas. If it doesn't
     request a tool, return its content directly.
  2. Otherwise invoke the first requested tool over MCP, send the result back as a
     "tool" message, and make a second call (no tools this time) so the model
     composes the final natural-language reply.
This cap is deliberate, not an oversight: a question needing two tool calls (e.g.
"what's my balance and how much did I spend on dining") only gets one half answered
per turn, same as today's behavior with the conversation-history tools. Don't make
this recursive without re-reading specs.md Phase 3/4.

run(message, history=None) seeds `messages` with a bounded window of recent turns
(see db.get_recent_history and specs.md Phase 7) before the loop above runs; the
loop itself is unchanged and just keeps appending to whatever `messages` it's given.
"""

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

import httpx

# `db` is imported for the CATEGORIES constant in SYSTEM_PROMPT only -- the model
# needs the category names spelled out in prose, and SYSTEM_PROMPT is built at
# import time, before any tool discovery has happened. This module makes no db
# call and owns no tool dispatch (specs.md Phase 8); everything else goes via MCP.
import db
import mcp_client
import observability

logger = logging.getLogger(__name__)

OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
# Defaults to the model this project is actually developed and evaluated
# against -- see evals/README.md. It was llama3.1:8b, which no config in the
# repo selected and no deployment ran, so the default was the one model
# guaranteed not to be installed.
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gemma4:latest")

SYSTEM_PROMPT = (
    "You are a helpful assistant with two sets of tools. First, tools to list, "
    "search, and delete the user's saved conversation history — use them when the "
    "user asks about past conversations or wants history deleted. Only call "
    "delete_conversation when the user has clearly confirmed they want their "
    "history deleted; otherwise ask them to confirm first. Second, tools over the "
    "user's imported bank account: list_accounts for the account name and "
    "balance, search_transactions for specific transaction lookups, and "
    "get_spending_summary for any total/sum question. Categories are: "
    f"{', '.join(db.CATEGORIES)}. Always use get_spending_summary for totals — "
    "never add up individual transaction amounts yourself. You can only call one "
    "tool per turn, so if a question needs two lookups, answer the first and ask "
    "the user to follow up for the second. Write replies in Markdown, using "
    "**bold** for figures worth emphasising and short bullet lists where you're "
    "listing several things. Keep it light — no headings or tables; the chat "
    "rail is narrow."
)

DELETE_INTENT_WORDS = ("delete", "remove", "clear", "wipe")
CONFIRM_TOKENS = ("confirm", "yes", "sure", "please")

# Returned instead of invoking delete_conversation when the confirmation gate
# rejects the turn. Kept as a module constant so the wording is asserted against
# in one place rather than duplicated in tests.
NOT_CONFIRMED_RESULT: dict[str, Any] = {
    "deleted": False,
    "reason": (
        "Not confirmed yet. Ask the user to explicitly confirm "
        "deletion (e.g. 'yes, please delete it') before calling "
        "this tool again."
    ),
}


class AgentError(Exception):
    """Raised when the Ollama backend can't be reached or returns an error."""


def _is_delete_confirmed(user_message: str) -> bool:
    lowered = user_message.lower()
    if "confirm" in lowered:
        return True
    has_intent = any(word in lowered for word in DELETE_INTENT_WORDS)
    has_confirm_token = any(word in lowered for word in CONFIRM_TOKENS)
    return has_intent and has_confirm_token


async def _execute_tool(name: str, arguments: dict[str, Any], user_message: str) -> dict[str, Any]:
    """
    Route one model-requested tool call to the MCP server.

    Two things happen before anything leaves the process:

    1. **The delete gate.** `delete_conversation` is checked against the raw
       current-turn user message *first*, so an unconfirmed delete sends no
       invocation at all. This gate stays here, in the client, rather than moving
       to mcp_server.py with the tool itself: it must be a pure function of the
       user's own words, and the MCP server never sees them (it only receives the
       arguments the model chose). See CLAUDE.md hard constraints.
    2. **A membership check.** A tool the server didn't advertise is refused
       locally, which also covers the "discovery failed, cache is empty, model
       hallucinated a tool call anyway" case.

    Never raises -- MCP failures come back from mcp_client as `{"error": ...}`, and
    the caller feeds that to the model as a normal tool result.
    """
    if name == "delete_conversation" and not _is_delete_confirmed(user_message):
        # Counted, not just returned: "how often does the model try to delete
        # without confirmation" is the number that says whether the gate is
        # load-bearing, and it is invisible from the MCP server's side because
        # a blocked delete sends no invocation at all.
        observability.tool_invocations_total.labels(name, "not_confirmed").inc()
        return dict(NOT_CONFIRMED_RESULT)

    if name not in mcp_client.cached_tool_names():
        observability.tool_invocations_total.labels(name, "unknown_tool").inc()
        return {"error": f"Unknown tool: {name}. It is not available on the tool server."}

    result = await mcp_client.call_tool(name, arguments)
    observability.tool_invocations_total.labels(
        name, "error" if "error" in result else "ok"
    ).inc()
    return result


async def _call_ollama(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": OLLAMA_MODEL,
        "messages": messages,
        "stream": False,
    }
    if tools:
        payload["tools"] = tools

    # Labelled by whether schemas were sent rather than by call order, because
    # that is the difference worth measuring: the tool-bearing call carries the
    # six schemas in its prompt and the follow-up call does not, so splitting
    # them keeps one slow shape from hiding inside the other's average.
    phase = "with_tools" if tools else "no_tools"
    started = time.perf_counter()
    outcome = "error"
    try:
        # A cold model load (multi-GB) plus real inference can take well over
        # 60s -- keep this at or above nginx's proxy_read_timeout (nginx.conf.template)
        # so the backend, not the reverse proxy, is what decides "too slow."
        async with httpx.AsyncClient(timeout=170.0) as client:
            response = await client.post(f"{OLLAMA_BASE_URL}/api/chat", json=payload)
            response.raise_for_status()
        outcome = "ok"
    except httpx.RequestError as exc:
        raise AgentError(f"Could not reach Ollama at {OLLAMA_BASE_URL}: {exc}") from exc
    except httpx.HTTPStatusError as exc:
        raise AgentError(f"Ollama returned an error: {exc.response.status_code}") from exc
    finally:
        # In `finally` so a failed call is timed too -- a timeout is the single
        # most useful duration to have on record, and returning early from the
        # except branches would be exactly when it went missing.
        duration = time.perf_counter() - started
        observability.ollama_call_duration_seconds.labels(phase, outcome).observe(duration)
        logger.info(
            "Ollama call finished",
            extra={
                "phase": phase,
                "outcome": outcome,
                "model": OLLAMA_MODEL,
                "duration_ms": round(duration * 1000, 1),
            },
        )

    return response.json()


async def run(message: str, history: list[dict[str, str]] | None = None) -> str:
    # SYSTEM_PROMPT is a static constant, so nothing else tells the model what
    # day it is -- required for it to resolve relative dates ("this month",
    # "last week") in transaction questions. Interpolated per call, not baked
    # into the constant, so SYSTEM_PROMPT stays the stable, docs-referenced
    # persona text.
    today = datetime.now(timezone.utc).date().isoformat()
    system_content = f"{SYSTEM_PROMPT}\n\nToday's date is {today}. Resolve relative dates (e.g. \"this month\", \"last week\") against this."

    messages: list[dict[str, Any]] = [{"role": "system", "content": system_content}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": message})

    # Cached after the first success; retries discovery once per request while the
    # cache is empty. An empty list means "MCP server unavailable" -- call Ollama
    # with no tools and let it answer directly rather than failing the turn.
    tools = await mcp_client.ensure_tools()

    first = await _call_ollama(messages, tools=tools or None)
    assistant_message = first["message"]
    tool_calls = assistant_message.get("tool_calls")

    if not tool_calls:
        return assistant_message["content"]

    call = tool_calls[0]["function"]
    name = call["name"]
    raw_arguments = call.get("arguments") or {}
    arguments = raw_arguments if isinstance(raw_arguments, dict) else json.loads(raw_arguments)

    result = await _execute_tool(name, arguments, message)

    messages.append(assistant_message)
    messages.append({"role": "tool", "content": json.dumps(result, default=str)})

    second = await _call_ollama(messages, tools=None)
    return second["message"]["content"]
