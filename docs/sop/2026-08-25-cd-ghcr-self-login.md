# CD: fix production deploy (GHCR auth + pipefail crontab)

## What shipped
Two `.github/workflows/cd.yml` fixes that took the production deploy from failing to
green (verified: `https://api.timpbills.com/api/v1/health` → `200 {"status":"ok"}`,
2026-08-25):

1. **GHCR self-login** (PR #11 → `main`). Both `staging-deploy` and
   `production-deploy` now run `docker login ghcr.io` on the VPS inside the SSH
   deploy step, using the run's ephemeral `GITHUB_TOKEN`, immediately before
   `docker compose pull`. Removes the dependency on a manually pre-configured
   `read:packages` PAT living in the host's `~/.docker/config.json`.
2. **Pipefail crontab guard** (PR #12 → `main`). Added `|| true` to the production
   backup-cron install pipeline, mirroring the staging block (see "Second root
   cause" below).

> Deploy-chain gotcha (cost a full round-trip): CD triggers on
> `on: workflow_run: workflows:["CI"]`, so it **always executes `cd.yml` from the
> default branch (`main`)** — never from the branch whose CI fired it. The first fix
> pushed only to `develop` had zero effect on the deploy; it had to reach `main`.

## Why
Production (and then staging) deploys failed at `docker compose ... pull` with:

```
ghcr.io/innocent98/timpbills-backend:<sha>  Error error from registry: denied
Error response from daemon: error from registry: denied
```

Root cause: the image is a **private** GHCR package. The GitHub runner pushes it
fine (it has `GITHUB_TOKEN`), but the **VPS is a separate machine** with no GHCR
credentials of its own. The deploy script never logged in — it relied on a PAT
someone had run `docker login` with on the host, once, by hand. That PAT (in
`~/.docker/config.json`) is not a GitHub secret and is invisible in the repo
Settings; it silently expired, and every pull started returning `denied`.
`denied` (not `manifest unknown`) is the tell that it is an **authorization**
failure, not a missing tag — confirmed by `docs/DEVOPS_PHASE234_STATUS.md:201`.

## How
Pass the token + actor into the SSH step via `env:` and forward them with `envs:`,
then log in on the host with `--password-stdin`:

```yaml
env:
  IMAGE_TAG: ${{ steps.meta.outputs.image_tag }}
  GHCR_TOKEN: ${{ secrets.GITHUB_TOKEN }}
  GHCR_ACTOR: ${{ github.actor }}
with:
  envs: IMAGE_TAG,GHCR_TOKEN,GHCR_ACTOR
  script: |
    set -euo pipefail
    ...
    echo "${GHCR_TOKEN}" | docker login ghcr.io -u "${GHCR_ACTOR}" --password-stdin
    docker compose -f docker-compose.prod.yml pull
```

Key decisions / tradeoffs:
- **`GITHUB_TOKEN` over a stored PAT.** It auto-rotates per run and is repo-scoped,
  so nothing to expire or renew. The workflow already grants `permissions:
  packages: write`, which lets the token pull the repo's own package.
- **Token passed via `env:` + quoted `$GHCR_ACTOR`, never inline `${{ }}`** in the
  shell — the injection-safe pattern; `--password-stdin` keeps the token out of
  the process arg list and out of logs (`GITHUB_TOKEN` is also auto-masked).
- Alternative rejected: keep relying on a manual host PAT. It works but reintroduces
  the exact expiry footgun that caused this outage. (A dedicated read:packages PAT
  logged in on the host is still useful for *manual* ops like `docker compose pull`
  by hand — orthogonal to this automated-path fix.)

## Second root cause — pipefail crontab (PR #12)
With the pull fixed, the production deploy reached a **healthy** stack
(`Production API is healthy.`, all containers `Up (healthy)`) and *then* exited 1
on the backup-cron install:

```bash
(crontab -l 2>/dev/null | grep -v "backup_cron.sh"; echo "…") | crontab -
```

On a VPS with no existing crontab, `crontab -l` and `grep -v` both exit 1; with
`set -o pipefail` the pipeline returns 1 and `set -e` aborts the job — *after* a
successful deploy. Staging already carried the `|| true` guard for exactly this;
production did not. Fix = add `|| true`, mirroring staging.

## What's involved
- `.github/workflows/cd.yml` — staging SSH step + production SSH step (GHCR login
  before `compose pull`; `|| true` on the production crontab pipeline).

## Verification (live)
- `python3 -c "import yaml; yaml.safe_load(...)"` → OK.
- `permissions: packages: write` present → `GITHUB_TOKEN` can pull the private image.
- Production deploy run **succeeded** end-to-end: `Log in to GHCR` ✓, `Pull image +
  deploy production` ✓, migrations + `up -d` ✓, health loop ✓.
- External check: `curl https://api.timpbills.com/api/v1/health` → `200
  {"status":"ok"}` (2026-08-25).

## Operate / roll back
Deploys are push-driven (develop → staging, main → production), workflow file taken
from `main`. Rollback = revert PR #11/#12 on `main`; the host then falls back to
needing a manual `docker login` and the crontab step can false-fail again.

## Follow-ups (NOT fixed here)
- **`.env` on the VPS lacks `POSTGRES_USER/PASSWORD/DB` + `REDIS_PASSWORD`.** Deploys
  still warn `… not set, defaulting to blank`; `docker compose` interpolates these
  from `./.env` at parse time (compose L158-160, L205). It came up healthy this time
  only because the prod DB volume was already initialised — a fresh volume would fail.
  Decide which:
  1. "Decrypt … .env" logs *"server .env must already exist"* → `ENV_ENCRYPTION_KEY`
     not set on that GitHub **environment**; set it so the `.env.{env}.enc` decrypts
     and scp delivers a full `.env`.
  2. Logs *"Decrypted … successfully"* but warnings persist → the `.enc` itself lacks
     those 4 keys; re-encrypt with the complete env (`./scripts/env.sh encrypt <env>`).
