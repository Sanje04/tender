# Azure Deployment (Container Apps, free tier)

Puts Tender on a public HTTPS URL at zero cost, with the LLM still running on your
own machine. This is the third deployment topology in the repo, alongside
[DEPLOYMENT.md](DEPLOYMENT.md) (single-host `docker compose`) and
[KUBERNETES_DEPLOYMENT.md](KUBERNETES_DEPLOYMENT.md) (a k3s plan, unbuilt). Unlike
that one, everything here exists and runs.

The same three containers as compose — `backend`, `mcp`, `frontend` — become three
Azure Container Apps, plus a Tailscale sidecar that gives the backend a private
route to the Ollama instance on your home machine.

```
                    Internet (HTTPS, Container Apps managed certificate)
                                      │
        ┌─────────────────────────────▼──────────────────────────────┐
        │  Azure Container Apps environment (Consumption, free grant) │
        │                                                             │
        │   frontend (nginx)  ──/api/──▶  backend (FastAPI) ──▶ mcp   │
        │   external ingress             internal ingress   internal  │
        │                                     │                       │
        │                          ┌──────────┴──────────┐            │
        │                          │ tailscale sidecar    │           │
        │                          │ same replica,        │           │
        │                          │ HTTP proxy on :1055  │           │
        └──────────────────────────┴──────────┬───────────┴───────────┘
                                              │ tailnet (WireGuard)
                   ┌──────────────────────────┴────┐
                   │  Your machine: Ollama :11434   │
                   └────────────────────────────────┘
                              │
                   MongoDB Atlas M0 (free), reached by backend and mcp
```

## What it costs

Nothing, provided you stay inside these:

| Resource | Free allowance | How this deployment stays inside it |
|---|---|---|
| Container Apps (Consumption) | 180,000 vCPU-s + 360,000 GiB-s + 2M requests / month | All three apps run `minReplicas=0`, so an idle deployment consumes none of it |
| Log Analytics | billed per GB ingested | Not created at all: the environment uses `--logs-destination none` |
| Container registry | — | Public GHCR packages, not Azure Container Registry (whose cheapest tier is not free) |
| MongoDB | Atlas M0, free forever | One M0 cluster |
| Tailscale | free plan, 3 users / 100 devices | Two devices: your Ollama host and an ephemeral node per backend replica |
| TLS certificate | — | Container Apps' managed certificate on its own `*.azurecontainerapps.io` hostname, so no domain purchase |

The cost of `minReplicas=0` is a cold start on the first request after idle. Chat is
already a multi-second operation against a local LLM, so this is barely noticeable
in practice.

## Two shapes: three containers or one

Everything below deploys the **three-container split** — `frontend`, `backend`, `mcp`
as separate container apps, mirroring `docker-compose.yml`. There is also a
**single all-in-one image** built by the `Dockerfile` at the repository root, which
packs all three processes into one container. Pick one:

| | Three containers | All-in-one image |
|---|---|---|
| Deploy unit | 3 container apps + scripts | 1 container app |
| Setup | `infra/deploy.ps1` | ~5 `az` commands, [below](#deploying-the-all-in-one-image) |
| Internal wiring | Internal ingress + FQDNs | loopback, nothing to configure |
| Scaling | Each part independently | All three together |
| Demonstrates | The MCP process boundary as a deployed service | Less of it — same protocol, shared lifecycle |

The split is the more interesting artifact, since the whole point of Phase 8 was
making the agent's tools a separate service. The all-in-one image is markedly
simpler to operate and is the better choice if you want one thing running and
addressable today. Both keep the MCP boundary real — the backend still speaks MCP
over HTTP to a separate process, never to an in-process function table.

### Deploying the all-in-one image

With a student subscription, `az acr build` is the shortest path: it builds in
Azure, so nothing needs pushing from your machine and the GHCR public-package step
is unnecessary.

```powershell
az group create -n tender-rg -l canadacentral
az acr create -n <globally-unique-name> -g tender-rg --sku Basic --admin-enabled true

# Builds from the repo root. Must be run from the repository root: the Dockerfile
# needs both ui/ and backend/ in its context.
az acr build -r <acr-name> -t tender:v1 .

az containerapp env create -n tender-env -g tender-rg -l canadacentral --logs-destination none

az containerapp create -n tender -g tender-rg --environment tender-env `
  --image <acr-name>.azurecr.io/tender:v1 `
  --registry-server <acr-name>.azurecr.io `
  --ingress external --target-port 80 `
  --cpu 0.5 --memory 1.0Gi `
  --min-replicas 1 --max-replicas 1 `
  --secrets mongodb-uri="<atlas-connection-string>" `
  --env-vars MONGODB_URI=secretref:mongodb-uri `
             OLLAMA_BASE_URL="http://<your-ollama-host>.<tailnet>.ts.net:11434" `
             OLLAMA_MODEL="gemma4:latest" `
             IMPORT_ENABLED="false"
```

`IMPORT_ENABLED="false"` has to be passed explicitly here. The application defaults
it to `true` so that local dev and `docker compose` keep working, and unlike the
split deployment there is no `backend-app.yaml.template` setting it for you. Seed
the data from your machine as in [step 5](#step-5--get-your-data-in-from-your-machine).

`--min-replicas 1` rather than 0, which a student subscription makes affordable and
which buys two things: no cold start on the first visit, and no first-message-
without-tools window, because the MCP process is already warm. `--max-replicas 1`
stays, for the same reason as the split deployment: `main.py`'s rate limiter is a
module-level dict and therefore per-replica.

The image sets its own internal topology as defaults (`MCP_HOST=127.0.0.1`,
`MCP_SERVER_URL=http://127.0.0.1:9000/mcp`, `FORCE_HTTPS=false`,
`MONGODB_SERVER_SELECTION_TIMEOUT_MS=5000`), so the only things a deployment must
supply are the Mongo connection string and the Ollama address. Set
`ALLOWED_ORIGINS` to the app's public URL once `az containerapp show` reports it.

**This image does not solve Ollama reachability.** A container in Azure still has
no route to a machine at home, so you need either the Tailscale sidecar — added to
this same app as a second container, exactly as
`infra/backend-app.yaml.template` does it, with `HTTP_PROXY=http://localhost:1055` —
or another way to reach the model. Bundling the app's own three processes together
changes nothing about that.

To redeploy after a change: `az acr build -r <acr-name> -t tender:v2 .` then
`az containerapp update -n tender -g tender-rg --image <acr-name>.azurecr.io/tender:v2`.
Use a new tag each time rather than overwriting one; Container Apps only creates a
revision when the image reference changes.

## Before you start

You need free accounts on [Tailscale](https://tailscale.com),
[MongoDB Atlas](https://www.mongodb.com/atlas), and
[Azure](https://azure.microsoft.com/free/), plus the
[Azure CLI](https://aka.ms/installazurecli) and Docker locally. The Azure free
account asks for a card for identity verification; the Consumption-plan grant used
here is a permanent monthly allowance, not trial credit, so it does not lapse after
30 days.

---

## Step 1 — Tailscale: a private route to your Ollama host

The problem this solves: Ollama runs on your machine, which has no public address,
and exposing it to the internet would let anyone use your GPU. A tailnet gives the
Azure-side container a private path to it instead.

The mechanism is worth understanding because it needed no application code.
`agent.py` calls Ollama through a bare `httpx.AsyncClient()`, and httpx honours
proxy environment variables by default. So a `tailscaled` sidecar exposing an
outbound HTTP proxy, plus `HTTP_PROXY` on the backend container, is enough —
`agent.py` never learns a tunnel exists. `tailscaled` also resolves the MagicDNS
name itself, so the backend container never has to resolve a `.ts.net` address.

1. **Install Tailscale on the Ollama machine** and sign in. Note the machine name
   it appears under in the admin console.
2. **Enable MagicDNS** (admin console → DNS). This gives you a stable
   `<machine>.<tailnet>.ts.net` name instead of an IP that can change.
3. **Confirm Ollama is reachable on the tailnet, not just on loopback.** By default
   Ollama binds `127.0.0.1`, which no tunnel can reach. Set
   `OLLAMA_HOST=0.0.0.0` for the Ollama service and restart it.
4. **Add a tag owner.** In the admin console's ACL editor, add:
   ```jsonc
   "tagOwners": { "tag:tender-backend": ["autogroup:admin"] }
   ```
   Registration fails with a permissions error without this.
5. **Create an OAuth client** (Settings → OAuth clients) with the `auth_keys` write
   scope and the `tag:tender-backend` tag. Keep the client ID and secret.

   An OAuth client rather than a plain auth key for two reasons: auth keys expire
   within 90 days, which would take the deployment down silently months later; and
   nodes registered with an OAuth secret are ephemeral by default, so the routine
   scale-to-zero restarts do not accumulate dead nodes in your tailnet.

**Prove it locally before trusting it in the cloud.** `docker-compose.yml` carries
the same sidecar under an opt-in profile for exactly this:

```powershell
# in backend/.env.production, uncomment and fill in:
#   OLLAMA_BASE_URL=http://<your-ollama-host>.<your-tailnet>.ts.net:11434
#   HTTP_PROXY=http://tailscale:1055
#   NO_PROXY=mcp,localhost,127.0.0.1,.azurecontainerapps.io
#   TS_CLIENT_ID=...
#   TS_CLIENT_SECRET=...
docker compose --profile tailnet up -d --build
docker compose logs tailscale        # should report a successful login
Invoke-WebRequest http://localhost/api/chat -Method POST `
  -ContentType 'application/json' -Body '{"message":"hello"}'
```

A real reply means Ollama was reached over the tailnet rather than over the LAN.

`NO_PROXY` is not optional. Without it, the same `HTTP_PROXY` swallows the
backend's MCP calls — also plain HTTP — and sends them down the tunnel looking for
a tool server that is not there. Tool discovery fails soft (chat answers, minus
tools, see `backend/specs.md` Phase 8), so this misconfigures silently rather than
loudly. MongoDB is unaffected either way: Motor does not read proxy variables.

One difference between the two topologies, and it is the only one: under compose the
sidecar is a separate container reached by service name (`http://tailscale:1055`,
bound `0.0.0.0`), while on Container Apps it shares the replica's network namespace
and is reached on `http://localhost:1055` (bound `127.0.0.1`).

## Step 2 — MongoDB Atlas M0

1. Create a free **M0** cluster. Choose **Azure** as the provider and the same
   region you will use in step 4 — every chat turn makes several round trips to
   Mongo, so a mismatched pair is felt directly in response time.
2. Create a database user, and under Network Access allow the addresses the
   deployment will connect from.
3. Take the `mongodb+srv://...` connection string.

Atlas rather than Cosmos DB's Mongo API, despite Cosmos being the Azure-native
option: `db.ensure_indexes()` creates a text index on `messages.content`, which the
`search_history` tool depends on, and Cosmos' text-index support is partial. Atlas
M0 hosted in an Azure region is free too, and is known to support it.

Verify locally before deploying, by pointing `backend/.env` at the Atlas string and
asking the assistant something like "what did we talk about earlier?" — that
exercises the text index through the real tool path.

## Step 3 — Publish the images to GHCR

Push to `master`. `.github/workflows/ci.yml` runs both test suites, then builds and
pushes three images tagged with the commit SHA and `latest`:

```
ghcr.io/<you>/tender-backend
ghcr.io/<you>/tender-mcp
ghcr.io/<you>/tender-frontend
```

**Make all three packages public** (GitHub → your profile → Packages → each
package → Package settings → Change visibility). Container Apps then pulls with no
registry credential to store or rotate. Private packages would work but need a pull
secret on every app.

## Step 4 — Deploy

```powershell
cp infra/deploy.env.example infra/deploy.env
# fill in: resource group, region, GHCR owner, Atlas URI, Ollama tailnet URL,
#          Tailscale OAuth client id/secret
az login
.\infra\deploy.ps1
```

`infra/deploy.sh` is the bash equivalent. Both are idempotent — every step checks
for an existing resource first — so re-running to pick up a new image tag is
normal. `infra/deploy.env` holds real credentials and is gitignored.

The script creates a resource group, a Consumption-plan environment, and then the
three apps in dependency order, because each needs the previous one's hostname:

1. **`mcp`** — internal ingress, target port 9000, `MCP_HOST=0.0.0.0` (a loopback
   bind would refuse the backend's calls), Mongo URI as a secret.
2. **`backend`** — internal ingress, target port 8000, defined from
   `infra/backend-app.yaml.template` rather than CLI flags because it is the one app
   with two containers, which the flag interface cannot express. Carries the
   Tailscale sidecar, `HTTP_PROXY`/`NO_PROXY`, `FORCE_HTTPS=false`, and a liveness
   probe on `/api/health`.
3. **`frontend`** — external ingress on 80, with `BACKEND_ORIGIN` set to the
   backend's internal FQDN. This is the only publicly reachable app.

Then it narrows the backend's `ALLOWED_ORIGINS` from `*` to the frontend's public
URL. That has to happen last: CORS cannot name a hostname that does not exist yet.

It prints the public URL when it finishes. That is the resume link.

A few decisions embedded in the scripts, so they are not mysteries later:

- **`--logs-destination none`.** No Log Analytics workspace, so no ingestion bill.
  `az containerapp logs show -n backend -g <rg> --follow` still streams live logs;
  only historical queries are unavailable.
- **`maxReplicas=1` on the backend.** `main.py`'s rate limiter is a module-level
  dict, so it is per-replica state. A second replica would silently double the
  effective limit rather than sharing it.
- **Internal ingress listens on port 80**, not the target port. `MCP_SERVER_URL` is
  therefore `http://mcp.internal.<env-domain>/mcp` with no `:9000`.
- **`FORCE_HTTPS=false`.** Ingress terminates TLS and forwards plain HTTP inside the
  environment; redirecting would loop the platform's own traffic. Same reasoning as
  the compose deployment behind a tunnel.
- **`allowInsecure: true` on the internal apps** means plain HTTP *within* the
  environment only. Public traffic is HTTPS-only, terminated at the frontend's
  ingress.

## Step 5 — Get your data in, from your machine

A fresh deployment has no accounts or transactions: the backend image deliberately
ships neither `scripts/seed_transactions.py` nor `backend/data/*.csv` (see
`backend/specs.md` Phase 5).

**Seed Atlas from your machine before deploying, rather than importing through the
running app.** The deployment ships with `IMPORT_ENABLED=false`
(`infra/backend-app.yaml.template`), which makes `POST /api/transactions/import`
return 404 — see [Known tradeoffs](#known-tradeoffs) for why that endpoint cannot
be left open on a public hostname.

```powershell
# In backend/.env, temporarily point MONGODB_URI at the Atlas connection string.
cd backend
.\.venv\Scripts\python.exe scripts\seed_transactions.py
# Then point MONGODB_URI back at your local MongoDB.
```

The script is idempotent, so re-running it is the normal way to refresh. Because
`data/transactions.csv` stores `days_ago` rather than absolute dates, each run
re-anchors the data to "the last ~6 months up to today" — a deployment seeded once
in March does not read as stale in August; re-run the script and it is current
again.

This is also the better demo: whoever opens the link lands on a populated dashboard
instead of an empty upload screen.

If you specifically want import available on the deployment — to load a real bank
export into it — set `IMPORT_ENABLED=true` on the backend app and treat the URL as
private for as long as it stays on, since anyone holding the link can replace all
of the data while it is.

## Step 6 — Make merges deploy themselves

The `deploy` job in `ci.yml` rolls all three apps onto each new commit's images. It
authenticates with GitHub Actions OIDC, so no long-lived Azure credential is stored
in the repo. To enable it:

1. Register an Entra ID app registration and grant its service principal
   **Contributor** on the resource group.
2. Add a **federated credential** on that app registration for this repository and
   the `master` branch.
3. Add repository **secrets** `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`,
   `AZURE_SUBSCRIPTION_ID`, and a repository **variable** `AZURE_RESOURCE_GROUP`.

The job's `if` tests the variable, not a secret, because secrets are not readable in
an `if` expression — so until you set it the job is skipped rather than failing every
push. It deploys the SHA tag, never `latest`: Container Apps creates a new revision
only when the image reference changes, so pushing `latest` would be a no-op that
looks like a successful deploy.

---

## Operating it

**Cold starts and the first message.** With `mcp` scaled to zero, the first chat
request after an idle period may find tool discovery still cold — `mcp_client`'s
discovery budget is 10s. That request answers without tools; the next one picks
them up, because discovery retries per request. This is the documented fail-soft
path, not a fault. If it bothers you, `minReplicas=1` on `mcp` fixes it and costs
roughly 650,000 vCPU-seconds a month against a 180,000 grant — i.e. it stops being
free.

**"The agent stopped using its tools."** Almost always `mcp` being unreachable
rather than a prompt regression, exactly as in local dev. Check
`az containerapp logs show -n mcp -g <rg>`.

**Chat returns 502.** Reserved for Ollama specifically (see CLAUDE.md's two-error-
paths constraint). Check your machine is awake, Ollama is running, it is bound
beyond loopback, and the tailnet node is online.

**Chat answers but nothing persists.** Atlas connectivity. Auto-save and history
fetch both fail soft by design, so this degrades quietly.

**The deploy script dies with `ConnectionResetError(10054)`.** That is the network
between you and `management.azure.com`, not the script or your subscription. Both
`deploy.ps1` and `deploy.sh` now retry a call that fails in transit — eight
attempts with backoff — and fail fast on anything Azure actually answered with, so
an occasional reset no longer ends the run. If every attempt resets, check whether
it is IPv6-specific before blaming Azure:

```powershell
curl.exe -4 -s -o NUL -w "%{http_code}`n" https://management.azure.com/subscriptions?api-version=2020-01-01
curl.exe -6 -s -o NUL -w "%{http_code}`n" https://management.azure.com/subscriptions?api-version=2020-01-01
```

`401` means reachable (unauthenticated, which is the expected answer); `000` means
the connection failed. A clean `-4` next to a failing `-6` is a broken IPv6 path on
your network — the Azure CLI does not fall back, because the socket connects and is
then reset mid-TLS rather than failing to connect. Prefer IPv4 system-wide
(`HKLM\SYSTEM\CurrentControlSet\Services\Tcpip6\Parameters\DisabledComponents` =
`0x20`, admin, needs a reboot), or -- lighter, and reversible without one --
demote IPv6 in the address-selection policy so `getaddrinfo` offers the CLI the
IPv4 address first:

```powershell
netsh interface ipv6 set prefixpolicy ::ffff:0:0/96 60 4   # elevated; effective at once
netsh interface ipv6 set prefixpolicy ::ffff:0:0/96 35 4   # restore the default
```

That leaves IPv6 enabled and only stops it being *preferred*. Deploying from a
different network also works, if that network's path is healthy.

**`/api/health` returns an Azure "Container App is stopped or does not exist"
page while the `backend` app looks fine.** Check the *revision*, not the app:

```powershell
az containerapp replica list -n backend -g tender-rg `
    --query "[].properties.containers[].{name:name,ready:ready,restarts:restartCount}" -o json
```

A crash-looping `tailscale` sidecar fails the whole revision even though the
`backend` container is ready, and Azure's edge then has no healthy replica to
route to -- so a dead sidecar presents as a missing backend. If the sidecar logs
(`--container tailscale`) show `error initializing kube client`, that is Container
Apps injecting `KUBERNETES_SERVICE_HOST`, which the image's containerboot reads as
"I am in Kubernetes" before dying on service-account files that are never mounted
here. `backend-app.yaml.template` blanks that variable, `KUBERNETES_SERVICE_PORT`
and `TS_KUBE_SECRET` to prevent it. The failure cannot reproduce under
`docker-compose`, which injects no such variable.

**The backend logs `SSL handshake failed ... TLSV1_ALERT_INTERNAL_ERROR` against
every Atlas shard.** Not a certificate or TLS-version problem, despite the wording.
Atlas' shared (M0) tier answers a connection from an address that is not on the
cluster's Network Access list by failing the TLS handshake with an internal-error
alert rather than refusing the connection, so it reads like a crypto fault. Confirm
by handshaking to one shard from a machine that *is* allowlisted -- if that succeeds
while the container fails, it is the access list:

```powershell
python -c "import socket,ssl;h='<shard>.mongodb.net';s=socket.create_connection((h,27017),timeout=20);print(ssl.create_default_context().wrap_socket(s,server_hostname=h).version())"
```

Then fix the access list -- but **not** with a single IP. A Container Apps
environment with no custom VNet has no stable outbound address: it egresses from a
large shared Azure SNAT pool, and the list is neither short nor fixed.

```powershell
az containerapp show -n backend -g tender-rg --query "length(properties.outboundIpAddresses)"
az containerapp show -n backend -g tender-rg --query properties.outboundIpAddresses -o tsv
```

That returned **160+ addresses** here. Note the environment's `staticIp` is the
*inbound* address and is **not** in that list -- allowlisting it does nothing, which
is an easy hour to lose. Three real options:

- **Allow `0.0.0.0/0`** in Atlas → Network Access. The database user password
  becomes the only access control, so use a strong generated one. Reasonable for a
  demo or portfolio deployment; Atlas will warn you.
- **Paste the whole outbound list** in. It works today and silently breaks when
  Azure changes the pool, so it trades one outage now for a mysterious one later.
- **Give the environment a VNet with a NAT gateway**, which yields one stable
  egress IP to allowlist. The correct answer for anything real, and the only one
  that needs the environment recreated (`--infrastructure-subnet-resource-id`).

Mongo failures fail soft by design, so the only visible symptoms are an empty
dashboard and chat that answers but never persists -- `/api/health` still returns 200.

## Known tradeoffs

**The live URL is dark whenever your machine or Ollama is off.** This is the
accepted cost of keeping inference free and self-hosted. If a recruiter clicking a
dead link is unacceptable, the fix already has a spec:
[`docs/specs.md`](docs/specs.md) Phase 9 makes the provider pluggable, and pointing
it at Azure AI Foundry would remove the home-machine dependency entirely.

**`POST /api/transactions/import` is disabled here, not secured.** Import is a full
replace (`backend/specs.md` Phase 5), so on a public URL an open one is a data-wipe
button for anyone holding the link. `IMPORT_ENABLED=false` in
`infra/backend-app.yaml.template` makes it 404 instead.

Disabled rather than authenticated because the obvious fix does not work: the caller
is the browser (`ui/src/services/transactions.ts`), so a shared secret checked in
`main.py` would have to reach the frontend as a `VITE_` variable — which Vite inlines
into the JS bundle at build time, where anyone can read it out of devtools. It would
authenticate nobody. Gating it properly means a passphrase typed into the import
dialog, so the secret is never in the bundle; that is UI work a demo does not need,
because step 5 seeds the data instead.

**The rate limiter is per-replica and in-memory.** Fine at `maxReplicas=1`, and it
resets on every cold start. It is abuse mitigation, not a quota.

## Teardown

```powershell
az group delete --name <your-resource-group> --yes
```

Removes every Azure resource. The Atlas cluster and the Tailscale tag/OAuth client
are separate; delete those in their own consoles.
