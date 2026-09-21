# Deployment

> For a public HTTPS URL on Azure's free tier, with Ollama still on your own
> machine behind a Tailscale tunnel, see [AZURE_DEPLOYMENT.md](AZURE_DEPLOYMENT.md)
> instead. This document is the single-host `docker compose` topology.

This covers running Tender via `docker-compose.yml` on a self-hosted machine,
as opposed to the manual `.venv`/`npm run dev` setup in [README.md](README.md#getting-started).
It builds and runs three containers — the FastAPI backend, the MCP tool server
that hosts the agent's six tools (`backend/mcp_server.py`, see
`backend/specs.md` Phase 8), and the built React frontend served by nginx
(which also reverse-proxies `/api/` to the backend, see
`ui/nginx.conf.template`, rendered at container start from `BACKEND_ORIGIN`) — and
expects MongoDB and Ollama to keep running outside Docker, the same
external-service roles they already have in local dev (see `CLAUDE.md`).

## 1. MongoDB: use Atlas, not a container

This compose file does not run MongoDB in a container. Create a free-tier
[MongoDB Atlas](https://www.mongodb.com/atlas) cluster and get its connection
string (`mongodb+srv://<user>:<password>@<cluster>.mongodb.net/...`).

If you'd rather test locally against the MongoDB already running on this
machine instead of setting up Atlas first, you can point `MONGODB_URI` at
`mongodb://host.docker.internal:27017` instead — but note `host.docker.internal`
only reaches services bound beyond `127.0.0.1`; if your local `mongod` only
binds to loopback, this will fail to connect from inside the container even
though it works fine from the host itself.

If that local `mongod` runs as a replica set (`replication.replSetName` set in
`mongod.cfg`), append `/?directConnection=true`:
`mongodb://host.docker.internal:27017/?directConnection=true`. Otherwise the
driver reads the replica set's advertised member list, discards the seed host
and dials whatever that list contains (typically `127.0.0.1:27017`) — which
inside a container is the container itself, so the connection is refused even
though the seed address was reachable. Do not add this option to the Atlas
`mongodb+srv://` string; it is incompatible with SRV/multi-host seedlists.

## 2. Configure the backend

```
cd backend
cp .env.production.example .env.production
```

Fill in `backend/.env.production`:
- `MONGODB_URI` — the Atlas connection string from step 1 (or the local
  fallback above).
- `OLLAMA_BASE_URL` / `OLLAMA_MODEL` — same LAN Ollama instance local dev
  already uses. `host.docker.internal` works out of the box under Docker
  Desktop; on Linux it requires the `extra_hosts: host.docker.internal:host-gateway`
  entry `docker-compose.yml` already has.
- `FORCE_HTTPS` — leave `false`. TLS is expected to terminate in front of
  this deployment (e.g. a Cloudflare Tunnel) and forward plain HTTP to the
  frontend container's port 80; setting this to `true` would make the
  backend redirect its own internal HTTP traffic and break that.
- `MAX_HISTORY_TURNS` — optional, defaults to 5 if left unset. Number of
  recent conversation turns replayed to the model as context on each chat
  call (see `backend/specs.md` Phase 7).
- `MCP_SERVER_URL` / `MCP_HOST` / `MCP_PORT` — the MCP tool server (Phase 8).
  `MCP_SERVER_URL=http://mcp:9000/mcp` addresses the `mcp` container by its
  compose service name, and is also the built-in default, so it can be left
  out entirely. `MCP_HOST` **must** be `0.0.0.0` here (not `127.0.0.1`) so the
  backend container can reach the mcp container. Compose gives this same
  `env_file` to both services, which is why all three keys live in one file.

This file is gitignored — never commit it with real credentials.

## 3. Build and run

```
docker compose up -d --build
```

This builds three images:
- `backend/Dockerfile` — installs `requirements.txt`, runs `uvicorn` with
  `--proxy-headers` so the per-client-IP rate limiter in `main.py` sees real
  client IPs through nginx rather than nginx's own container IP.
- `backend/Dockerfile.mcp` — same `./backend` build context and the same
  `requirements.txt` (so `db.py` stays one shared file rather than being
  copied), but copies only `mcp_server.py db.py` and runs the tool server.
- `ui/Dockerfile` — multi-stage: `npm run build`, then serves `dist/` from
  `nginx:alpine`.

The frontend is published on host port 80. Both the backend and the mcp
service are only reachable from within the compose network (nginx proxies to
the backend; the backend is the only thing that talks to mcp) — neither is
published directly.

`backend` declares `depends_on: [mcp]` but **not** a healthcheck condition, so
compose won't wait for the tool server to be ready. That's intentional: the
backend tolerates it, retrying tool discovery on each chat request until it
succeeds (see `backend/specs.md` Phase 8). If you see the agent answering but
never using its tools, check `docker compose logs mcp` — the backend will keep
serving regardless.

## 4. Get your data in

The backend image deliberately does not ship `scripts/seed_transactions.py`
or `backend/data/*.csv` — those are dev/demo-only (see `backend/specs.md`
Phase 5). A fresh deployment starts with no accounts or transactions. Load
real data through the running app itself: open the Transactions panel and
use **Import CSV** to upload a bank-statement export — this is the actual
reason the CSV import feature (Phase 5) exists, not just a local-dev
convenience.

**`.env.production.example` ships `IMPORT_ENABLED=false`, so you have to turn
import on to do that.** The endpoint takes no credential and replaces every
account and transaction in one request, which is fine on a machine only you
can reach and not fine on a hostname you have handed out. Once step 5 puts a
reverse proxy in front of this, decide which one you have:

- **Tunnel is private to you** — leave `IMPORT_ENABLED=true` and import
  whenever you have a new statement. This is the normal case for a
  single-operator self-hosted install.
- **The URL is shared with anyone else** — set it to `true`, import, set it
  back to `false`, and `docker compose up -d` to apply. Or skip import
  entirely and seed from your machine with
  `scripts/seed_transactions.py` pointed at the deployment's `MONGODB_URI`.

## 5. Put a reverse proxy in front

Point a TLS-terminating reverse proxy of your choice (Cloudflare Tunnel,
Caddy, nginx-with-certbot, etc.) at `http://localhost:80`. This repo doesn't
prescribe or script one — set up whichever you already use for other
self-hosted services on this machine.
