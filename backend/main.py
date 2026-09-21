"""
Chatbot backend API.

Exposes POST /api/chat, matching the frontend's contract:
  Request:  { "message": "<user text>" }
  Response: { "response": "<agent's reply>" }

Messages are forwarded to a local LLM served by Ollama (see agent.py), along with
a bounded window of recent conversation history (see db.get_recent_history).
Every turn is auto-saved to MongoDB (see db.py) regardless of the LLM's behavior.
"""

import logging
import os
import time
from collections import defaultdict, deque

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, Request, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.httpsredirect import HTTPSRedirectMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, ValidationError

load_dotenv()

import agent
import db
import mcp_client
import observability

# Installs the root log handler for this process. Called at import time, after
# load_dotenv() so LOG_LEVEL/LOG_FORMAT from .env are visible, and before the app
# is built so anything module-level below can log. Without this the API process
# has no root handler at all and every logger.info() it makes is discarded.
observability.configure_logging("api")

logger = logging.getLogger(__name__)

METRICS_PATH = "/metrics"

app = FastAPI(title="Tender Chatbot Backend")

MAX_MESSAGE_LENGTH = 4000  # keep in sync with ui/src/constants.ts

# Off by default so local HTTP dev keeps working; set FORCE_HTTPS=true behind a
# real TLS-terminating deployment (reverse proxy, load balancer, etc.).
if os.getenv("FORCE_HTTPS", "false").lower() == "true":
    app.add_middleware(HTTPSRedirectMiddleware)

# On by default so local dev and the compose deployment are unchanged, but set to
# false on any public URL: POST /api/transactions/import is unauthenticated and
# destructive (a full accounts/transactions replace -- see specs.md Phase 5), so
# on a public hostname anyone with the link could wipe the data. It can't be fixed
# with a shared-secret header, because the caller is the browser and a build-time
# secret ships inside the JS bundle. A deployment therefore seeds its data once
# (backend/scripts/seed_transactions.py) and turns this off -- see AZURE_DEPLOYMENT.md.
IMPORT_ENABLED = os.getenv("IMPORT_ENABLED", "true").lower() == "true"


# Fixed-window rate limit per client IP, to slow down basic chat spam/flooding.
RATE_LIMIT_MAX_REQUESTS = 20
RATE_LIMIT_WINDOW_SECONDS = 60
_request_log: dict[str, deque[float]] = defaultdict(deque)


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    if request.url.path == "/api/chat":
        client_ip = request.client.host if request.client else "unknown"
        now = time.monotonic()
        log = _request_log[client_ip]
        while log and now - log[0] > RATE_LIMIT_WINDOW_SECONDS:
            log.popleft()
        if len(log) >= RATE_LIMIT_MAX_REQUESTS:
            return JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={"error": "Too many requests. Please slow down and try again shortly."},
            )
        log.append(now)
    return await call_next(request)


@app.on_event("startup")
async def on_startup() -> None:
    # Mirrors save_turn's resilience (main.py chat()): a Mongo outage at boot
    # must not take down the whole API, since chat itself doesn't depend on it.
    try:
        await db.ensure_indexes()
    except Exception:
        logger.exception("Failed to ensure MongoDB indexes at startup")

    # Discover the agent's tools from the MCP server (specs.md Phase 8). Same
    # posture as ensure_indexes above: mcp_client.discover_tools() never raises,
    # so a tool server that isn't up yet leaves the cache empty and startup
    # completes anyway -- the next /api/chat retries discovery once, and until it
    # succeeds the model is simply called with no tools. This is why
    # docker-compose.yml's `depends_on` deliberately has no healthcheck
    # condition: readiness is handled here, not by container ordering.
    await mcp_client.discover_tools()


def _load_allowed_origins() -> list[str]:
    """Origins permitted by CORS, from a comma-separated ALLOWED_ORIGINS.

    Defaults to "*" so local dev (Vite on a different port) keeps working with no
    .env change. A real deployment should pin this to the one frontend hostname:
    because nginx proxies /api/ to this backend (ui/nginx.conf.template), traffic is
    same-origin there and needs no wildcard -- see AZURE_DEPLOYMENT.md.
    """
    raw = os.getenv("ALLOWED_ORIGINS", "*")
    origins = [origin.strip() for origin in raw.split(",") if origin.strip()]
    if not origins:
        logger.warning("ALLOWED_ORIGINS=%r contained no usable values; falling back to '*'", raw)
        return ["*"]
    return origins


# Enable CORS so a cross-origin frontend (Vite on another port in dev) can call
# this API. Wide open by default, narrowed by ALLOWED_ORIGINS in deployment.
app.add_middleware(
    CORSMiddleware,
    allow_origins=_load_allowed_origins(),
    allow_methods=["*"],
    allow_headers=["*"],
)


def _route_template(request: Request) -> str:
    """The matched route's path template, not the raw request path.

    Every distinct label value is a separate time series, so labelling with the
    raw path would let unmatched or probing requests mint unbounded series in the
    registry. This app has only a handful of routes today; the bound is the point.
    """
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else "__unmatched__"


# Added last, so Starlette runs it outermost: the request id therefore exists
# before anything downstream can log, and the recorded duration covers the whole
# stack -- including a 429 from the rate limiter above, which would otherwise be
# the one response class that never showed up in the metrics.
@app.middleware("http")
async def observability_middleware(request: Request, call_next):
    # Adopt an inbound id if a proxy or caller already set one, so a request keeps
    # a single identity end to end; mint one otherwise.
    request_id = request.headers.get("x-request-id") or observability.new_request_id()
    observability.set_request_id(request_id)

    # Scrapes are excluded from the app's own metrics so monitoring traffic never
    # reads as application load.
    if request.url.path == METRICS_PATH:
        return await call_next(request)

    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        duration = time.perf_counter() - started
        route = _route_template(request)
        observability.http_requests_total.labels(request.method, route, "5xx").inc()
        observability.http_request_duration_seconds.labels(request.method, route).observe(duration)
        logger.exception(
            "%s %s failed",
            request.method,
            request.url.path,
            extra={"route": route, "duration_ms": round(duration * 1000, 1)},
        )
        raise

    duration = time.perf_counter() - started
    route = _route_template(request)
    status_class = f"{response.status_code // 100}xx"
    observability.http_requests_total.labels(request.method, route, status_class).inc()
    observability.http_request_duration_seconds.labels(request.method, route).observe(duration)

    # Echoed so a caller (or the frontend, in a bug report) can quote the id that
    # identifies their request in the server logs.
    response.headers["X-Request-ID"] = request_id
    logger.info(
        "%s %s -> %d",
        request.method,
        request.url.path,
        response.status_code,
        extra={
            "route": route,
            "status": response.status_code,
            "duration_ms": round(duration * 1000, 1),
        },
    )
    return response


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=MAX_MESSAGE_LENGTH)


class ChatResponse(BaseModel):
    response: str


class ChatError(BaseModel):
    error: str


class HealthResponse(BaseModel):
    status: str


class AccountOut(BaseModel):
    id: str
    name: str
    type: str
    current_balance: float


class TransactionOut(BaseModel):
    id: str
    account_id: str
    account_name: str
    account_type: str
    date: str
    amount: float
    merchant: str
    description: str
    category: str
    running_balance: float


class TransactionsResponse(BaseModel):
    accounts: list[AccountOut]
    transactions: list[TransactionOut]


class ImportResult(BaseModel):
    imported_count: int
    accounts: list[AccountOut]


@app.exception_handler(ValidationError)
async def validation_exception_handler(request: Request, exc: ValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content=ChatError(
            error=f"Invalid request: 'message' must be a non-empty string of at most {MAX_MESSAGE_LENGTH} characters."
        ).model_dump(),
    )


@app.get("/api/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Liveness probe for the container platform (see AZURE_DEPLOYMENT.md).

    Deliberately touches nothing: no MongoDB, no Ollama, no MCP server. Those are
    all external dependencies the app is designed to outlive (startup fails soft on
    each -- see on_startup above), so letting them fail this probe would have the
    platform restart or drain a container that is serving correctly. This reports
    "this process is up", not "the whole system is healthy".

    Outside the rate limiter by construction: the middleware matches /api/chat only,
    so probe traffic can never consume a client's chat budget.
    """
    return HealthResponse(status="ok")


@app.get(METRICS_PATH, include_in_schema=False)
async def metrics() -> Response:
    """Prometheus exposition for this process.

    Deliberately not under /api/: ui/nginx.conf.template proxies only /api/ to
    this backend and the Azure backend app has internal ingress, so this is not
    reachable from the public frontend hostname -- it is a cluster-side concern,
    not a user-facing endpoint. Outside the rate limiter by the same construction
    as /api/health (the limiter matches /api/chat only), so scraping can never
    consume a client's chat budget.

    Like /api/health, it touches no external dependency: these are process-local
    counters, so a scrape succeeds while Mongo, Ollama and the MCP server are all
    down -- which is exactly when you most want the numbers.
    """
    payload, content_type = observability.render_metrics()
    return Response(content=payload, media_type=content_type)


@app.post("/api/chat", response_model=ChatResponse)
async def chat(request: Request) -> ChatResponse | JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content=ChatError(error="Invalid request: body must be valid JSON.").model_dump(),
        )

    try:
        chat_request = ChatRequest.model_validate(body)
    except ValidationError:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content=ChatError(
            error=f"Invalid request: 'message' must be a non-empty string of at most {MAX_MESSAGE_LENGTH} characters."
        ).model_dump(),
        )

    # Fetched before agent.run() and before save_turn() below persists this
    # turn, so the current message is never duplicated into its own history.
    history: list[dict[str, str]] = []
    try:
        history = await db.get_recent_history()
    except Exception:
        logger.exception(
            "Failed to load conversation history from MongoDB; continuing with empty history for this turn"
        )

    try:
        reply = await agent.run(chat_request.message, history=history)
    except agent.AgentError as exc:
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content=ChatError(error=str(exc)).model_dump(),
        )

    try:
        await db.save_turn(chat_request.message, reply)
    except Exception:
        logger.exception("Failed to auto-save conversation turn to MongoDB")

    return ChatResponse(response=reply)


@app.get("/api/transactions", response_model=TransactionsResponse)
async def get_transactions() -> TransactionsResponse | JSONResponse:
    """
    Read-only endpoint for the frontend's transactions panel display only —
    separate from the agent's tool-calling path (list_accounts/
    search_transactions/get_spending_summary in agent.py), which is the only
    way the model itself reads this data. Not rate-limited like /api/chat
    (cheap read, no LLM cost) and not covered by the "no REST CRUD" stance
    that applies specifically to conversation history — see CLAUDE.md.
    """
    try:
        account_docs = await db.list_accounts()
        # 500 is a display cap, not pagination (none exists) -- comfortably above
        # the seed script's ~107 rows so nothing is silently dropped today, but
        # this endpoint returns everything in one shot, not "all" unconditionally.
        # Revisit if the seed volume grows past this.
        transaction_docs = await db.search_transactions(limit=500)
    except Exception:
        logger.exception("Failed to load transactions from MongoDB")
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=ChatError(
                error="Unable to load transaction data right now. Please try again shortly."
            ).model_dump(),
        )

    return TransactionsResponse(accounts=account_docs, transactions=transaction_docs)


@app.post("/api/transactions/import", response_model=ImportResult)
async def import_transactions(
    file: UploadFile = File(...),
    account_name: str = Form(...),
    account_type: str = Form(...),
    opening_balance: float = Form(0.0),
) -> ImportResult | JSONResponse:
    """
    Replace all account/transaction data with a single account built from an
    uploaded bank-statement CSV plus the name/type/opening balance the
    frontend's import dialog collects -- see specs.md Phase 5 for the
    expected CSV shape and the fail-fast-validation/full-replace semantics.
    Like GET /api/transactions, this is display-tier plumbing for the
    frontend only -- the agent never calls this. Not rate-limited, same
    reasoning as GET /api/transactions above.

    Disabled entirely when IMPORT_ENABLED is false, which is how a public
    deployment protects the full-replace semantics below.
    """
    # 404 rather than 403: a deployment with import off should look like it never
    # had the route, not like it has one worth attacking.
    if not IMPORT_ENABLED:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content=ChatError(error="Not Found").model_dump(),
        )

    raw = await file.read()
    try:
        csv_text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content=ChatError(error="Invalid file: must be UTF-8 encoded text.").model_dump(),
        )

    try:
        result = await db.import_transactions(csv_text, account_name, account_type, opening_balance)
    except ValueError as exc:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content=ChatError(error=str(exc)).model_dump(),
        )
    except Exception:
        logger.exception("Failed to import transactions into MongoDB")
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=ChatError(
                error="Unable to import transactions right now. Please try again shortly."
            ).model_dump(),
        )

    return ImportResult(**result)
