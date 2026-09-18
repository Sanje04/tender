# Design — Local Kubernetes + Jenkins CI/CD for Tender

**Status:** design only. Nothing in this document has been built yet — no
`Jenkinsfile`, no `k8s/` manifests, and no Jenkins instance exist in this repo.
Every snippet below is the proposed content for a file that still has to be
created.

**Goal.** Run the whole Tender stack on a local single-node Kubernetes cluster
(Minikube) on a Windows 11 workstation, and drive builds and deploys from a
locally hosted Jenkins, using open-source tooling throughout.

---

## Which deployment doc do I follow?

This repo now has three deployment paths. They are alternatives, not stages of
one pipeline — pick the row that matches what you're doing.

| Doc | Target | Orchestrator | CI/CD | Use it when |
|---|---|---|---|---|
| [DEPLOYMENT.md](../DEPLOYMENT.md) | One host you own | `docker compose` | none (manual `up -d`) | Simplest real deployment; the current default |
| [KUBERNETES_DEPLOYMENT.md](../KUBERNETES_DEPLOYMENT.md) | Oracle Cloud free VM | k3s | GitHub Actions → GHCR → Argo CD (GitOps) | You want a public, always-on, $0 hosted deployment |
| **This doc** | Your own Windows 11 machine | Minikube | Jenkins (local, push-button) | You want to practise/demonstrate a CI/CD pipeline offline, with no cloud account and no public exposure |

The application itself is identical in all three: the same three images, the
same environment variables, MongoDB and Ollama always external.

---

## Architecture

```
  Windows 11 host
  ┌─────────────────────────────────────────────────────────────────────────┐
  │                                                                          │
  │  WSL2 ───────────────────────────────────────────────────────────────┐   │
  │  │  dockerd  (Rancher Desktop, moby runtime — OSS)                   │   │
  │  │                                                                    │   │
  │  │   ┌────────────────────────────┐                                   │   │
  │  │   │ jenkins  (container)       │  1. git checkout                  │   │
  │  │   │  + docker CLI              │  2. pytest / vitest / tsc         │   │
  │  │   │  + kubectl                 │  3. docker build x3               │   │
  │  │   │  + minikube CLI            │  4. minikube image load           │   │
  │  │   │  :8080  (Jenkins UI)       │  5. kubectl apply (build tag)     │   │
  │  │   └──────────┬─────────────────┘  6. rollout status + smoke test   │   │
  │  │              │ mounts: /var/run/docker.sock                        │   │
  │  │              │         ~/.kube, ~/.minikube  (same paths)          │   │
  │  │              ▼                                                      │   │
  │  │   ┌────────────────────────────────────────────────────────────┐   │   │
  │  │   │ minikube node   (itself a container on this same dockerd)  │   │   │
  │  │   │ ──── namespace: tender ──────────────────────────────────  │   │   │
  │  │   │                                                             │   │   │
  │  │   │    Service frontend  (NodePort 30080)                       │   │   │
  │  │   │         │                                                   │   │   │
  │  │   │         ▼                                                   │   │   │
  │  │   │    Deployment frontend ────▶ Service backend :8000          │   │   │
  │  │   │       (nginx)                      │                        │   │   │
  │  │   │                                    ▼                        │   │   │
  │  │   │                            Deployment backend               │   │   │
  │  │   │                               (FastAPI)                     │   │   │
  │  │   │                                 │        │                  │   │   │
  │  │   │       Service mcp :9000 ◀───────┘        │                  │   │   │
  │  │   │            │                             │                  │   │   │
  │  │   │            ▼                             │                  │   │   │
  │  │   │     Deployment mcp                       │                  │   │   │
  │  │   │     (MCP tool server)                    │                  │   │   │
  │  │   │            │                             │                  │   │   │
  │  │   │     ConfigMap tender-config              │                  │   │   │
  │  │   │     Secret    tender-secrets             │                  │   │   │
  │  │   └────────────┼─────────────────────────────┼──────────────────┘   │   │
  │  └───────────────┼─────────────────────────────┼──────────────────────┘   │
  │                   │                             │                          │
  └───────────────────┼─────────────────────────────┼──────────────────────────┘
                      │ host.minikube.internal      │ internet
                      ▼                             ▼
             ┌──────────────────┐        ┌────────────────────────┐
             │ Ollama           │        │ MongoDB Atlas (M0)     │
             │ host or LAN box  │        │ external, free tier    │
             │ :11434           │        │                        │
             └──────────────────┘        └────────────────────────┘
```

Browse the app at `http://<minikube-ip>:30080`, or run
`minikube service frontend -n tender` to have it open the URL for you.

---

## Decisions made

| Area | Choice | Why |
|---|---|---|
| Container runtime | **Rancher Desktop** (Apache-2.0) on WSL2, `dockerd`/moby backend | Fully open source. Docker Desktop is *not* OSS and its licence restricts commercial use at larger organisations — it works identically here if that's acceptable to you, but it contradicts the "open source only" requirement |
| Shell | **WSL2 (Ubuntu), not PowerShell**, for every command in this doc | The Docker socket (`/var/run/docker.sock`) that Jenkins needs lives inside the WSL2 distro. Mixing PowerShell and WSL paths is the single most common reason this setup fails |
| Cluster | **Minikube**, `--driver=docker`, single node | Runs the node as a container on the dockerd you already have — no Hyper-V, no admin rights, no nested-virtualisation conflict with WSL2 |
| Jenkins location | **A container on the host dockerd, outside the cluster** | Jenkins builds the images it deploys. Running it inside the cluster it redeploys means it can restart itself mid-pipeline. Keeping it outside also means a broken deploy never takes CI down with it |
| Docker access for Jenkins | **Docker-outside-of-Docker (DooD)** — mount `/var/run/docker.sock` | Simpler and faster than Docker-in-Docker: no nested daemon, no privileged container, shared image cache. The trade-off is real and is stated below |
| Image delivery to cluster | **`minikube image load`** | One mechanism, works with any driver, needs no registry and no TLS. Alternatives and when to switch are under "Image delivery" |
| MongoDB | **External (Atlas M0)**, not in-cluster | Same as both other deployment docs. A StatefulSet + PVC would make this cluster stateful and the pipeline destructive |
| Ollama | **Stays on the host/LAN**, reached via `host.minikube.internal` | No GPU passthrough into Minikube, and the model is multi-GB — it should not be re-pulled per cluster |
| Secrets | **`kubectl create secret generic`** from `.env.production` | Local, single-operator cluster. Sealed Secrets (the cloud doc's approach) solves committing secrets to a Git repo — there is no GitOps repo here, so it would be ceremony without a benefit |
| backend ↔ mcp startup | **No readiness gating, deliberately** | Carried forward from `docker-compose.yml`. The backend tolerates the MCP server being absent: startup discovery fails soft and the next `/api/chat` retries it. An `initContainer` waiting on `mcp` would violate a documented hard constraint in CLAUDE.md |

### The DooD trade-off, stated plainly

Mounting `/var/run/docker.sock` into the Jenkins container gives that container
root-equivalent control of the host's Docker daemon. Any pipeline Jenkins runs
can start a privileged container on your machine. That is acceptable here
because this Jenkins is local, single-user, not exposed to the network, and only
ever builds this repo. **Do not copy this topology to a shared or
internet-reachable Jenkins** — there, use Kubernetes agents with Kaniko or
Buildah, which build images without a Docker daemon at all.

### Image delivery

Three mechanisms exist. This design uses the first.

1. **`minikube image load tender-backend:42`** *(chosen)* — builds on the host
   daemon, then copies the image into the node. Driver-agnostic, no registry, no
   TLS. Costs a few seconds per image per build for the tar-and-transfer.
2. **`eval $(minikube docker-env)`** — builds *directly* on the node's internal
   daemon, so there is nothing to transfer. Faster, but it silently changes
   which daemon every subsequent `docker` command in that shell talks to, which
   makes pipeline failures confusing to diagnose, and it does not work with
   every driver.
3. **`minikube addons enable registry`** — a real in-cluster registry, closest
   to production. Adds a registry to run, port-forwarding to maintain, and
   insecure-registry configuration to get right.

Switch to (2) or (3) if `image load` becomes the slow step in your pipeline. At
this image size it usually is not.

**Always set `imagePullPolicy: IfNotPresent`** on every container in the
manifests. The default for a `:latest`-style tag is `Always`, which makes
Kubernetes try to pull from Docker Hub, fail, and leave the pod in
`ErrImagePull` — even though the image is sitting right there on the node. This
is the number-one reason locally-loaded images "don't work".

Tag images with the Jenkins build number (`tender-backend:${BUILD_NUMBER}`),
never `:latest`. A moving tag gives Kubernetes no reason to restart the pods, so
the deploy appears to succeed while the old code keeps running.

---

## Part 1 — Prepare the local environment

Run every command in this part from a **WSL2 Ubuntu shell** unless a step says
otherwise.

### Step 1 — Enable WSL2

In an **Administrator PowerShell** (the only step that is not WSL):

```powershell
wsl --install -d Ubuntu
wsl --set-default-version 2
```

Reboot, open Ubuntu, and let it finish first-run setup. Confirm:

```powershell
wsl -l -v          # Ubuntu should show VERSION 2
```

### Step 2 — Install Rancher Desktop (the OSS container runtime)

Download and install Rancher Desktop for Windows from
<https://rancherdesktop.io/>. On first run:

- **Container Engine:** choose **dockerd (moby)**, not containerd. The `docker`
  CLI and Minikube's docker driver both need it.
- **Kubernetes:** **disable** it. Rancher Desktop ships its own k3s; leaving it
  enabled means two clusters competing over your kubeconfig. Minikube is the
  cluster in this design.
- **WSL Integration:** enable it for your Ubuntu distro.

Verify from Ubuntu:

```bash
docker version                   # client AND server must both report
docker run --rm hello-world
ls -l /var/run/docker.sock       # must exist — Jenkins depends on it
```

If `docker version` prints a client but no server, WSL integration is not on for
this distro. Fix that before continuing; nothing later will work.

### Step 3 — Install kubectl and Minikube

```bash
# kubectl
curl -LO "https://dl.k8s.io/release/$(curl -Ls https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
sudo install -m 0755 kubectl /usr/local/bin/kubectl && rm kubectl

# minikube
curl -LO https://storage.googleapis.com/minikube/releases/latest/minikube-linux-amd64
sudo install -m 0755 minikube-linux-amd64 /usr/local/bin/minikube && rm minikube-linux-amd64

kubectl version --client
minikube version
```

### Step 4 — Start the cluster

```bash
minikube start --driver=docker --cpus=4 --memory=6g
minikube status
kubectl get nodes
kubectl create namespace tender
```

Sizing note: 4 CPU / 6 GB is for the cluster alone. Ollama is **not** in here —
it runs on the host and needs its own several GB on top. On a 16 GB machine,
drop to `--cpus=2 --memory=4g`.

### Step 5 — Confirm the cluster can reach Ollama and Atlas

Worth doing before any manifest exists, because this failure looks like an
application bug.

```bash
# Ollama on the Windows host (the Windows firewall must allow inbound 11434)
kubectl run nettest --rm -it --restart=Never --image=curlimages/curl -n tender -- \
  curl -s -m 5 http://host.minikube.internal:11434/api/tags

# Ollama on another LAN machine — use its IP instead
kubectl run nettest --rm -it --restart=Never --image=curlimages/curl -n tender -- \
  curl -s -m 5 http://10.0.0.68:11434/api/tags
```

Either should print a JSON list of models. If the `host.minikube.internal` form
hangs, it is the Windows Defender firewall blocking inbound 11434 — allow it, or
fall back to the LAN IP.

---

## Part 2 — Kubernetes manifests

Proposed layout — to be created as `k8s/` in the repo root:

```
k8s/
  namespace.yaml
  configmap.yaml
  backend-deployment.yaml     backend-service.yaml
  mcp-deployment.yaml         mcp-service.yaml
  frontend-deployment.yaml    frontend-service.yaml
```

The Secret is deliberately **not** a file — it is created imperatively from
`.env.production` so real credentials never land in the repo.

### Config and secrets

Non-secret values, mirroring `backend/.env.production`:

```yaml
# k8s/configmap.yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: tender-config
  namespace: tender
data:
  OLLAMA_BASE_URL: "http://host.minikube.internal:11434"
  OLLAMA_MODEL: "llama3.1:8b"
  MONGODB_DB_NAME: "ag_ai"
  MAX_HISTORY_TURNS: "5"
  FORCE_HTTPS: "false"
  # Unchanged from docker-compose: the backend finds the tool server by Service
  # name. Naming the Service `mcp` means this value needs no edit at all.
  MCP_SERVER_URL: "http://mcp:9000/mcp"
  MCP_HOST: "0.0.0.0"
  MCP_PORT: "9000"
```

Secrets, created from the command line:

```bash
kubectl create secret generic tender-secrets -n tender \
  --from-literal=MONGODB_URI='mongodb+srv://user:pass@cluster.mongodb.net/?retryWrites=true&w=majority'
```

### Deployment shape

All three deployments follow the same shape. Backend shown in full:

```yaml
# k8s/backend-deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: backend
  namespace: tender
spec:
  replicas: 1
  selector:
    matchLabels: { app: backend }
  template:
    metadata:
      labels: { app: backend }
    spec:
      containers:
        - name: backend
          image: tender-backend:dev        # Jenkins overwrites the tag per build
          imagePullPolicy: IfNotPresent    # required — see "Image delivery"
          ports:
            - containerPort: 8000
          envFrom:
            - configMapRef: { name: tender-config }
            - secretRef:    { name: tender-secrets }
          readinessProbe:
            httpGet: { path: /api/transactions, port: 8000 }
            initialDelaySeconds: 5
            periodSeconds: 10
---
# k8s/backend-service.yaml
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

`mcp` is the same with port `9000`. `frontend` is the same with port `80` plus
`type: NodePort` and `nodePort: 30080`, so it is reachable from Windows.

> **Carried-forward constraint:** do not add an `initContainer` or any other
> readiness gate making `backend` wait for `mcp`. The fail-soft discovery path
> in `mcp_client.py` is the designed behaviour, and gating on `mcp` would let a
> slow tool server block the entire API. See CLAUDE.md, "MCP discovery and
> invocation both fail soft".

Manual deploy, to prove the manifests before Jenkins touches them:

```bash
cd backend
docker build -t tender-backend:dev .
docker build -t tender-mcp:dev -f Dockerfile.mcp .
cd ../ui && docker build -t tender-frontend:dev . && cd ..

minikube image load tender-backend:dev
minikube image load tender-mcp:dev
minikube image load tender-frontend:dev

kubectl apply -f k8s/
kubectl rollout status deploy/backend -n tender --timeout=120s
minikube service frontend -n tender
```

---

## Part 3 — Jenkins

### Step 6 — Build a Jenkins image with the tools the pipeline needs

Stock `jenkins/jenkins` has no `docker`, `kubectl`, or `minikube` binary. Rather
than installing them from a pipeline step on every build, bake them in. Create
`jenkins/Dockerfile`:

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
 && curl -L https://storage.googleapis.com/minikube/releases/latest/minikube-linux-amd64 \
      -o /usr/local/bin/minikube && chmod +x /usr/local/bin/minikube \
 && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
 && apt-get install -y nodejs \
 && rm -rf /var/lib/apt/lists/*

# The jenkins user must belong to the group that owns the host's docker socket,
# or every `docker` call in the pipeline fails with a permission error.
# Find the host's GID with:  stat -c '%g' /var/run/docker.sock
ARG DOCKER_GID=988
RUN groupadd -g ${DOCKER_GID} hostdocker && usermod -aG hostdocker jenkins

USER jenkins
```

### Step 7 — Run Jenkins

```bash
DOCKER_GID=$(stat -c '%g' /var/run/docker.sock)
docker build --build-arg DOCKER_GID=$DOCKER_GID -t tender-jenkins:lts jenkins/

docker volume create jenkins_home

docker run -d --name jenkins --restart unless-stopped \
  -p 8080:8080 -p 50000:50000 \
  -v jenkins_home:/var/jenkins_home \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$HOME/.kube:$HOME/.kube:ro" \
  -v "$HOME/.minikube:$HOME/.minikube:ro" \
  -e KUBECONFIG="$HOME/.kube/config" \
  tender-jenkins:lts
```

> **Why both mounts use `$HOME` on both sides.** Minikube writes *absolute* host
> paths for its client certificates into `~/.kube/config`. Mount those
> directories at any other path inside the container and `kubectl` looks for the
> certs where they are not, then fails with a TLS error that says nothing about
> mounts. Keeping the paths identical is what makes the kubeconfig usable inside
> the container unedited.

Unlock and finish setup:

```bash
docker logs jenkins 2>&1 | grep -A2 'initialAdminPassword'
```

Open <http://localhost:8080>, paste the password, install **suggested plugins**,
and create your admin user. Then verify the toolchain from inside the container
— do this now, not after a pipeline fails:

```bash
docker exec -it jenkins bash -lc 'docker ps >/dev/null && echo docker OK; \
  kubectl get nodes >/dev/null && echo kubectl OK; \
  minikube version >/dev/null && echo minikube OK'
```

All three must print OK.

### Step 8 — The Jenkinsfile

To be created as `Jenkinsfile` in the repo root:

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
            # Ollama and MongoDB are not available to CI. The live_llm test is
            # the one test that needs them (see backend/README.md).
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
        sh "minikube image load tender-backend:${TAG}"
        sh "minikube image load tender-mcp:${TAG}"
        sh "minikube image load tender-frontend:${TAG}"
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
        // Smoke-test from inside the cluster so it does not depend on the
        // NodePort being reachable from the Jenkins container.
        sh """
          kubectl run smoke-${TAG} -n ${NS} --rm -i --restart=Never \
            --image=curlimages/curl -- \
            curl -fsS -m 10 http://backend:8000/api/transactions > /dev/null
        """
      }
    }
  }

  post {
    failure {
      // The new images are already applied by the time Verify fails,
      // so an explicit undo is what actually restores the previous version.
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

### Step 9 — Create the job

Jenkins → **New Item** → *Pipeline* → name it `tender`.

- **Pipeline → Definition:** *Pipeline script from SCM*
- **SCM:** Git, repository URL = your repo (for a purely local setup, the
  container path of a bind-mounted working copy also works)
- **Script Path:** `Jenkinsfile`
- **Build Triggers:** *Poll SCM*, schedule `H/5 * * * *` — Minikube is not
  reachable from GitHub, so a webhook would have nothing to call. Polling every
  five minutes is the offline equivalent.

Click **Build Now**.

---

## Verification checklist

| Check | Command | Expected |
|---|---|---|
| Cluster up | `kubectl get nodes` | one node, `Ready` |
| All pods running | `kubectl get pods -n tender` | 3 pods, `1/1 Running` |
| Images are this build's | `kubectl get deploy -n tender -o jsonpath='{..image}'` | all tagged with the build number |
| Tool server reachable | `kubectl logs -n tender deploy/backend \| grep -i mcp` | discovery succeeded, non-empty tool list |
| App loads | `minikube service frontend -n tender` | browser opens the dashboard |
| Agent has tools | ask the chat "what's my balance?" | a real answer, not a generic reply |

That last row is the meaningful end-to-end check. A generic answer with no
account figures means the backend came up but `mcp` did not — by design that
degrades quietly instead of erroring.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Pod stuck `ErrImagePull` / `ImagePullBackOff` | `imagePullPolicy: Always` with a local-only image | Set `IfNotPresent`, re-run `minikube image load` |
| `docker: permission denied` in a Jenkins build | `jenkins` user not in the socket's group | Rebuild the Jenkins image with the correct `DOCKER_GID` (Step 6) |
| `kubectl` TLS / certificate error inside Jenkins | `.minikube` mounted at a different path than on the host | Mount `$HOME/.minikube` at the identical path (Step 7) |
| Chat replies but never uses tools | `mcp` pod not running | `kubectl logs -n tender deploy/mcp`. Not a prompt regression — see CLAUDE.md |
| Backend 502s on every chat | Ollama unreachable from the cluster | Re-run the Step 5 probe; check the Windows firewall on 11434 |
| `connection refused` to Atlas | Atlas IP allowlist | Add your current public IP under Atlas → Network Access |
| Everything gone after a Windows reboot | Minikube does not auto-start | `minikube start` — the profile and its data persist |

---

## Deliberately out of scope

- **Ingress.** NodePort is enough for one local machine. `minikube addons enable
  ingress` plus a hosts-file entry is the upgrade if you want `tender.local`.
- **In-cluster MongoDB or Ollama.** Both would make the cluster stateful or
  GPU-bound, and would contradict the other two deployment docs.
- **Multi-node / HA.** A single node is the point of Minikube.
- **Pushing to a registry.** Nothing outside this machine consumes these images.
- **Jenkins agents on Kubernetes.** The right answer for a shared Jenkins; pure
  overhead for a single-user local one.

---

## Related

- [specs.md](specs.md) — the LLM provider switch (Ollama ↔ Claude API), including
  what it adds to the ConfigMap and Secret above.
- [../backend/specs.md](../backend/specs.md) — phase-by-phase backend history.
- [../DEPLOYMENT.md](../DEPLOYMENT.md) and
  [../KUBERNETES_DEPLOYMENT.md](../KUBERNETES_DEPLOYMENT.md) — the other two
  deployment paths.
