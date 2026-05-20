# GitHub Actions Setup — Timpbills API

This project ships with three workflows in `.github/workflows/`:

| Workflow | Trigger | What it does |
|---|---|---|
| `ci.yml` | push/PR to `main`, `develop` | Lint, test (with Postgres + Redis service containers), Docker build, Trivy SAST. |
| `cd.yml` | CI success on `main`/`develop`, or manual dispatch | Deploy staging → run API E2E against staging → deploy production if E2E passes. |
| `docker.yml` | push to `main`/`develop`, tags `v*`, PRs | Build and push image to GitHub Container Registry (GHCR) with semver/SHA/branch tags. |

## One-time setup (≈10 minutes)

### Step 1. Generate an SSH keypair for GitHub Actions → your VPS

On your laptop:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/timpbills_deploy -C "gha-timpbills"
# Press Enter for no passphrase (simpler for automation). If you do set one,
# also add it as the VPS_SSH_PASSPHRASE secret.
```

Copy the **public** key to your VPS:

```bash
ssh-copy-id -i ~/.ssh/timpbills_deploy.pub <user>@<vps-host>
# Verify:
ssh -i ~/.ssh/timpbills_deploy <user>@<vps-host> 'echo ok'
```

### Step 2. Create GitHub Environments

Go to **Settings → Environments** on the GitHub repo and create two environments: `staging` and `production`.

Environments give you per-environment secrets and an optional manual approval gate for production.

### Step 3. Add environment secrets

Add the following secrets to **both** environments. Values differ per environment:

| Secret | Staging value | Production value |
|---|---|---|
| `VPS_HOST` | e.g., `vps.example.com` | same host (or different VPS) |
| `VPS_USERNAME` | e.g., `deploy` | same |
| `VPS_SSH_KEY` | contents of `~/.ssh/timpbills_deploy` (the PRIVATE key) | same |
| `VPS_PORT` | `22` (optional) | same |
| `VPS_SSH_PASSPHRASE` | only if the key has one | same |
| `DEPLOY_PATH` | `/opt/timpbills-staging` | `/opt/timpbills` |
| `APP_URL` | `https://staging-api.example.com` | `https://api.example.com` |
| `API_PORT` | `8001` (so staging doesn't clash with prod) | `8000` |
| `ENV_ENCRYPTION_KEY` | passphrase used to encrypt `.env.staging.enc` | passphrase used to encrypt `.env.production.enc` |

> **Why the same VPS, different paths?** You can run both stacks on one server by giving each its own `DEPLOY_PATH`, `COMPOSE_PROJECT_NAME`, and `API_PORT`. Costs less, and staging exercises the real network path.

### Step 4. Add repo-level secrets (shared)

**Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Why |
|---|---|
| `CI_SECRET_KEY` | 32+ char random string used by tests. Generate: `openssl rand -hex 32` |
| `CI_OPENAI_API_KEY` | Only if your tests import OpenAI. Use a throwaway/test key. |

> **Never put real-looking tokens inline in workflow YAML.** The previous version of this repo's CI had plaintext JWT-shaped strings committed in `ci.yml` — even when they're test values, they train bad habits and can be mistaken for leaked production secrets during audits. Always pull from `${{ secrets.X }}`.

### Step 5. Bootstrap VPS directories

SSH into your VPS:

```bash
sudo mkdir -p /opt/timpbills /opt/timpbills-staging
sudo chown "$USER:$USER" /opt/timpbills /opt/timpbills-staging

# Install Docker if not already present:
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER"  # log out/in after this

docker compose version   # should print a v2 version
```

Each stack also needs its own `.env` on the VPS. You have two options:

**Option A — encrypted-in-repo (recommended):** commit `.env.staging.enc` and `.env.production.enc` to git; CD decrypts them at deploy time using `ENV_ENCRYPTION_KEY`. See `docs/deployment/ENV_ENCRYPTION.md`.

**Option B — manual on VPS:** create `/opt/timpbills-staging/.env` and `/opt/timpbills/.env` by hand; CD will leave them in place. Set `chmod 600 .env` — `/opt` is world-readable by default.

### Step 6. First deploy

Push to `develop` → CI runs → on green, staging is deployed → API E2E runs → if green and the push was on `main`, production is deployed.

Or trigger manually: **Actions → CD - Staging Gate and Deploy → Run workflow → Select environment**.

## Verifying the deploy on the VPS

```bash
cd /opt/timpbills    # or /opt/timpbills-staging
docker compose -f docker-compose.prod.yml ps
docker compose -f docker-compose.prod.yml logs --tail=100 api
curl -sS http://localhost:8000/api/v1/health
docker images | grep timpbills-api
docker inspect timpbills-api:latest --format='{{.Created}}'
```

If health is OK and containers are up, the deploy is live.

## Manual rollback

The workflow doesn't auto-rollback. To revert:

```bash
ssh <user>@<vps-host>
cd /opt/timpbills
# Find the previous image tag (SHAs are stored per deploy):
docker images | grep timpbills-api

# Re-tag the previous SHA as what compose pulls:
docker tag timpbills-api:<previous-sha> timpbills-api:latest
docker compose -f docker-compose.prod.yml up -d
```

## Troubleshooting

| Symptom | Check |
|---|---|
| CD fails with `Permission denied (publickey)` | SSH key not added to VPS — rerun `ssh-copy-id`. |
| CD fails with `No such file or directory: /opt/…` | VPS bootstrap skipped — run Step 5 above. |
| CI fails with `Module not found` | `poetry.lock` missing — `git add poetry.lock && git commit`. |
| Docker build fails in CI | Test locally first: `docker build -t timpbills-api .` |
| Health check times out after 12×10s | SSH into VPS; `docker compose logs --tail=200`. Most common cause: missing env var. |

## What each secret is used for

| Secret | Used in |
|---|---|
| `VPS_HOST`, `VPS_USERNAME`, `VPS_SSH_KEY`, `VPS_PORT`, `VPS_SSH_PASSPHRASE` | `cd.yml` — SSH into the VPS for file copy + deploy |
| `DEPLOY_PATH` | `cd.yml` — which directory on the VPS to deploy into |
| `APP_URL` | `cd.yml` — E2E target for staging API tests |
| `API_PORT` | `cd.yml` — local port the container exposes for health checks |
| `ENV_ENCRYPTION_KEY` | `cd.yml` — decrypts `.env.<env>.enc` before scp |
| `CI_SECRET_KEY`, `CI_OPENAI_API_KEY` | `ci.yml` — env for the test job |
| `GITHUB_TOKEN` (auto) | `docker.yml` — logs into GHCR |

That's it.
