# Kubernetes Deployment Plan (Free-Tier)

This is the step-by-step plan for moving Tender off `docker-compose` onto a
single-node Kubernetes cluster **on Oracle Cloud, driven by Argo CD**, using only
free services. Follow the steps in order; each one builds on the last.

**Status: still a plan — nothing here is built.** Note the ambiguity this
sentence used to carry: the repo *does* now have Kubernetes manifests in `k8s/`,
but they target a **local Minikube cluster driven by Jenkins** (see
`docs/design.md` and the `Jenkinsfile`), which is a different topology from the
one below. No Oracle Cloud tenancy, no k3s cluster, and no Argo CD `Application`
manifest exist.

> **Predates Phase 8.** This plan was written when the app was two containers.
> It's now three: `backend/specs.md` Phase 8 added an `mcp` service (the MCP
> tool server, `backend/Dockerfile.mcp`) that the backend talks to over
> streamable HTTP. Nothing below is wrong, but it's incomplete — when you
> execute this plan you'll need a third `Deployment`/`Service` pair for `mcp`
> (ClusterIP, port 9000, not externally reachable, `MCP_HOST=0.0.0.0`), a third
> image in the CI `build-and-push` job, and `MCP_SERVER_URL` pointing at the
> `mcp` Service's DNS name in the backend's `ConfigMap`. No readiness gate is
> needed: the backend tolerates the tool server being absent or slow to start
> (see Phase 8), so a plain `Deployment` with no `initContainer` wait is correct.

## Architecture

```
                     Internet (Tailscale Funnel — free HTTPS)
                                    │
                    ┌───────────────▼───────────────┐
                    │   Oracle Cloud Free VM (ARM)   │
                    │        k3s single-node          │
                    │                                  │
                    │  ┌────────────────────────────┐  │
                    │  │ Argo CD (GitOps controller) │  │
                    │  └────────────────────────────┘  │
                    │  ┌────────────────────────────┐  │
                    │  │ Sealed Secrets controller    │  │
                    │  └────────────────────────────┘  │
                    │  ┌────────────────────────────┐  │
                    │  │ Tailscale k8s operator       │  │
                    │  └────────────────────────────┘  │
                    │  ┌───────────┐   ┌─────────────┐ │
                    │  │ frontend  │   │  backend     │ │
                    │  │ (nginx)   │──▶│  (FastAPI)   │ │
                    │  └───────────┘   └──────┬──────┘ │
                    └──────────────────────────┼────────┘
                                                │ Tailscale tailnet (private)
                        ┌──────────────────────┼──────────────────────┐
                        │                                              │
                 ┌──────▼───────┐                          ┌──────────▼─────────┐
                 │ Ollama (LAN) │                          │ MongoDB Atlas (M0)  │
                 │ your machine │                          │ free cloud cluster  │
                 └──────────────┘                          └─────────────────────┘
```

**Cost: $0.** Oracle Always Free VM, k3s (OSS), Argo CD (OSS), Sealed
Secrets (OSS), GHCR (free for this repo), Tailscale free tier (incl.
Funnel), MongoDB Atlas M0 (free forever).

## Decisions made

| Area | Choice | Why |
|---|---|---|
| Cluster | k3s, single node, Oracle Cloud Always Free ARM VM | Free forever, public IP without exposing your home network |
| Ollama | Stays on your LAN, unchanged | No GPU on the free VM; reached over Tailscale instead of the public internet |
| MongoDB | Atlas free tier (M0), unchanged | Already documented in `DEPLOYMENT.md`, zero ops |
| Registry | GitHub Container Registry (GHCR) | Free, integrates with GitHub Actions with no extra auth setup |
| Deploy mechanism | GitOps via Argo CD | Cluster state lives in Git; Argo CD reconciles automatically |
| Secrets in Git | Sealed Secrets (Bitnami) | Encrypted secrets are safe to commit; decrypted only in-cluster |
| Cross-network access | Tailscale (VM ↔ your LAN's Ollama) | Private mesh VPN, free, no port-forwarding |
| Public access | Tailscale Funnel via the Tailscale Kubernetes operator | Free HTTPS with no domain purchase or cert-manager setup |
| Manifest repo | Single repo — `k8s/` folder in this `ag-ai` repo | No second repo to maintain at this scale |
| CI test gate | GitHub Actions runs pytest (excluding `live_llm`) + vitest before building/pushing images | Matches existing `-m "not live_llm"` convention; broken builds never reach GHCR |

---

## Step 1 — Provision the Oracle Cloud free VM

1. Create an [Oracle Cloud](https://www.oracle.com/cloud/free/) account (requires a card for identity verification, but the Always Free tier itself is not billed).
2. Create a VM instance using the **Ampere A1 (ARM)** shape — the Always Free tier gives you up to 4 OCPUs / 24GB RAM total, so one VM with e.g. 2 OCPUs / 12GB RAM is comfortably free and plenty for this app.
3. Use **Ubuntu 22.04** as the image (best k3s/Tailscale package support).
4. Open port **22** (SSH) in the VM's security list/network security group. You will *not* need to open 80/443 publicly — Tailscale Funnel handles that without inbound firewall rules.
5. SSH in and confirm you have a public IP and outbound internet access.

## Step 2 — Install Tailscale on the VM and your Ollama host

Kubernetes-concept note: none yet — this step is just networking, done before k3s exists.

1. [Sign up for Tailscale](https://tailscale.com) (free tier: up to 3 users / 100 devices, more than enough).
2. On the Oracle VM: `curl -fsSL https://tailscale.com/install.sh | sh` then `sudo tailscale up`.
3. On the machine that runs Ollama (your LAN host): install Tailscale the same way, `sudo tailscale up`.
4. In the Tailscale admin console, enable **MagicDNS** — this lets the VM reach Ollama by a stable name like `ollama-host.your-tailnet.ts.net` instead of an IP that might change.
5. Verify from the Oracle VM: `curl http://ollama-host.your-tailnet.ts.net:11434` should reach Ollama's API through the tailnet, privately.
6. In the Tailscale admin console, enable **Funnel** for the tailnet (Settings → Funnel). This is what will later expose the frontend publicly with free HTTPS.

## Step 3 — Install k3s on the VM

**Kubernetes concept: what is k3s?** It's a lightweight, certified Kubernetes distribution that bundles the control plane, a container runtime, and basic networking into a single small binary — ideal for single-node or edge setups. Regular Kubernetes (`kubeadm`) is heavier to install and typically assumes multiple nodes.

1. `curl -sfL https://get.k3s.io | sh -`
2. Verify: `sudo k3s kubectl get nodes` should show one node in `Ready` state.
3. Copy the kubeconfig to your local machine so you can run `kubectl` from your laptop instead of SSHing in every time:
   ```powershell
   # on the VM
   sudo cat /etc/rancher/k3s/k3s.yaml
   ```
   Copy that content locally to `~/.kube/config`, replacing the `server:` IP (`127.0.0.1`) with the VM's Tailscale IP or hostname, so `kubectl` from your laptop talks to the cluster over the tailnet rather than the public internet.
4. Install Tailscale on your **laptop** too (or already have it from Step 2) so it can reach the VM's Tailscale IP for `kubectl` access.

**Kubernetes concept: `kubectl`** is the CLI you use to inspect and control a cluster's state — get resources, view logs, apply manifests. You'll use it for troubleshooting even though Argo CD does the actual deploying.

## Step 4 — Install the Tailscale Kubernetes operator (for Funnel)

**Kubernetes concept: what does an "operator" do?** It's a controller running inside the cluster that watches for specific custom resources (or annotations on normal ones, like `Ingress`) and takes action — in this case, wiring a Kubernetes `Ingress`/`Service` up to a Tailscale Funnel endpoint automatically.

1. Follow Tailscale's [Kubernetes operator install guide](https://tailscale.com/kb/1236/kubernetes-operator) — it's installed via a Helm chart.
2. **Kubernetes concept: Helm.** Helm is a package manager for Kubernetes — instead of hand-writing every manifest for a complex piece of software (like this operator), you install a "chart" someone else authored, with your own config values layered on top.
3. Create an OAuth client in the Tailscale admin console scoped for the Kubernetes operator (the guide walks through this) — this becomes a secret the operator uses to register devices in your tailnet on your behalf.
4. Confirm the operator pod is running: `kubectl get pods -n tailscale`.

## Step 5 — Install Sealed Secrets

**Kubernetes concept: `Secret` objects.** Kubernetes has a built-in `Secret` resource for credentials, but it's only base64-encoded, not encrypted — unsafe to commit to Git as-is. Sealed Secrets solves this: you encrypt a `Secret` locally into a `SealedSecret` (safe to commit, unreadable without the cluster's private key), and a controller running in-cluster decrypts it back into a normal `Secret` at apply time.

1. Install the controller: follow the [Sealed Secrets install guide](https://github.com/bitnami-labs/sealed-secrets#installation) (a single `kubectl apply` of their release manifest, or via Helm).
2. Install the `kubeseal` CLI locally (matching the controller version) — this is what encrypts secrets on your laptop before they ever touch Git.
3. You'll use this in Step 9 to seal: `MONGODB_URI`, `GHCR` pull credentials, and the backend's `.env.production` values.

## Step 6 — Install Argo CD

**Kubernetes concept: GitOps.** Instead of running `kubectl apply` by hand or from CI, you commit the *desired* cluster state (manifests) to Git. A controller (Argo CD) continuously compares the live cluster against what's in Git and reconciles differences automatically. Git becomes the single source of truth — you can see exactly what's deployed by reading the repo, and rollbacks are just a `git revert`.

1. Install Argo CD:
   ```
   kubectl create namespace argocd
   kubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml
   ```
2. Access the Argo CD UI. Since you don't want to open more public ports, either:
   - `kubectl port-forward svc/argocd-server -n argocd 8080:443`, then browse `https://localhost:8080` from your laptop (works over Tailscale + kubeconfig from Step 3), or
   - expose it via the Tailscale operator too, on a separate internal-only hostname (no Funnel needed since only you need this).
3. Get the initial admin password:
   ```
   kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath="{.data.password}"
   ```
   (base64-decode it), log in, and change it.

## Step 7 — Set up GHCR + GitHub Actions CI

**Kubernetes concept: none — this is the CI/image side that feeds Kubernetes, not Kubernetes itself.**

1. No separate signup needed — GHCR uses your existing GitHub account/repo permissions.
2. Create `.github/workflows/ci.yml` in `ag-ai` with three jobs:
   - **test-backend**: sets up Python, installs `backend/requirements.txt` + `requirements-dev.txt`, runs `pytest -m "not live_llm"`.
   - **test-frontend**: sets up Node, installs `ui` deps, runs `npm test` and `npm run lint`.
   - **build-and-push**: runs only after both test jobs pass, on pushes to `main`. Builds `backend/Dockerfile` and `ui/Dockerfile`, tags each image with the Git SHA (and `latest`), pushes to `ghcr.io/<your-github-username>/tender-backend` and `ghcr.io/<your-github-username>/tender-frontend`, using the automatically-provided `GITHUB_TOKEN` (no extra secret needed for GHCR auth from Actions).
3. Make the GHCR packages match your repo's visibility — if the repo is private, GHCR packages default to private too; the cluster will need a pull secret in that case (sealed via Step 9).

## Step 8 — Write the Kubernetes manifests

**Kubernetes concepts you'll use here:**
- **`Deployment`** — describes how many replicas of a pod (your container) should run, and how to roll out updates.
- **`Service`** — a stable internal DNS name/IP that load-balances traffic to a `Deployment`'s pods, so other pods (or Ingress) don't need to track individual pod IPs.
- **`Ingress`** — routes external HTTP(S) traffic to the right `Service` based on path/host. In this setup, the Tailscale operator watches an `Ingress` (or a `Service` with the right annotation) and wires it to a Funnel endpoint.
- **`ConfigMap`** — non-secret configuration values (e.g. `OLLAMA_BASE_URL` pointing at the tailnet hostname).
- **`Namespace`** — a logical partition of the cluster; put this app in its own namespace (e.g. `tender`) to keep it isolated from `argocd`, `tailscale`, `kube-system`, etc.

Create this structure in `k8s/`:
```
k8s/
├── namespace.yaml
├── backend/
│   ├── deployment.yaml       # image: ghcr.io/<you>/tender-backend:<tag>
│   ├── service.yaml          # ClusterIP, port 8000
│   ├── configmap.yaml        # OLLAMA_BASE_URL=http://ollama-host.<tailnet>.ts.net:11434
│   └── sealed-secret.yaml    # MONGODB_URI, GHCR pull secret (created in Step 9)
├── frontend/
│   ├── deployment.yaml       # image: ghcr.io/<you>/tender-frontend:<tag>
│   ├── service.yaml          # ClusterIP, port 80
│   └── ingress.yaml          # Tailscale-operator-annotated Ingress → Funnel
└── argocd-app.yaml           # the Argo CD Application resource pointing at this folder
```

Notes carried over from your existing `docker-compose.yml`/Dockerfiles:
- Backend: no host port published, same as compose (`expose: 8000`, not `ports:`) — only the frontend/nginx (or here, only the `Ingress`) is externally reachable.
- Backend still needs `--proxy-headers --forwarded-allow-ips=*` (already baked into the Dockerfile `CMD`) since traffic still arrives via a proxy layer, just Kubernetes' Ingress instead of nginx directly.
- `FORCE_HTTPS` stays `false` — TLS still terminates in front of this deployment (now at the Tailscale Funnel layer), same reasoning as today's `DEPLOYMENT.md`.

## Step 9 — Seal your secrets

1. Create plain (local-only, never committed) `Secret` YAML for:
   - Backend: `MONGODB_URI` (your Atlas connection string), `OLLAMA_MODEL`.
   - A `docker-registry` secret for pulling from GHCR if your packages are private.
2. Encrypt each with `kubeseal`:
   ```
   kubeseal --format yaml < backend-secret.yaml > k8s/backend/sealed-secret.yaml
   ```
3. Delete the plaintext file locally. Commit only the `SealedSecret` output — this is what's safe in Git.
4. Reference the resulting `Secret` (Sealed Secrets creates a normal `Secret` in-cluster once decrypted) via `envFrom`/`secretKeyRef` in `backend/deployment.yaml`.

## Step 10 — Point Argo CD at the repo

1. Apply `k8s/argocd-app.yaml`, an Argo CD `Application` resource with:
   - `source.repoURL`: this repo's GitHub URL
   - `source.path`: `k8s`
   - `destination`: your cluster, namespace `tender`
   - `syncPolicy.automated`: enabled, so pushes to `main` auto-deploy without manual `kubectl apply` or clicking "sync" in the UI.
2. From here on, **the only way state changes in the cluster is via a Git commit** — that's the GitOps discipline paying off: `git log` on the `k8s/` folder is your deployment history.

## Step 11 — Wire up image updates

GitOps needs *something* to bump the image tag in `k8s/*/deployment.yaml` after CI builds a new image — Argo CD only syncs what's in Git, it doesn't know about new GHCR pushes on its own.

Simplest free option at this scale: have the CI `build-and-push` job (Step 7), after a successful push, also commit the new image tag back into `k8s/backend/deployment.yaml` and `k8s/frontend/deployment.yaml` on `main`, using a bot commit (`git config user.name "github-actions"` + `git push`). Argo CD picks up that commit within its polling interval (default 3 minutes, or instantly if you enable a webhook) and rolls out the new version.

(A dedicated tool, **Argo CD Image Updater**, automates this without a CI commit step — worth adopting later if the manual-commit approach feels clunky, but it's one more moving part not needed to get started.)

## Step 12 — Verify end to end

1. Check pods: `kubectl get pods -n tender` — both `backend` and `frontend` should be `Running`.
2. Check the Tailscale operator created a Funnel endpoint: `kubectl get ingress -n tender` and the Tailscale admin console's Funnel section.
3. Visit the generated `https://<name>.<your-tailnet>.ts.net` URL from any browser — this is now public, backed by free Let's Encrypt-issued TLS via Tailscale.
4. Send a test chat message; confirm the backend reaches Ollama over the tailnet and MongoDB Atlas over the public internet (Atlas allows this by IP allowlist — add the Oracle VM's IP, or `0.0.0.0/0` if you accept the tradeoff, in Atlas's Network Access settings).
5. Check Argo CD's UI shows the `tender` Application as `Synced` and `Healthy`.

## Step 13 — Ongoing workflow

Once this is all wired up, your day-to-day loop is:
1. Write code, commit, push to `main`.
2. GitHub Actions runs tests → builds images → pushes to GHCR → commits new tags to `k8s/`.
3. Argo CD detects the Git change and rolls out the new pods automatically.
4. You watch it happen in the Argo CD UI, or just check the live URL.

No manual `kubectl apply`, no SSHing into the VM to redeploy — the only manual step left is approving/merging code changes.

---

## Open items to revisit later (not blockers to a first deploy)

- **Backups**: Atlas M0 has limited built-in backup; consider periodic manual exports if this data matters long-term.
- **Resource limits**: add `resources.requests`/`limits` to the Deployments once you see real memory/CPU usage — the free VM's 12GB RAM is finite.
- **Observability**: no logging/metrics stack yet (e.g. free-tier Grafana Cloud) — `kubectl logs` is your only visibility for now, fine at this scale.
- **CORS**: still wide open (`*`) per `INTERVIEW_NOTES.md` — worth tightening to the Funnel hostname once it's stable, since this deployment is now genuinely public.
