"""
Structured logging and Prometheus metrics, shared by both backend processes.

Imported by main.py (the API) and mcp_server.py (the tool server). Depends on
nothing else in this package -- only stdlib and prometheus_client -- so it can
be imported from agent.py and mcp_client.py without an import cycle.

Three things live here:

1. **Logging configuration.** Until now `main.py` never configured logging at
   all: it created a module logger and called `logger.exception(...)` in its
   fail-soft handlers, but with no root handler installed those records fell
   through to `logging.lastResort` (stderr, WARNING and above, no timestamp)
   and every `logger.info` was dropped silently. The only `basicConfig` in the
   repo sat inside mcp_server.py's `if __name__ == "__main__"` block, so it
   applied to the tool server and nothing else. `configure_logging()` fixes
   that for both processes.

2. **A request-ID context variable.** A `ContextVar` rather than a parameter
   threaded through call signatures: it follows the task across `await` points,
   so agent.py and mcp_client.py can stamp their own log lines with the id of
   the request that caused them without `run()` and `call_tool()` growing an
   argument they'd otherwise have no use for. The API generates one per request
   and forwards it to the tool server as `X-Request-ID`, which the server adopts
   (see mcp_server.py) -- that is what makes one chat turn traceable across the
   process boundary.

3. **Metric definitions.** Module-level singletons, because prometheus_client
   registers each metric in a global registry on construction and a second
   construction with the same name raises. Defining them here once means the
   API process and the tool server share the definitions without either one
   re-registering.

Why JSON lines: container platforms collect stdout and parse it. A wrapped,
human-formatted traceback is one log event spread over N lines, which is exactly
what a collector can't group. `exc_info` is rendered into a single `exc` string
field here for that reason.
"""

import json
import logging
import os
import sys
import time
import uuid
from contextvars import ContextVar
from typing import Any

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

# "-" rather than "" so a log line emitted outside any request (startup,
# shutdown, a background task) is visibly unattributed instead of looking like
# a request whose id went missing.
NO_REQUEST_ID = "-"

_request_id: ContextVar[str] = ContextVar("request_id", default=NO_REQUEST_ID)

# Marks the handler this module installs, so configure_logging() can be called
# twice (both processes import this; tests import main repeatedly) without
# stacking duplicate handlers and double-printing every line.
_HANDLER_FLAG = "_tender_observability_handler"


def new_request_id() -> str:
    """A short correlation id. 12 hex chars is plenty to disambiguate concurrent
    requests in a log file while staying readable when grepped by eye."""
    return uuid.uuid4().hex[:12]


def current_request_id() -> str:
    return _request_id.get()


def set_request_id(value: str) -> None:
    _request_id.set(value)


class JsonFormatter(logging.Formatter):
    """One JSON object per line, with the active request id attached.

    Any extra keyword passed as `extra={...}` at the call site is copied onto
    the record and serialized too, so adding a field to one log line doesn't
    require touching this class.
    """

    # Attributes LogRecord always carries; anything outside this set was added
    # by an `extra=` at the call site and is worth emitting.
    _RESERVED = frozenset(
        vars(logging.LogRecord("", 0, "", 0, "", None, None)).keys()
        | {"asctime", "message", "taskName"}
    )

    def __init__(self, service: str) -> None:
        super().__init__()
        self._service = service

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "service": self._service,
            "logger": record.name,
            "request_id": getattr(record, "request_id", None) or current_request_id(),
            "msg": record.getMessage(),
        }

        for key, value in record.__dict__.items():
            if key in self._RESERVED or key in payload:
                continue
            try:
                json.dumps(value)
            except (TypeError, ValueError):
                value = repr(value)
            payload[key] = value

        # Collapsed into one field rather than left to the default multi-line
        # rendering, so one exception stays one log event.
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)


def configure_logging(service: str) -> None:
    """Install a single stdout handler on the root logger.

    `LOG_FORMAT=text` falls back to a plain human-readable formatter, which is
    what you want when tailing a local dev server by eye; JSON is the default
    because that is the deployed case. `LOG_LEVEL` overrides the INFO default.

    Uvicorn's own loggers are re-pointed at this handler rather than left alone.
    Uvicorn installs handlers with `propagate=False` when it starts, so without
    this its access lines would keep their own format and bypass the JSON
    stream -- giving a deployment two different log shapes on one stdout.
    """
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    root = logging.getLogger()
    root.setLevel(level)

    for existing in root.handlers:
        if getattr(existing, _HANDLER_FLAG, False):
            existing.setLevel(level)
            return

    handler = logging.StreamHandler(sys.stdout)
    if os.getenv("LOG_FORMAT", "json").lower() == "text":
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s"))
    else:
        handler.setFormatter(JsonFormatter(service))
    handler.setLevel(level)
    setattr(handler, _HANDLER_FLAG, True)
    root.addHandler(handler)

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True

    # httpx logs one INFO line per outbound request, which on this codebase means
    # a near-duplicate of agent.py's own "Ollama call finished" line -- same event,
    # less detail, no duration. Raised to WARNING so the richer line is the only
    # record of a model call; genuine transport errors still come through.
    logging.getLogger("httpx").setLevel(logging.WARNING)


# --- Metrics ---------------------------------------------------------------
#
# Bucket boundaries are hand-picked per metric rather than left at the
# prometheus_client default (which tops out at 10s). An Ollama call on a cold
# model load can legitimately take over a minute, so the default buckets would
# put nearly every observation in +Inf and make the histogram useless for exactly
# the case worth measuring.

http_requests_total = Counter(
    "tender_http_requests_total",
    "HTTP requests handled, by route template and status class.",
    ["method", "route", "status"],
)

http_request_duration_seconds = Histogram(
    "tender_http_request_duration_seconds",
    "Wall-clock time to serve an HTTP request, by route template.",
    ["method", "route"],
    buckets=(0.005, 0.025, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, float("inf")),
)

ollama_call_duration_seconds = Histogram(
    "tender_ollama_call_duration_seconds",
    "Time for one Ollama /api/chat round-trip.",
    ["phase", "outcome"],
    buckets=(0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 30.0, 60.0, 120.0, 170.0, float("inf")),
)

mcp_call_duration_seconds = Histogram(
    "tender_mcp_call_duration_seconds",
    "Time for one MCP tool invocation, measured client-side.",
    ["tool", "outcome"],
    buckets=(0.005, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 15.0, float("inf")),
)

mcp_call_failures_total = Counter(
    "tender_mcp_call_failures_total",
    "MCP invocations that did not return a usable result, by failure reason.",
    ["tool", "reason"],
)

tool_invocations_total = Counter(
    "tender_tool_invocations_total",
    "Tool calls the model requested, by tool and how the request was resolved.",
    ["tool", "outcome"],
)


def render_metrics() -> tuple[bytes, str]:
    """The Prometheus exposition payload and its content type."""
    return generate_latest(), CONTENT_TYPE_LATEST
