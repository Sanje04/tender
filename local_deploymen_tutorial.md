# Tender — Local CI/CD Tutorial
### Jenkins → Docker → Minikube, on one Windows 11 machine

**Who this is for.** You wrote the Tender application. You have not built a
CI/CD pipeline before. By the end of this tutorial you will have a local
Kubernetes cluster running your three containers, and a Jenkins job that — on
one click — tests your code, builds three images, loads them into the cluster,
rolls them out, smoke-tests them, and rolls *back* automatically if the smoke
test fails.

**How this document is structured.** Every section is tagged:

- **`[T]` Theory** — read it. Roughly 30% of the document. No commands.
- **`[L]` Lab** — do it. Roughly 70%. Every command is meant to be typed.
- **`[✓] Checkpoint`** — how you know the lab worked, before moving on.

Do not skip the checkpoints. A pipeline is a chain of six or seven things; when
it breaks at the end, the only cheap way to find the cause is to have verified
each link as you added it.

**Time budget.** Phase 1 ≈ 2 hours (mostly downloads). Phase 2 ≈ 3 hours.
Phase 3 ≈ 2 hours. It is fine to stop between phases — everything persists.

**Your confirmed setup** (from the interview):

| Decision | Your choice |
|---|---|
| Container runtime | Rancher Desktop on WSL2, dockerd/moby engine |
| Jenkins location | A container on the host Docker daemon, *outside* the cluster |
| Monitoring depth | `kubectl` + probes + the Minikube dashboard (no Prometheus) |
| MongoDB | Atlas M0 free tier (external to the cluster) |

**Related reading in this repo.** [docs/design.md](docs/design.md) is the
*design document* for this same topology — it states decisions and rationale,
but assumes you already know Kubernetes. This tutorial is the teaching version
of it. Where the two differ, this one is correct and says why.

---

## `[T]` 0.1 — What you are actually deploying

Before any tooling, be clear about the shape of the thing. Tender is **three
containers plus two external services**:

```
   Browser
      |  http://<minikube-ip>:30080
      v
  +-------------+        +--------------+        +----------------+
  |  frontend   |--/api/-->|   backend    |--MCP-->|  mcp           |
  |  nginx      |  proxy  |   FastAPI    |  HTTP  |  tool server   |
  |  :80        |        |   :8000      |        |  :9000         |
  +-------------+        +------+-------+        +-------+--------+
                                |                        |
                    +-----------+----------+             |
                    v                      v             v
              Ollama :11434          MongoDB Atlas   MongoDB Atlas
              (your host machine)      (internet)     (internet)
```

Four facts about this diagram carry consequences for every later phase:

1. **`mcp` is a separate process, not a library.** `backend/Dockerfile` and
   `backend/Dockerfile.mcp` build two different images from the *same*
   `./backend` directory. Three images, three Deployments. A tutorial that
   deploys "a frontend and a backend" is deploying two-thirds of your app.
2. **MongoDB and Ollama stay outside the cluster.** Ollama is multi-gigabyte
   and GPU-adjacent; MongoDB holds your real data. Putting either inside a
   cluster that a pipeline can tear down would make your deploys destructive.
   This matches all three existing deployment docs in the repo.
3. **`backend` tolerates `mcp` being down.** This is deliberate (see
   `CLAUDE.md`, "MCP discovery and invocation both fail soft"). If `mcp` is
   missing, chat still answers — just without tools. You will exploit this in
   Phase 3 to learn what a *silent* failure looks like. You must **never** add
   an `initContainer` or a readiness gate that makes `backend` wait for `mcp`:
   that would let a slow tool server block your entire API, and it contradicts
   a documented design constraint.
4. **`backend` must stay at `replicas: 1`.** Its rate limiter is a module-level
   Python dict — per-process, not shared. Running two replicas silently doubles
   the effective rate limit. When you want to demonstrate scaling in Phase 3,
   scale the **frontend**, which is stateless nginx.

---

## `[T]` 0.2 — The vocabulary, in one page

You will meet these words constantly. Learn them now, so the labs read as
instructions rather than as noise.

| Term | What it actually is |
|---|---|
| **Image** | A frozen filesystem plus a default command. Built by `docker build`. Immutable. |
| **Container** | A running instance of an image. Disposable. |
| **Pod** | Kubernetes' smallest unit: one or more containers sharing a network address. For you, one container each. |
| **Deployment** | A controller that says "keep N pods of this image running". Handles rolling updates and rollbacks. |
| **Service** | A stable in-cluster DNS name and virtual IP in front of a Deployment's pods. `http://backend:8000` works from an ordinary client (curl, or the app's own HTTP calls) because the libc resolver expands the cluster's search domains to `backend.tender.svc.cluster.local`. Config that resolves names *itself* gets no such help — nginx's `resolver` skips the search list, so the frontend's proxy must spell out the fully-qualified `backend.tender.svc.cluster.local`. |
| **NodePort** | A Service type that also opens a fixed high port (30000–32767) on the cluster node, so you can reach it from outside. Your way into the app. |
| **ConfigMap** | Non-secret key/value config, injected as environment variables. |
| **Secret** | The same, for credentials. Base64-encoded, not encrypted — it is *access control*, not cryptography. |
| **Namespace** | A folder for Kubernetes objects. Yours will be `tender`. |
| **Probe** | A periodic check Kubernetes runs against your container. *Readiness* = "may it receive traffic?". *Liveness* = "should it be restarted?". |
| **Minikube** | A single-node Kubernetes cluster that itself runs as a container on your Docker daemon. |
| **Jenkins** | A server that runs your build steps on a trigger. The steps live in a `Jenkinsfile` in your repo. |
| **Pipeline / stage** | The `Jenkinsfile`'s script, divided into named phases that appear as columns in the Jenkins UI. |

---

## `[T]` 0.3 — What "CI/CD" means here, concretely

**CI (Continuous Integration)** answers *"did this commit break anything?"* —
for you: `pytest -m "not live_llm"`, `tsc --noEmit`, and `vitest`. It produces a
pass/fail and nothing else.

**CD (Continuous Deployment)** answers *"is this commit now running?"* — build
three images, get them onto the cluster node, tell each Deployment to use the
new tag, wait for the rollout, prove the result responds, undo it if it doesn't.

The valuable idea is not automation for its own sake. It is that **the pipeline
becomes the only path to the cluster**. Once it exists, "what is deployed?" has
one answer — the last green build — instead of being whatever someone last typed
by hand at 11pm. Every design decision below serves that.

One rule follows from it that beginners get wrong most often:

> **Tag every image with the build number. Never use `:latest`.**

Kubernetes decides whether to restart pods by comparing the image *reference*.
If the reference is `tender-backend:latest` before and after, Kubernetes sees no
change and keeps running the old code — while your pipeline reports success.
`tender-backend:42` → `tender-backend:43` is an unambiguous change, and it also
means `kubectl get deploy -o jsonpath='{..image}'` tells you exactly which build
is live.

---

# PHASE 1 — Prepare the environment

**Goal:** a working Kubernetes cluster that can reach Ollama and Atlas, with all
tooling installed. No application yet.

## `[T]` 1.1 — Why this particular stack

**Why WSL2 and not PowerShell?** Jenkins talks to the Docker daemon through its
Unix socket, `/var/run/docker.sock`. That socket lives inside the WSL2 distro.
Mixing PowerShell paths (`C:\Users\...`) and Linux paths (`/home/you/...`) in one
setup is the single most common reason this arrangement fails — usually with a
TLS error that mentions nothing about paths. **Every command in this tutorial
runs in a WSL2 Ubuntu shell unless the step says otherwise.**

**Why Rancher Desktop?** It is Apache-2.0 licensed and gives you the same
`dockerd` that Docker Desktop does. Docker Desktop works identically but is not
open source and its licence restricts commercial use at larger organisations —
which conflicts with the "open source tools" objective you started from.

**Why Minikube with `--driver=docker`?** The cluster node becomes just another
container on the Docker daemon you already have. No Hyper-V, no admin rights, no
nested-virtualisation fight with WSL2. And critically: `minikube image load` can
hand a locally built image straight to the node, so you need no container
registry at all.

**Why is Jenkins outside the cluster?** Jenkins builds and deploys the images.
If Jenkins ran *inside* the cluster it redeploys, a bad deploy could restart
Jenkins mid-pipeline — and you would lose the logs that tell you why. Keeping CI
outside the thing it changes is a general principle worth internalising.

## `[L]` 1.2 — Enable WSL2

This is the only step that runs in **Windows PowerShell, as Administrator**:

```powershell
wsl --install -d Ubuntu
wsl --set-default-version 2
```

Reboot. Open the "Ubuntu" app from the Start menu and let it finish first-run
setup (it asks for a username and password — these are Linux-local, unrelated to
your Windows account).

Back in PowerShell:

```powershell
wsl -l -v
```

**`[✓] Checkpoint`** — `Ubuntu` is listed with `VERSION` = `2`. If it says `1`,
run `wsl --set-version Ubuntu 2` and wait; a version-1 distro has no working
Docker socket.

## `[L]` 1.3 — Install Rancher Desktop

Download from <https://rancherdesktop.io/> and install on Windows. On first
launch, in its settings:

- **Container Engine:** `dockerd (moby)` — **not** containerd. The `docker` CLI
  and Minikube's docker driver both require it.
- **Kubernetes:** **disable it.** Rancher Desktop bundles its own k3s cluster.
  Leaving it on gives you two clusters fighting over `~/.kube/config`, and you
  will spend an hour deploying to the wrong one.
- **WSL Integration:** enable it for your `Ubuntu` distro.

Now open **Ubuntu** — all remaining commands are here:

```bash
docker version
docker run --rm hello-world
ls -l /var/run/docker.sock
```

**`[✓] Checkpoint`** — `docker version` prints **both** a `Client:` and a
`Server:` block, `hello-world` prints its greeting, and the socket file exists.

> If you see a client but no server, WSL integration is not enabled for this
> distro. Fix it now. Nothing after this point can work without it.

## `[L]` 1.4 — Install kubectl and Minikube

```bash
# kubectl -- the Kubernetes CLI
curl -LO "https://dl.k8s.io/release/$(curl -Ls https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
sudo install -m 0755 kubectl /usr/local/bin/kubectl && rm kubectl

# minikube -- the single-node cluster
curl -LO https://storage.googleapis.com/minikube/releases/latest/minikube-linux-amd64
sudo install -m 0755 minikube-linux-amd64 /usr/local/bin/minikube && rm minikube-linux-amd64

kubectl version --client
minikube version
```

**`[✓] Checkpoint`** — both print a version number.

## `[L]` 1.5 — Start the cluster

```bash
minikube start --driver=docker --cpus=4 --memory=6g
minikube status
kubectl get nodes
kubectl create namespace tender
```

**Sizing note.** Those 4 CPUs and 6 GB are for the cluster *alone*. Ollama runs
on the host and needs several GB on top. On a 16 GB machine use
`--cpus=2 --memory=4g` instead.

**`[✓] Checkpoint`** — `kubectl get nodes` shows one node with `STATUS Ready`,
and `kubectl get ns` lists `tender`.

> **Worth knowing now:** after a Windows reboot, Minikube does *not* auto-start.
> `minikube start` brings the same cluster back with its data intact.

## `[T]` 1.6 — Why you test connectivity before writing any manifest

Your app depends on two services that are not in the cluster. If either is
unreachable *from inside* the cluster, the symptom shows up as an application
bug: chat returns 502, or the dashboard is empty. You then debug Python for an
hour and discover it was a Windows firewall rule.

Test the network first, with a throwaway pod, while there is no application to
blame. This habit — isolate the layer before you debug the code — is most of
what separates a fast operator from a slow one.

## `[L]` 1.7 — Prove the cluster can reach Ollama

Ollama must be running on your Windows host with a model pulled. In
**PowerShell**, `ollama list` should show a model.

Then from Ubuntu:

```bash
kubectl run nettest --rm -it --restart=Never --image=curlimages/curl -n tender -- \
  curl -s -m 5 http://host.minikube.internal:11434/api/tags
```

**`[✓] Checkpoint`** — a JSON blob listing your models.

If it hangs or is refused, Windows Defender is blocking inbound 11434. Either add
an inbound rule for that port, or — if Ollama runs on another machine on your LAN
— substitute its IP:

```bash
kubectl run nettest --rm -it --restart=Never --image=curlimages/curl -n tender -- \
  curl -s -m 5 http://10.0.0.68:11434/api/tags
```

Whichever form works, **write it down**. It becomes `OLLAMA_BASE_URL` in the
ConfigMap in Phase 2. Note the model name too — it becomes `OLLAMA_MODEL`, and
it must match a model you have actually pulled.

## `[L]` 1.8 — Set up MongoDB Atlas M0, and prove the cluster can reach it

1. Create a free account at <https://www.mongodb.com/atlas> and create an **M0**
   (free forever) cluster.
2. **Database Access** → add a database user with a password. Use a password
   with no `@ : / ?` characters, or you will have to percent-encode it in the URI.
3. **Network Access** → **Add IP Address**. For a local tutorial, `0.0.0.0/0`
   ("allow from anywhere") is acceptable and saves you re-adding your IP every
   time your ISP rotates it. Understand the trade-off: the database is then
   protected by the password alone.
4. **Connect** → **Drivers** → copy the `mongodb+srv://...` string.

Test it from inside the cluster:

```bash
kubectl run mongotest --rm -it --restart=Never -n tender \
  --image=mongo:7 -- \
  mongosh "mongodb+srv://USER:PASS@cluster0.xxxxx.mongodb.net/?retryWrites=true&w=majority" \
  --quiet --eval 'db.adminCommand({ping:1})'
```

**`[✓] Checkpoint`** — `{ ok: 1 }`.

> A timeout or `connection refused` here is almost always the Atlas IP allowlist
> (step 3), not your URI.

## `[L]` 1.9 — Seed the demo data (optional but recommended)

So the app has something to show, seed the mock transactions once, from
**PowerShell**, against the same Atlas cluster your pods will use:

```powershell
cd backend
.\.venv\Scripts\python.exe scripts\seed_transactions.py
```

This is safe to re-run. It reads `backend/data/*.csv` and writes to MongoDB.

## `[L]` 1.10 — One Windows-specific trap, dealt with up front

This repo is developed with Git's `core.autocrlf=true`, which converts text
files to Windows line endings (`\r\n`) on checkout. A shell script with a `\r` at
the end of its shebang line, run inside a Linux container, fails with:

```
/bin/sh^M: no such file or directory
```

— an error naming the interpreter, saying nothing about line endings. The repo's
`.gitattributes` already pins `*.sh`, `*.envsh` and `Dockerfile*` to LF for
exactly this reason. **If you create a new script that a container will
execute, add it to `.gitattributes` too.** Check any file you are unsure about:

```bash
file backend/Dockerfile   # "ASCII text" is good; "with CRLF line terminators" is not
```

## Phase 1 — Exit criteria

| Check | Command | Expected |
|---|---|---|
| Docker works in WSL | `docker run --rm hello-world` | greeting |
| Cluster is up | `kubectl get nodes` | one `Ready` node |
| Namespace exists | `kubectl get ns tender` | `Active` |
| Ollama reachable | the `nettest` pod above | JSON model list |
| Atlas reachable | the `mongotest` pod above | `{ ok: 1 }` |

Do not start Phase 2 until all five pass.

---

# PHASE 2 — Pipeline preparation

**Goal:** Kubernetes manifests that you have deployed by hand, then a Jenkins
container that does the same thing automatically from a `Jenkinsfile`.

The order matters. You will deploy manually *first*. If you let Jenkins be the
first thing that ever applies your manifests, then the first failure could be in
the manifests, in Jenkins' permissions, in the kubeconfig mount, or in the
pipeline script — four suspects instead of one.

## `[T]` 2.1 — How an image gets from your laptop into the cluster

The cluster node has its own image store. An image you just built with `docker
build` is on the *host* daemon and the node cannot see it. Three ways to bridge
that gap:

1. **`minikube image load tender-backend:42`** — builds on the host daemon, then
   tars the image and copies it into the node. Works with any driver, needs no
   registry and no TLS. Costs a few seconds per image per build. **This is what
   you will use** — by hand in 2.5. The pipeline does the same copy itself in
   2.10, without the `minikube` CLI, for the reason given at the end of 2.6.
2. **`eval $(minikube docker-env)`** — builds *directly* on the node's internal
   daemon, so there is nothing to transfer. Faster, but it silently redirects
   every subsequent `docker` command in that shell to a different daemon, which
   makes pipeline failures confusing to diagnose.
3. **`minikube addons enable registry`** — a real in-cluster registry, closest to
   production. Adds a registry to run, port-forwards to maintain, and
   insecure-registry config to get right.

Switch to (2) or (3) only if `image load` becomes your slowest step.

> **The number-one reason locally-loaded images "don't work":**
> **`imagePullPolicy: IfNotPresent` is mandatory on every container.** With a
> `:latest`-style tag Kubernetes defaults to `Always`, tries to pull from Docker
> Hub, fails, and parks the pod in `ErrImagePull` — with the image sitting right
> there on the node.

## `[T]` 2.2 — ConfigMap vs Secret: what goes where

Split config by *sensitivity*, not by convenience:

| Value | Where | Why |
|---|---|---|
| `OLLAMA_BASE_URL`, `OLLAMA_MODEL` | ConfigMap | Not secret; changes per environment |
| `MONGODB_DB_NAME`, `MAX_HISTORY_TURNS`, `FORCE_HTTPS` | ConfigMap | Plain settings |
| `MCP_SERVER_URL`, `MCP_HOST`, `MCP_PORT` | ConfigMap | Internal topology |
| `ALLOWED_ORIGINS` | ConfigMap | A hostname, not a credential |
| `MONGODB_URI` | **Secret** | Contains a username and password |

The Secret is created **imperatively from the command line**, not written into a
YAML file, so a real credential never lands in the repo. That is the whole
reason for the asymmetry — everything else is a file you commit.

Two config details specific to this app:

- **`FORCE_HTTPS=false`.** nginx (or here, a NodePort) terminates the connection
  and forwards plain HTTP internally. Setting it true makes the backend redirect
  its own internal traffic and breaks the app.
- **`ALLOWED_ORIGINS`.** The default in code is `*`, which is what keeps local
  dev working, and the ConfigMap below keeps it that way deliberately. A real
  deployment narrows it to the frontend's public hostname — but on Minikube that
  hostname is the NodePort URL, which you cannot know until the cluster is
  running. `*` on a single-user local cluster is the right call; narrowing it is
  a production step, not a local one.

## `[L]` 2.3 — Write the manifests

Create a `k8s/` directory at the repo root:

```bash
cd /mnt/c/Users/sanje/OneDrive/Documents/work/ag-ai
mkdir -p k8s
```

> **A note on that path.** `/mnt/c/...` is WSL's view of your Windows drive. It
> works, but file I/O across that boundary is slow. If builds feel sluggish
> later, `git clone` the repo into the WSL filesystem (`~/tender`) and work there
> instead.

**`k8s/namespace.yaml`**

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: tender
```

**`k8s/configmap.yaml`** — edit `OLLAMA_BASE_URL` and `OLLAMA_MODEL` to the
values you confirmed in lab 1.7:

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: tender-config
  namespace: tender
data:
  OLLAMA_BASE_URL: "http://host.minikube.internal:11434"
  # Must name a model you have actually pulled -- the one you saw in lab 1.7.
  OLLAMA_MODEL: "gemma4:latest"
  MONGODB_DB_NAME: "ag_ai"
  MONGODB_SERVER_SELECTION_TIMEOUT_MS: "5000"
  MAX_HISTORY_TURNS: "5"
  FORCE_HTTPS: "false"
  ALLOWED_ORIGINS: "*"
  # The backend finds the tool server by Service name. Because the Service below
  # is named `mcp`, this value is identical to the docker-compose one.
  MCP_SERVER_URL: "http://mcp:9000/mcp"
  # 0.0.0.0 so the mcp container accepts connections from the backend pod.
  MCP_HOST: "0.0.0.0"
  MCP_PORT: "9000"
```

**`k8s/backend.yaml`** — Deployment and Service in one file, separated by `---`:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: backend
  namespace: tender
spec:
  # Load-bearing. main.py's rate limiter is a per-process dict, so more replicas
  # silently multiply the effective limit. See CLAUDE.md.
  replicas: 1
  selector:
    matchLabels: { app: backend }
  template:
    metadata:
      labels: { app: backend }
    spec:
      containers:
        - name: backend
          image: tender-backend:dev      # Jenkins overwrites this tag per build
          imagePullPolicy: IfNotPresent  # required for locally loaded images
          ports:
            - containerPort: 8000
          envFrom:
            - configMapRef: { name: tender-config }
            - secretRef:    { name: tender-secrets }
          readinessProbe:
            httpGet: { path: /api/health, port: 8000 }
            initialDelaySeconds: 5
            periodSeconds: 10
          livenessProbe:
            httpGet: { path: /api/health, port: 8000 }
            initialDelaySeconds: 15
            periodSeconds: 20
          resources:
            requests: { cpu: "100m", memory: "256Mi" }
            limits:   { memory: "512Mi" }
---
apiVersion: v1
kind: Service
metadata:
  name: backend
  namespace: tender
spec:
  selector: { app: backend }
  ports:
    - port: 8000
      targetPort: 8000
```

> **Why `/api/health` and not `/api/transactions`?** `/api/health` is
> deliberately dependency-free: it touches no MongoDB, no Ollama, no MCP server.
> That is exactly the property a probe needs. If the probe hit
> `/api/transactions`, a brief Atlas hiccup would fail the liveness check and
> Kubernetes would restart a backend that is working perfectly — turning a
> transient dependency blip into a self-inflicted outage.
> (`docs/design.md` shows `/api/transactions` here; it predates `/api/health`.
> Use `/api/health`.)

**`k8s/mcp.yaml`**

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: mcp
  namespace: tender
spec:
  replicas: 1
  selector:
    matchLabels: { app: mcp }
  template:
    metadata:
      labels: { app: mcp }
    spec:
      containers:
        - name: mcp
          image: tender-mcp:dev
          imagePullPolicy: IfNotPresent
          ports:
            - containerPort: 9000
          envFrom:
            - configMapRef: { name: tender-config }
            - secretRef:    { name: tender-secrets }
          resources:
            requests: { cpu: "50m", memory: "128Mi" }
            limits:   { memory: "384Mi" }
---
apiVersion: v1
kind: Service
metadata:
  name: mcp
  namespace: tender
spec:
  selector: { app: mcp }
  ports:
    - port: 9000
      targetPort: 9000
```

> **No probes on `mcp`, and no readiness gate from `backend` to `mcp`.** The
> backend's tool discovery fails soft and retries on the next request, by design.
> Adding an `initContainer` that waits for `mcp` would let a slow tool server
> block the whole API. This is a documented hard constraint — don't "improve" it.

**`k8s/frontend.yaml`**

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: frontend
  namespace: tender
spec:
  replicas: 1
  selector:
    matchLabels: { app: frontend }
  template:
    metadata:
      labels: { app: frontend }
    spec:
      containers:
        - name: frontend
          image: tender-frontend:dev
          imagePullPolicy: IfNotPresent
          ports:
            - containerPort: 80
          env:
            # ui/nginx.conf.template is rendered by envsubst at container start,
            # so the backend address is runtime config, not baked into the image.
            #
            # Must be the FULLY-QUALIFIED Service name here, overriding the
            # image's own default. The template proxies through a *variable*
            # upstream (so a scaled-to-zero backend doesn't stop nginx booting),
            # which requires an explicit `resolver` -- and nginx's resolver does
            # not apply /etc/resolv.conf's search domains. A bare `backend` is a
            # single-label name that CoreDNS answers with SERVFAIL, so every
            # /api/ request 502s and the UI shows "Can't reach the backend".
            # curl/wget in this same pod still work (the libc resolver does
            # expand the search list), which makes it look like an app bug
            # rather than a DNS one. The image default (http://backend:8000) is
            # correct for docker-compose's embedded DNS, not for Kubernetes.
            - name: BACKEND_ORIGIN
              value: "http://backend.tender.svc.cluster.local:8000"
          readinessProbe:
            httpGet: { path: /, port: 80 }
            initialDelaySeconds: 3
            periodSeconds: 10
          resources:
            requests: { cpu: "50m", memory: "64Mi" }
            limits:   { memory: "128Mi" }
---
apiVersion: v1
kind: Service
metadata:
  name: frontend
  namespace: tender
spec:
  type: NodePort
  selector: { app: frontend }
  ports:
    - port: 80
      targetPort: 80
      nodePort: 30080
```

## `[L]` 2.4 — Create the Secret

Not a file. Typed, once:

```bash
kubectl create secret generic tender-secrets -n tender \
  --from-literal=MONGODB_URI='mongodb+srv://USER:PASS@cluster0.xxxxx.mongodb.net/?retryWrites=true&w=majority'
```

Single quotes matter — the URI contains `?` and `&`, which your shell would
otherwise interpret.

**`[✓] Checkpoint`**

```bash
kubectl get secret tender-secrets -n tender
# And to prove the value round-tripped correctly:
kubectl get secret tender-secrets -n tender -o jsonpath='{.data.MONGODB_URI}' | base64 -d; echo
```

That second command also demonstrates the point from 2.2: a Secret is encoded,
not encrypted. Anyone with `get secret` rights can read it.

## `[L]` 2.5 — Build the three images and deploy by hand

```bash
cd /mnt/c/Users/sanje/OneDrive/Documents/work/ag-ai

docker build -t tender-backend:dev  ./backend
docker build -t tender-mcp:dev      -f ./backend/Dockerfile.mcp ./backend
docker build -t tender-frontend:dev ./ui

minikube image load tender-backend:dev
minikube image load tender-mcp:dev
minikube image load tender-frontend:dev

kubectl apply -f k8s/
kubectl get pods -n tender -w    # Ctrl-C when all three read 1/1 Running
```

> **`minikube image load` only works from the shell that ran `minikube start`**
> — it uses that shell's Minikube profile. From any other shell (or if you get
> `ssh: unable to authenticate`), this does the same copy with no profile at
> all, and is exactly what the pipeline uses in 2.10:
> `docker save tender-backend:dev | docker exec -i minikube ctr -n k8s.io images import -`

**`[✓] Checkpoint`**

```bash
kubectl get pods -n tender
```

Three pods, each `1/1 Running`. If any is not, jump to the troubleshooting table
in Phase 3 — do not continue to Jenkins with a broken deployment.

Now open the app:

```bash
minikube service frontend -n tender --url
```

Paste that URL into your Windows browser. The dashboard should load. Ask the
chat *"what's my balance?"* — a real answer with figures means the whole chain
works: frontend → backend → mcp → Atlas, plus backend → Ollama.

> **A generic answer with no figures is the fail-soft path in action:** the
> backend is fine but could not reach `mcp`. Check `kubectl logs -n tender
> deploy/backend | grep -i mcp`. This is the single most useful diagnostic in
> this app, and it is worth causing on purpose later.

## `[T]` 2.6 — Jenkins, and the trade-off you are accepting

Jenkins runs as a container. That container needs to run `docker build`. There
are two ways to give a container Docker:

- **DinD (Docker-in-Docker)** — run a second, nested Docker daemon inside the
  container. Requires `--privileged`, duplicates the image cache, is slower.
- **DooD (Docker-outside-of-Docker)** — mount the *host's* socket,
  `/var/run/docker.sock`, into the container. The `docker` CLI inside talks to
  the daemon outside. Shared cache, no nesting, much simpler.

You will use DooD. **State the trade-off plainly, because it is real:** mounting
that socket gives the Jenkins container root-equivalent control of your host's
Docker daemon. Any pipeline it runs can start a privileged container on your
machine. That is acceptable here because this Jenkins is local, single-user, not
network-exposed, and only ever builds this one repo. **Do not copy this topology
to a shared or internet-reachable Jenkins** — there, use Kubernetes agents with
Kaniko or Buildah, which build images with no Docker daemon at all.

Second point, easy to miss and expensive to debug: **the kubeconfig Minikube
wrote for you is useless inside a container.** Its `server:` is
`https://127.0.0.1:<port>`, and inside the Jenkins container `127.0.0.1` is
Jenkins itself, not your machine. It also points at its TLS certificates by
host path (`C:\Users\...` if you ran `minikube start` from PowerShell), which a
Linux container cannot open. Both fail with errors that say nothing about
networking or mounts.

So Jenkins gets **its own kubeconfig**: certificates embedded inline rather than
referenced by path, and `server:` set to `https://minikube:8443`. The Jenkins
container joins Minikube's Docker network so that name resolves (`minikube` is
one of the names on the API server's certificate, so TLS still verifies). For
the same reason Jenkins never runs the `minikube` CLI, which would need your
host's Minikube profile. It loads images by talking to the node container
directly, over the Docker socket it already has.

## `[L]` 2.7 — Build a Jenkins image with your toolchain baked in

Stock `jenkins/jenkins` has no `docker`, `kubectl`, `minikube`, `python3` or
`node`. Installing them from a pipeline step on every build is slow and fragile.
Bake them into the image once.

Create `jenkins/Dockerfile`:

```dockerfile
FROM jenkins/jenkins:lts-jdk17
USER root

RUN apt-get update && apt-get install -y --no-install-recommends \
      curl ca-certificates gnupg python3 python3-venv python3-pip \
 && install -m 0755 -d /etc/apt/keyrings \
 && curl -fsSL https://download.docker.com/linux/debian/gpg \
      | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
 && echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.gpg] \
      https://download.docker.com/linux/debian bookworm stable" \
      > /etc/apt/sources.list.d/docker.list \
 && apt-get update && apt-get install -y --no-install-recommends docker-ce-cli \
 && curl -L "https://dl.k8s.io/release/$(curl -Ls https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl" \
      -o /usr/local/bin/kubectl && chmod +x /usr/local/bin/kubectl \
 && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
 && apt-get install -y nodejs \
 && rm -rf /var/lib/apt/lists/*

# The `jenkins` user must be in the group that owns the host's docker socket, or
# every `docker` call in the pipeline fails with a permission error. If a group
# with that GID already exists (Docker Desktop's socket is root:root, GID 0),
# join it rather than failing on a duplicate GID.
ARG DOCKER_GID=988
RUN : "${DOCKER_GID:?DOCKER_GID build arg is empty}" \
 && if getent group "${DOCKER_GID}" >/dev/null; then \
      usermod -aG "$(getent group "${DOCKER_GID}" | cut -d: -f1)" jenkins; \
    else \
      groupadd -g "${DOCKER_GID}" hostdocker && usermod -aG hostdocker jenkins; \
    fi

USER jenkins
```

Build it, passing the socket's GID **as a container sees it** — that is what
the `jenkins` user inside the container is checked against, and it is not
always what the host sees. Docker Desktop in particular mounts its own VM's
socket, which is `root:root` (GID `0`) inside a container whatever `stat` says
on the host. Asking a throwaway container gives the right answer on every
engine:

```bash
DOCKER_GID=$(docker run --rm -v /var/run/docker.sock:/var/run/docker.sock alpine stat -c '%g' /var/run/docker.sock)
echo "docker socket GID (as a container sees it) is $DOCKER_GID"
docker build --build-arg DOCKER_GID=$DOCKER_GID -t tender-jenkins:lts jenkins/
```

## `[L]` 2.8 — Run Jenkins

Run this step in **the shell you ran `minikube start` from**. Its kubeconfig is
the one holding the live cluster's credentials; a second Minikube profile in
the other shell will be stale. Both variants do the same three things: write
Jenkins its own kubeconfig (2.6), create the `jenkins_home` volume, and start
the container on Minikube's network.

**WSL / bash:**

```bash
mkdir -p ~/.kube/jenkins
kubectl config view --minify --flatten --context minikube \
  | sed -E 's#https://127\.0\.0\.1:[0-9]+#https://minikube:8443#' > ~/.kube/jenkins/config

docker volume create jenkins_home

docker run -d --name jenkins --restart unless-stopped \
  --network minikube \
  -p 8080:8080 -p 50000:50000 \
  -v jenkins_home:/var/jenkins_home \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$HOME/.kube/jenkins:/kube:ro" \
  -e KUBECONFIG=/kube/config \
  tender-jenkins:lts
```

**PowerShell** (the line continuation is a backtick, not `\`):

```powershell
New-Item -ItemType Directory -Force "$HOME\.kube\jenkins" | Out-Null
(kubectl config view --minify --flatten --context minikube) -replace 'https://127\.0\.0\.1:\d+', 'https://minikube:8443' |
  Set-Content -Encoding ascii "$HOME\.kube\jenkins\config"

docker volume create jenkins_home

docker run -d --name jenkins --restart unless-stopped `
  --network minikube `
  -p 8080:8080 -p 50000:50000 `
  -v jenkins_home:/var/jenkins_home `
  -v /var/run/docker.sock:/var/run/docker.sock `
  -v "$HOME\.kube\jenkins:/kube:ro" `
  -e KUBECONFIG=/kube/config `
  tender-jenkins:lts
```

`jenkins_home` is a named volume so your jobs, credentials and build history
survive recreating the container.

Two further details, easy to get wrong and expensive to debug:

- **`--flatten` is what removes the host paths.** It inlines the certificates
  and key into the file itself. That also means **`~/.kube/jenkins/config`
  holds your cluster's client key.** It lives outside the repo on purpose.
  Never put it in `jenkins/`, which you are about to commit.
- **`--network minikube` ties Jenkins to the cluster's lifetime.** Docker only
  resolves container names like `minikube` on a user-defined network. If you
  ever `minikube delete`, the network goes with it and Jenkins will not start.
  Recreate the cluster, re-run the kubeconfig line, then recreate the container
  (`docker rm -f jenkins`, then the `run` above). A plain `minikube stop` /
  `minikube start` needs none of this.

Get the unlock password (works in either shell):

```bash
docker exec jenkins cat /var/jenkins_home/secrets/initialAdminPassword
```

Open <http://localhost:8080> in Windows, paste the password, choose **Install
suggested plugins**, and create your admin user.

**`[✓] Checkpoint` — verify the toolchain from inside the container now, not
after a pipeline fails** (one line, so it works unchanged in either shell):

```bash
docker exec jenkins sh -c 'docker ps >/dev/null && echo docker OK; kubectl get nodes >/dev/null && echo kubectl OK; docker exec minikube ctr -n k8s.io images ls -q >/dev/null && echo image-load OK; python3 --version; node --version'
```

All three `OK` lines must print.

> **Why `kubectl get nodes` and not `kubectl version --client`?** The client
> version prints without contacting the cluster at all, so it passes happily
> while the thing you actually need — reaching `minikube:8443` with valid
> credentials — is broken. Likewise the third check does not ask whether `ctr`
> is installed; it runs it inside the node, over the exact path the pipeline's
> image load uses. **Check the capability, not the binary.** That principle
> applies to every checkpoint you will ever write.

If `docker ps` says permission denied, your
`DOCKER_GID` was wrong — re-run the `alpine stat` probe from 2.7, rebuild the
image, and recreate the container (`docker rm -f jenkins`, then the `run` above;
`jenkins_home` persists, so you keep your setup). If `kubectl` fails with
`no such host` or `connection refused`, the container is not on the `minikube`
network or its kubeconfig still says `127.0.0.1` — redo this step.

## `[T]` 2.9 — Reading a Jenkinsfile

A declarative pipeline has a fixed skeleton:

```groovy
pipeline {
  agent any                  // where to run: "any available executor"
  environment { ... }        // variables available to every stage
  stages {
    stage('Name') { steps { sh '...' } }   // one column in the UI
  }
  post { failure { ... } always { ... } }  // runs after, regardless of outcome
}
```

Three things to understand about the pipeline you are about to write:

**Stage order is a cost-ordering, not just a logical one.** Tests run before
builds because a failing test should cost you 30 seconds, not the three minutes
of a full image build. Put your cheapest, most likely-to-fail check first.

**`post { failure { ... } }` is what makes it a deployment pipeline rather than
a deployment script.** By the time the `Verify` stage fails, the new image tag
has already been applied — the broken version *is live*. Only an explicit
`kubectl rollout undo` puts the previous version back. A pipeline that detects
failure but leaves the broken version running has told you about an outage
rather than prevented one.

**The smoke test runs from inside the cluster.** `kubectl run ... --image=curl`
against `http://backend:8000` tests the Service and the pod. Testing through the
NodePort instead would also be testing whether the Jenkins *container's* network
can reach the Minikube node — a different thing, which fails for reasons that
have nothing to do with your commit.

## `[L]` 2.10 — Write the Jenkinsfile

Create `Jenkinsfile` at the repo root:

```groovy
pipeline {
  agent any

  environment {
    NS  = 'tender'
    TAG = "${env.BUILD_NUMBER}"
  }

  stages {
    stage('Checkout') {
      steps { checkout scm }
    }

    stage('Backend tests') {
      steps {
        dir('backend') {
          sh '''
            python3 -m venv .venv
            . .venv/bin/activate
            pip install -q -r requirements.txt pytest
            # Ollama and MongoDB are not available to the pipeline. The live_llm
            # test is the one test that needs them (see backend/README.md).
            pytest -q -m "not live_llm"
          '''
        }
      }
    }

    stage('Frontend tests') {
      steps {
        dir('ui') {
          // npm install, not npm ci: the Windows-generated lockfile fails
          // npm ci on Linux. See the comment in .github/workflows/ci.yml.
          sh 'npm install'
          sh 'npx tsc --noEmit'
          sh 'npm run test -- --run'
        }
      }
    }

    stage('Build images') {
      steps {
        sh "docker build -t tender-backend:${TAG} ./backend"
        sh "docker build -t tender-mcp:${TAG} -f ./backend/Dockerfile.mcp ./backend"
        sh "docker build -t tender-frontend:${TAG} ./ui"
      }
    }

    stage('Load into Minikube') {
      steps {
        // What `minikube image load` does internally, minus the host profile
        // Jenkins doesn't have (2.6). The node's runtime is containerd, so the
        // image goes into its k8s.io namespace, where the kubelet looks.
        sh "docker save tender-backend:${TAG}  | docker exec -i minikube ctr -n k8s.io images import -"
        sh "docker save tender-mcp:${TAG}      | docker exec -i minikube ctr -n k8s.io images import -"
        sh "docker save tender-frontend:${TAG} | docker exec -i minikube ctr -n k8s.io images import -"
      }
    }

    stage('Deploy') {
      steps {
        script { env.DEPLOYED = 'true' }
        // Stamp this build's tag into the manifests (the checkout's copy only)
        // so apply rolls out one revision. Applying the committed :dev tag and
        // then running set image made two, and rollout undo landed on :dev
        // instead of the last good build.
        sh "sed -i -E 's#(image: tender-[a-z]+):dev#\\1:${TAG}#' k8s/*.yaml"
        sh "kubectl apply -n ${NS} -f k8s/"
      }
    }

    stage('Verify') {
      steps {
        sh "kubectl rollout status -n ${NS} deploy/mcp      --timeout=120s"
        sh "kubectl rollout status -n ${NS} deploy/backend  --timeout=120s"
        sh "kubectl rollout status -n ${NS} deploy/frontend --timeout=120s"
        sh """
          kubectl run smoke-${TAG} -n ${NS} --rm -i --restart=Never \
            --image=curlimages/curl -- \
            curl -fsS -m 10 http://backend:8000/api/health > /dev/null
        """
      }
    }
  }

  post {
    failure {
      // The new images are already applied by the time Verify fails, so the
      // broken version is live. Undo is what actually restores service.
      // A failure before Deploy has nothing to undo; undoing then would roll
      // a healthy deployment back to an older revision.
      script {
        if (env.DEPLOYED == 'true') {
          sh "kubectl rollout undo -n ${NS} deploy/backend  || true"
          sh "kubectl rollout undo -n ${NS} deploy/mcp      || true"
          sh "kubectl rollout undo -n ${NS} deploy/frontend || true"
        }
      }
    }
    always {
      sh "docker image prune -f --filter 'until=168h' || true"
    }
  }
}
```

Note the `|| true` on every `post` step: a rollback must not itself fail the
post-block and mask the original error.

Commit both new directories:

```bash
git add k8s/ jenkins/ Jenkinsfile
git commit -m "Add Minikube manifests and a Jenkins pipeline"
```

## `[L]` 2.11 — Create the Jenkins job

Jenkins → **New Item** → name it `tender` → **Pipeline** → OK.

- **Pipeline → Definition:** *Pipeline script from SCM*
- **SCM:** *Git*
- **Repository URL:**
  - If your repo is on GitHub, its clone URL (add credentials if private).
  - For a purely local setup, bind-mount the working copy into Jenkins and use a
    `file://` URL. Recreate the container with one extra mount:
    ```bash
    docker rm -f jenkins
    docker run -d --name jenkins --restart unless-stopped \
      --network minikube \
      -p 8080:8080 -p 50000:50000 \
      -v jenkins_home:/var/jenkins_home \
      -v /var/run/docker.sock:/var/run/docker.sock \
      -v "$HOME/.kube/jenkins:/kube:ro" \
      -v /mnt/c/Users/sanje/OneDrive/Documents/work/ag-ai:/repo:ro \
      -e KUBECONFIG=/kube/config \
      tender-jenkins:lts
    ```
    From PowerShell, use 2.8's PowerShell `run` with one extra line,
    `-v "C:\Users\sanje\OneDrive\Documents\work\ag-ai:/repo:ro"`, ending in a
    backtick like the others.
    then use `file:///repo` as the Repository URL. Jenkins clones from it, so
    **only committed changes are built** — which is the correct behaviour and a
    useful thing to have to internalise early.

    If you use the GitHub URL instead (e.g. `https://github.com/<you>/<repo>`),
    Jenkins only sees what is on `origin`: every "commit" in the rest of this
    tutorial means **commit and push**.
- **Branch Specifier:** `*/azure-container-apps-deploy` (or whichever branch you
  are on — the default `*/master` will silently build the wrong code).
- **Script Path:** `Jenkinsfile`
- **Build Triggers:** tick *Poll SCM*, schedule `H/5 * * * *`.

> **Why polling rather than a webhook?** A webhook needs GitHub to make an
> inbound connection to your Jenkins. Your Jenkins is on a laptop behind a
> router, with no public address. Polling every five minutes is the offline
> equivalent. (`H` means "pick a consistent pseudo-random minute" so that many
> jobs don't all fire on the hour.)

Click **Build Now**.

## `[✓]` 2.12 — Phase 2 exit criteria

Watch the build in **Console Output**. It must go green through all seven
stages. Then:

```bash
kubectl get deploy -n tender -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{..image}{"\n"}{end}'
```

**`[✓] Checkpoint`** — all three images are tagged with the Jenkins build number
(`tender-backend:1`, not `:dev`). That is the proof the pipeline, and not your
hands, put the running code there.

Now make a trivial visible change (edit a heading in `ui/src/`), commit (and push, if the
job builds from GitHub), and either click **Build Now** or wait five minutes for the poll. The build number
increments, the tags increment, and the change appears in the browser. **That is
a working CI/CD pipeline.**

---

# PHASE 3 — Deployment and monitoring

**Goal:** understand what the cluster does during a deploy, see it fail, see it
recover, and know where to look when something is wrong.

## `[T]` 3.1 — What actually happens during a rolling update

When `kubectl apply` changes a Deployment's image reference:

1. The Deployment creates a **new ReplicaSet** for the new image, and scales it
   up by one pod.
2. The new pod starts. Its **readiness probe** begins failing (the app is still
   booting), so no traffic is sent to it.
3. When the readiness probe first succeeds, the pod is added to the Service's
   endpoint list — it now receives traffic.
4. Only then does the Deployment scale the **old** ReplicaSet down.
5. `kubectl rollout status` returns success.

The old ReplicaSet is **kept**, scaled to zero. That is what makes `kubectl
rollout undo` instant: it does not rebuild anything, it just scales the previous
ReplicaSet back up.

Two consequences worth holding on to:

- **Without a readiness probe, step 2 and 3 do not exist.** Kubernetes assumes
  a pod is ready the moment its process starts, sends traffic to a booting app,
  and users get errors during every deploy. The probe is what makes the update
  zero-downtime.
- **Zero-downtime requires more than one replica anyway.** Your `backend` is
  pinned to `replicas: 1`, so there *is* a brief gap. That is the accepted
  trade-off of the per-process rate limiter (see 0.1). For a local tutorial, a
  two-second gap is not a problem — but know it is there rather than believing
  you have HA when you don't.

## `[T]` 3.2 — Readiness vs liveness, and how to get them wrong

| | Readiness | Liveness |
|---|---|---|
| Question | "Can this pod serve traffic *right now*?" | "Is this pod wedged and in need of a restart?" |
| On failure | Removed from the Service's endpoints. Keeps running. | **Container is killed and restarted.** |
| Good target | Cheap, dependency-free endpoint | Cheap, dependency-free endpoint |

**The classic mistake is putting a dependency check in a liveness probe.** Say
liveness hit an endpoint that queries MongoDB. Atlas has a ten-second hiccup. The
probe fails three times. Kubernetes kills your backend and restarts it — and a
restarting pod cannot serve the requests it *could* have served, because most of
your app doesn't need Mongo for most requests. You have converted a small
dependency blip into a full outage, by hand, on purpose.

This is exactly why `/api/health` in `main.py` touches nothing: no MongoDB, no
Ollama, no MCP. Its docstring says so. It is the correct probe target, and its
being "too simple to be useful" is the entire point.

## `[L]` 3.3 — Watch a real rollout

In one Ubuntu terminal:

```bash
kubectl get pods -n tender -w
```

In Jenkins, click **Build Now**. Watch the first terminal. You will see, for each
Deployment in turn: a new pod appear as `Pending` → `ContainerCreating` →
`Running 0/1` (started but not ready) → `Running 1/1`, and only then the old pod
go `Terminating`.

That `0/1 → 1/1` transition is the readiness probe passing. It is the single most
informative thing in that output.

Then inspect the history:

```bash
kubectl rollout history deploy/backend -n tender
kubectl describe deploy/backend -n tender | head -40
```

## `[L]` 3.4 — Break it on purpose, and watch the pipeline save you

This is the most valuable lab in the tutorial. A rollback you have never seen
work is not a rollback you have.

**Experiment A — a failing test stops the deploy entirely.**

Edit any backend test so it fails (e.g. change an expected value in
`backend/tests/test_db_conversations.py`), commit, and build.

```bash
kubectl get deploy -n tender -o jsonpath='{..image}'
```

**`[✓] Checkpoint`** — the pipeline goes red at *Backend tests*, and the running
images are **unchanged**. The broken commit never reached the cluster. This is CI
doing its job: the cheapest possible failure.

Revert that change and rebuild to get back to green.

**Experiment B — a broken image gets rolled back.**

Now break something that tests do not catch — make the container fail to start.
Temporarily edit `backend/Dockerfile`'s last line to a command that exits:

```dockerfile
CMD ["python", "-c", "import sys; sys.exit(1)"]
```

Commit (and push, if the job builds from GitHub) and build. Watch both the
Jenkins console and:

```bash
kubectl get pods -n tender -w
```

What you should see:

1. Tests pass. Images build. They load.
2. `Deploy` succeeds — `kubectl apply` only *records the intent*, it does not
   wait.
3. The new backend pod goes to `CrashLoopBackOff`.
4. `Verify` blocks on `kubectl rollout status` and fails after 120 seconds.
5. `post { failure }` runs `kubectl rollout undo`. The old ReplicaSet scales back
   up. The good pod returns.

**`[✓] Checkpoint`**

```bash
kubectl get pods -n tender          # backend is 1/1 Running again
kubectl rollout history deploy/backend -n tender   # the undo is a new revision
kubectl get deploy -n tender -o custom-columns=NAME:.metadata.name,IMAGE:.spec.template.spec.containers[0].image   # previous build's tag, not :dev
```

The build is red, but **the app is up**. Sit with that for a second — that is the
entire difference between a deployment pipeline and a deployment script.

Revert the Dockerfile, commit, rebuild, confirm green.

## `[L]` 3.5 — Observe the fail-soft path (the silent failure)

Some failures do not crash anything. Scale `mcp` to zero:

```bash
kubectl scale deploy/mcp -n tender --replicas=0
kubectl get pods -n tender
```

Open the app and ask the chat *"what's my balance?"*. You get a plausible,
generic answer with **no actual figures**. Nothing errored. Nothing restarted.
No probe failed. Your monitoring is entirely green.

```bash
kubectl logs -n tender deploy/backend | grep -i mcp
```

The log line about tool discovery is your only signal.

```bash
kubectl scale deploy/mcp -n tender --replicas=1
```

**`[T]` The lesson.** This is the failure class that instrumentation catches and
health checks do not: the system is *up* and *wrong*. It is why the verification
checklist below ends with "ask the chat a question" rather than with "all pods
Running". Green dashboards are a claim about liveness, not about correctness.

## `[L]` 3.6 — The monitoring toolkit you actually have

No Prometheus in this setup (your choice — it would want 2–3 GB of cluster RAM
for a single-user local cluster). Here is what to reach for instead.

**Metrics — CPU and memory per pod:**

```bash
minikube addons enable metrics-server
sleep 30
kubectl top pods -n tender
kubectl top nodes
```

This is what tells you whether the `resources.limits` in your manifests are
sensible. If a pod's memory sits near its limit, it will eventually be
OOM-killed — which shows up as an unexplained restart.

**The visual dashboard:**

```bash
minikube dashboard
```

Opens in your browser. Select the `tender` namespace. Good for browsing pods,
logs, events and resource graphs without memorising flags. Not a substitute for
the CLI, but a fast way to build a mental model early on.

**Logs:**

```bash
kubectl logs -n tender deploy/backend            # current
kubectl logs -n tender deploy/backend -f         # follow
kubectl logs -n tender deploy/backend --previous # the crashed instance -- essential for CrashLoopBackOff
kubectl logs -n tender -l app=backend --tail=50
```

`--previous` is the one people forget. When a pod is crash-looping, the *current*
container has barely started; the reason it died is in the previous one's logs.

**Events — the first place to look when a pod won't start:**

```bash
kubectl get events -n tender --sort-by=.lastTimestamp | tail -20
kubectl describe pod -n tender <pod-name>
```

`describe` ends with an event list that names the actual cause: image pull
failure, failed probe, insufficient memory, missing Secret key.

**Restart counts — your cheapest health signal:**

```bash
kubectl get pods -n tender
```

The `RESTARTS` column. Anything above zero that you did not cause deserves a
`--previous` log read.

## `[L]` 3.7 — The end-to-end verification checklist

Run this after any deploy you care about.

| # | Check | Command | Expected |
|---|---|---|---|
| 1 | Cluster up | `kubectl get nodes` | one node, `Ready` |
| 2 | All pods running | `kubectl get pods -n tender` | 3 pods, `1/1`, `RESTARTS 0` |
| 3 | Right build deployed | `kubectl get deploy -n tender -o jsonpath='{..image}'` | all three carry this build's number |
| 4 | Backend healthy | `kubectl run c --rm -i --restart=Never -n tender --image=curlimages/curl -- curl -fsS http://backend:8000/api/health` | 200 |
| 5 | Tool server found | `kubectl logs -n tender deploy/backend \| grep -i mcp` | discovery succeeded, non-empty tool list |
| 6 | Data path works | `curl "$(minikube service frontend -n tender --url)/api/transactions"` | JSON accounts and transactions |
| 7 | App loads | `minikube service frontend -n tender` | the dashboard opens |
| 8 | **Agent has tools** | ask the chat "what's my balance?" | a real answer **with figures** |

Rows 1–7 can all pass while the app is subtly broken. **Row 8 is the one that
actually proves the system works**, for the reason you saw in lab 3.5.

## `[L]` 3.8 — Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Pod `ErrImagePull` / `ImagePullBackOff` | `imagePullPolicy: Always` with a local-only image, or the image was never loaded | Set `IfNotPresent`; re-run the build (or `minikube image load` from the host) |
| Pod `CrashLoopBackOff` | The process exits on start | `kubectl logs -n tender <pod> --previous` |
| Pod `Pending` forever | Node lacks CPU/memory for the requests | `kubectl describe pod` (see Events); lower `resources.requests` or restart Minikube with more memory |
| Pod `Running 0/1` forever | Readiness probe never passes | `kubectl describe pod` shows the probe failure; check the path and port |
| `docker: permission denied` in a build | The `jenkins` user is not in the socket's group | Rebuild the Jenkins image with the correct `DOCKER_GID` (lab 2.7) |
| `kubectl` inside Jenkins: `no such host`, `connection refused`, or a certificate error | Jenkins is not on the `minikube` network, or its kubeconfig still has `127.0.0.1` / host paths | Redo lab 2.8: regenerate `~/.kube/jenkins/config`, recreate the container with `--network minikube` |
| Chat replies but never uses tools | The `mcp` pod is not running | `kubectl logs -n tender deploy/mcp`. This is almost never a prompt regression |
| Backend 502s on every chat | Ollama unreachable from the cluster | Re-run the lab 1.7 probe; check the Windows firewall on 11434 |
| `connection refused` to Atlas | Atlas IP allowlist | Atlas → Network Access → add your current public IP |
| Transactions panel empty but chat works | Database not seeded, or the wrong `MONGODB_DB_NAME` | Re-run the seed script (lab 1.9); check the ConfigMap |
| Everything gone after a Windows reboot | Minikube does not auto-start | `minikube start` — the profile and its data persist |
| `/bin/sh^M: no such file or directory` | CRLF line endings in a script | See lab 1.10 |
| Jenkins builds old code | Wrong branch in the job config, or you didn't commit | Jenkins clones — uncommitted changes are invisible to it |

## `[T]` 3.9 — Deliberately out of scope, and when you'd add it

Knowing what you *didn't* build is as useful as knowing what you did.

- **Ingress.** NodePort is enough for one machine. `minikube addons enable
  ingress` plus a hosts-file entry is the upgrade if you want `tender.local`.
- **Prometheus + Grafana.** The right next step once you want history —
  "was latency worse last Tuesday?" is a question `kubectl top` cannot answer.
  `helm install kube-prometheus-stack` is the standard route; budget 2–3 GB.
- **A container registry.** `minikube image load` works because exactly one
  machine consumes these images. The moment there are two nodes, you need a
  registry.
- **In-cluster MongoDB or Ollama.** Both would make the cluster stateful or
  GPU-bound, and would contradict every other deployment doc in this repo.
- **Horizontal autoscaling.** Would need the backend's rate limiter made
  distributed first (see 0.1). The prerequisite, not an afterthought.
- **Jenkins agents on Kubernetes.** The right answer for a shared Jenkins; pure
  overhead for a single-user local one.
- **GitOps (Argo CD).** The pipeline *pushes* to the cluster here. GitOps
  inverts that: a controller in the cluster *pulls* from Git. See
  [KUBERNETES_DEPLOYMENT.md](KUBERNETES_DEPLOYMENT.md), which uses that model.

---

# Appendix A — Daily cheat sheet

```bash
# --- start of day ---
minikube start                                  # cluster does not auto-start
docker start jenkins                            # if it isn't already up
kubectl get pods -n tender

# --- looking around ---
kubectl get all -n tender
kubectl get deploy -n tender -o jsonpath='{..image}'   # what build is live?
kubectl top pods -n tender
minikube dashboard

# --- when something is wrong ---
kubectl describe pod -n tender <pod>
kubectl logs -n tender deploy/backend --previous
kubectl get events -n tender --sort-by=.lastTimestamp | tail -20

# --- manual intervention (the pipeline should normally do this) ---
kubectl rollout restart deploy/backend -n tender
kubectl rollout undo    deploy/backend -n tender
kubectl rollout status  deploy/backend -n tender

# --- open the app ---
minikube service frontend -n tender

# --- shut down for the day ---
minikube stop
docker stop jenkins
```

# Appendix B — Full teardown and rebuild

To start the application over without touching Jenkins or the cluster:

```bash
kubectl delete namespace tender
kubectl create namespace tender
kubectl create secret generic tender-secrets -n tender --from-literal=MONGODB_URI='...'
kubectl apply -f k8s/
```

To destroy the cluster entirely (Jenkins and its history survive — they live in
the `jenkins_home` volume on the host daemon):

```bash
minikube delete
```

To remove Jenkins as well, including all job history:

```bash
docker rm -f jenkins && docker volume rm jenkins_home
```

Nothing in Atlas is touched by any of this. Your data is safe from all of it —
which is the payoff of having kept it outside the cluster.

# Appendix C — Where to go next

In rough order of value for what you already have:

1. **Add a build badge and test reports.** `junit 'backend/**/junit.xml'` in a
   `post` block gives Jenkins a test-trend graph across builds.
2. **Add Prometheus + Grafana** (see 3.9) and chart pod memory against the limits
   you set in lab 2.3. You will discover at least one of them is wrong.
3. **Push to a registry** and split build from deploy into two jobs. This is the
   step that makes the pipeline portable to a second machine.
4. **Read [KUBERNETES_DEPLOYMENT.md](KUBERNETES_DEPLOYMENT.md)** and compare the
   push model you just built with the GitOps pull model described there. They
   solve the same problem with opposite arrows, and understanding why is most of
   a modern CD education.
