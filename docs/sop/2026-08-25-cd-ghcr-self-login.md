# CD: self-authenticate the VPS to GHCR during deploy

## What shipped
`.github/workflows/cd.yml` — both `staging-deploy` and `production-deploy` now run
`docker login ghcr.io` on the VPS inside the SSH deploy step, using the run's
ephemeral `GITHUB_TOKEN`, immediately before `docker compose pull`. Removes the
dependency on a manually pre-configured `read:packages` PAT living in the host's
`~/.docker/config.json`.

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

## What's involved
- `.github/workflows/cd.yml` — staging step (~L119-146) and production step
  (~L364-397).

## Verification
- `python3 -c "import yaml; yaml.safe_load(open('.github/workflows/cd.yml'))"` → OK.
- `permissions: packages: write` present at workflow level → `GITHUB_TOKEN` can pull.
- Not yet run live — needs a push (develop → staging, main → production) to exercise.

## Operate / roll back
Re-run the deploy after merging. Rollback = revert this commit; the host then
falls back to needing a manual `docker login` again.

## Follow-ups (NOT fixed here)
- **`.env` missing on the VPS.** Same runs warn `POSTGRES_USER / POSTGRES_PASSWORD
  / POSTGRES_DB / REDIS_PASSWORD ... not set, defaulting to blank`. `docker compose`
  interpolates these from `./.env` at parse time (compose L158-160, L205). Once the
  pull succeeds, `up -d` will start Postgres with blank creds unless `.env` at
  `DEPLOY_PATH` carries them. Decide which:
  1. If the "Decrypt … .env" step logs *"server .env must already exist"* →
     `ENV_ENCRYPTION_KEY` is not set on that GitHub **environment**; set it so the
     `.env.{staging,production}.enc` file decrypts and scp delivers a full `.env`.
  2. If it logs *"Decrypted … successfully"* but the warnings persist → the encrypted
     env file itself lacks those 4 keys; re-encrypt it with the complete env.
