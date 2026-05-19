# CI/CD Pipeline — Timpbills API

## The pipeline at a glance

```
Push to main/develop
        │
        ▼
   ┌────────┐
   │   CI   │  .github/workflows/ci.yml
   │        │
   │ 1. Lint (ruff + black)
   │ 2. Test (pytest + Postgres + Redis, with coverage)
   │ 3. Build Docker image (no push)
   │ 4. Trivy filesystem SAST → GitHub Security tab
   └───┬────┘
       │ all green ✓
       ▼
   ┌────────────────────┐
   │   staging-deploy   │  .github/workflows/cd.yml
   │                    │
   │ • Build + save image as tar.gz
   │ • Decrypt .env.staging.enc with ENV_ENCRYPTION_KEY
   │ • SCP image + compose file + alembic + .env → VPS
   │ • docker load; alembic upgrade head; optional seed_data.py
   │ • docker compose up -d
   │ • Health check /api/v1/health (12 × 10s)
   │ • Install daily backup cron (if scripts/backup_cron.sh exists)
   └─────┬──────────────┘
         │ staging healthy ✓
         ▼
   ┌────────────────────┐
   │  staging-api-e2e   │
   │                    │
   │ • Flush http_* rate-limit keys in staging Redis
   │ • pytest tests/e2e -m e2e against $APP_URL
   │ • Upload logs as artifact
   └─────┬──────────────┘
         │ E2E green ✓ AND branch is main
         ▼
   ┌────────────────────┐
   │ production-deploy  │
   │                    │
   │ Same steps as staging-deploy,
   │ targeting /opt/timpbills instead of /opt/timpbills-staging.
   └────────────────────┘
```

## Why split CI and CD via `workflow_run`?

CI runs on every push and PR — cheap, parallel-friendly, no deploy side-effects. CD listens for `workflow_run: completed + success` before touching any infrastructure. This decoupling means:

- A red build can never trigger a deploy.
- You can re-run CD manually (via `workflow_dispatch`) without re-running CI.
- PRs from forks run CI but not CD (they can't access deploy secrets — GitHub policy).

## Why a staging gate?

The staging-deploy + staging-api-e2e pair is the key quality gate. Unit and integration tests in CI use `TestClient` — they call the ASGI app in-process, no network, no real HTTP. That catches ~80% of bugs but misses everything that depends on the real environment: NGINX in front, real TLS, real DB connection strings, real rate limiter, cold-start timing.

The staging E2E suite makes **real HTTP requests** against the deployed staging URL, using test accounts seeded by the deploy. If staging E2E fails, production doesn't deploy — period. This is a textbook progressive-delivery gate.

## What makes real HTTP calls in CI vs. CD?

| Stage | What it runs | Real network? |
|---|---|---|
| CI `test` job | `pytest tests/ -v --cov=app` | No — `TestClient` is in-process ASGI |
| CD `staging-api-e2e` | `pytest tests/e2e -m e2e` against `$APP_URL` | **Yes** — `requests.Session()` against the deployed API |

Put any test that needs a running server under `tests/e2e/` with `@pytest.mark.e2e`. The conftest should skip these when `E2E_BASE_URL` isn't set, so `pytest tests/` in CI skips them cleanly.

### Projects without an E2E suite

The `staging-api-e2e` job degrades gracefully:

| Situation | Behavior |
|---|---|
| No `tests/e2e/` directory | Logs "skipping E2E", exits 0. Production-deploy gate passes. |
| `tests/e2e/` exists but no tests collected with `-m e2e` | pytest exits 5 ("no tests collected"); the job treats this as success. |
| `tests/e2e/` has failing tests | pytest exits non-zero; the job fails; production-deploy is blocked. |

In other words: having no E2E suite never blocks deploys. Having a broken E2E suite does. That's the correct behavior — the alternative (hard-requiring E2E tests) would force projects to create dummy test files just to satisfy CI.

## Branch strategy

| Push to | CI runs? | Staging deploy? | Production deploy? |
|---|---|---|---|
| `develop` | ✅ | ✅ (if CI green) | ❌ |
| `main` | ✅ | ✅ (if CI green) | ✅ (if staging E2E green) |
| `v1.2.3` tag | ✅ (via Docker workflow) | ❌ | ❌ (tag only publishes versioned image) |
| feature branch | ✅ | ❌ | ❌ |
| PR from fork | ✅ (no secrets) | ❌ | ❌ |

## Manual deploy

If you need to force-deploy a branch bypassing `workflow_run`:

**Actions → CD - Staging Gate and Deploy → Run workflow → select environment → Run workflow**

Staging can be manually dispatched from any branch. Production manual dispatch still requires the staging-api-e2e job to pass first (the `needs:` gate is enforced regardless of trigger).

## Image immutability

Every deploy builds a new image tagged with the commit SHA (e.g., `timpbills-api:a1b2c3d`), saves it as `api-image.tar.gz`, and `scp`s it to the VPS. The VPS `docker load`s it, so each deploy runs a specific, immutable image — not whatever's at `:latest`. This is what makes rollback trivial: old SHA-tagged images stay on the VPS until pruned.

The `docker image prune -f` step at the end of production-deploy removes untagged/dangling images but keeps all SHA-tagged ones for the rollback window.

## Adding cross-repo coordination (optional)

If you have a companion frontend repo with its own E2E suite (Playwright, Cypress, etc.), you can make the backend CD dispatch that workflow after staging deploy and wait for it to pass before production deploys. This is how the original Luran pipeline coordinated with the dashboard repo.

The pattern:

```yaml
dashboard-staging-e2e:
  needs: staging-deploy
  steps:
    - name: Dispatch dashboard workflow
      env:
        GH_TOKEN: ${{ secrets.CROSS_REPO_GITHUB_TOKEN }}
      run: |
        curl -X POST \
          -H "Authorization: Bearer $GH_TOKEN" \
          https://api.github.com/repos/$OWNER/$REPO/actions/workflows/staging-e2e.yml/dispatches \
          -d '{"ref":"main","inputs":{"base_url":"'$APP_URL'"}}'
    - name: Poll for completion
      # loop on /actions/workflows/staging-e2e.yml/runs filtered by created_at
```

Then add `dashboard-staging-e2e` to the `needs:` of `production-deploy`. Omitted here to keep the template simple.
