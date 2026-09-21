# docs/

Longer-form documents that don't belong beside the code. **Mixed status — check
the Status column before citing anything here as shipped behaviour.** This
directory used to be proposals only, which is why its own header claimed as much
long after `design.md` had been built.

## Contents

| Doc | What it covers | Status |
|---|---|---|
| [design.md](design.md) | Local Minikube cluster + Jenkins CI/CD pipeline on Windows 11, open-source toolchain throughout | **Built** — `Jenkinsfile`, `jenkins/Dockerfile` and `k8s/*.yaml` all exist and the pipeline has been run, including a break-and-rollback drill |
| [local-deployment-tutorial.md](local-deployment-tutorial.md) | Step-by-step walkthrough of that same pipeline, 30% theory / 70% lab, with checkpoints | **Built** — describes the shipped pipeline |
| [specs.md](specs.md) | Phase 9: switch the agent between Ollama and the Claude API via one `.env` key | **Spec only** — no code changed. Superseded in practice: the Tailscale tunnel (see `AZURE_DEPLOYMENT.md`) removed the need it was written for |

## Shipped behaviour lives elsewhere

| Doc | Covers |
|---|---|
| [../README.md](../README.md) | What Tender is, how to run it |
| [../backend/specs.md](../backend/specs.md) | Backend phase log (Phases 1–8, 10 — shipped; 9 is the spec-only one above) |
| [../ui/specs.md](../ui/specs.md) | Frontend spec + six addenda (shipped) |
| [../DEPLOYMENT.md](../DEPLOYMENT.md) | Single-host `docker compose` deployment (shipped) |
| [../AZURE_DEPLOYMENT.md](../AZURE_DEPLOYMENT.md) | Azure Container Apps deployment — scripts and CI job exist; see the README roadmap for whether an instance is currently running |

## Not built, and not in this directory

| Doc | Covers |
|---|---|
| [../KUBERNETES_DEPLOYMENT.md](../KUBERNETES_DEPLOYMENT.md) | Oracle Cloud + k3s + Argo CD. A plan, despite sitting at the repo root next to the deployment docs that are real |

`CLAUDE.md` is deliberately gitignored — it's a local operating manual, so it is
not linked here; a fresh clone won't have it.
