# Timpbills Backend — Deployment Preparation Plan

**Status:** Draft — Phase 1 audit complete. Awaiting Adebayo's authorization to execute.
**Author:** DevOps agent (Opus, backend-devops-template-aligned)
**Date:** 2026-05-18
**Scope:** FastAPI backend at `/Users/adebayovictor/Documents/mobile/timp/timpbills-backend`. Edge (nginx + LE) is owned by `nginx-ssl-engineer`; database-side ops (pg tuning, restore drills proper) coordinate with `database-ops-engineer`. This doc covers everything between the app and the host.
**Target:** Docker-Compose on a Contabo VPS (per PRD §11 / TECHNICAL_PRD §12.3).

---

## 0. Security findings — read these first

> Severity legend: P0 = ship-blocker, P1 = before production, P2 = pre-production-hardening, P3 = nice-to-have.

### P0 — Plaintext production-grade secrets sitting on disk
- `/Users/adebayovictor/Documents/mobile/timp/timpbills-backend/.env` contains live (sandbox-tier) third-party credentials in plaintext:
  - `RESEND_API_KEY=re_R5Gqxk…` (production Resend key — sandbox keys don't exist for Resend; this can send real email)
  - `PAYSTACK_SECRET_KEY=sk_test_…` (test key, but rotation-on-leak still required by Paystack)
  - `VTPASS_API_KEY` / `VTPASS_PUBLIC_KEY` / `VTPASS_SECRET_KEY` / `VTPASS_WEBHOOK_SECRET` (all real sandbox creds)
  - `SECRET_KEY=26f706…` (JWT signing key — leak = forge any user's token)
- `timpbills-firebase-adminsdk.json` — Firebase service-account JSON sits unencrypted at the repo root. Has `private_key` field.
- Both files are correctly listed in `.gitignore` and NOT tracked by git (verified). **The risk is local-disk / backup / accidental upload, not a public leak today.**
- **Action required:** establish an encrypted-env policy (`.env.<env>.enc` via OpenSSL AES-256 + PBKDF2, per the backend-devops-template `scripts/env.sh` pattern) and rotate the `RESEND_API_KEY` and `SECRET_KEY` before anything ships to a VPS — those two are the highest-blast-radius values.

### P1 — `FIRST_SUPERUSER_PASSWORD=changethis` in `.env`
- App startup creates a superuser with this password if no user exists. If this value reaches production unchanged, a known-password admin exists from minute one.
- **Action required:** documented "change before first prod boot" check + a startup-time refusal when `ENVIRONMENT=production` AND password matches a denylist (`changethis`, `password`, `admin`).

### P1 — JWT `SECRET_KEY` has no rotation story
- Single static value; refresh tokens in Redis are not keyed by `kid`. Rotation today = all sessions invalidated globally (acceptable but undocumented).
- **Action required:** at minimum, document the rotation runbook. Optional: support a comma-separated decode-secrets list (`SECRET_KEYS=<new>,<old>`) to enable zero-downtime rotation in v2.

### P1 — `BACKEND_CORS_ORIGINS` default is permissive-by-developer-intent
- Default still hits `localhost:3000` / `localhost:8000`. Easy to leak `*` or local origin into production by accident.
- **Action required:** add a `Settings.validate_prod_cors` check — if `ENVIRONMENT in {staging, production}`, reject `localhost` and `*` in CORS.

### P1 — `DEBUG=true` in repo `.env` + no guard against prod-with-debug
- `Settings.DEBUG: bool = False` defaults safely, but the committed `.env.example` and the on-disk `.env` both set `DEBUG=true`. One mis-copied `.env` to the VPS = stack traces in error responses.
- **Action required:** Pydantic validator that errors at boot when `ENVIRONMENT=production` and `DEBUG=true`.

### P2 — `test.db` (157 KB SQLite) sits in repo root
- `*.db` is gitignored — not committed — but its presence at root means a future `docker build` could pull it into the image if the Dockerfile broadens its `COPY` scope. Today the Dockerfile only copies `./app`, so it's safe; flag for the multi-stage refactor.

### P2 — `.dockerignore` does not exclude `tests/` or `htmlcov/`
- Production Dockerfile only copies `./app` so the bloat stays out of the image, but `Dockerfile.dev` does `COPY . .` — it would copy `htmlcov/` (visible in the audit, ~100 files) and `test.db` if used as a build context for prod by accident. Tighten `.dockerignore` to be belt-and-suspenders.

### P2 — Pre-commit hook does not run a secret scanner
- `gitleaks` / `detect-secrets` would have caught the Resend key on first commit. Not on the production critical path, but a cheap install.

### P2 — `bcrypt` pinned to 4.0.x for passlib compat
- Documented in `pyproject.toml`. CVE feed should be watched — Trivy will catch upgrades the project can't take without a passlib bump.

### P3 — `loguru` writes to `logs/app.log` from inside the container
- Compose mounts `./logs` as a bind in dev, but the prod compose has no equivalent yet. Either bind-mount to host for retention or strip the file sink in production and rely on stdout → docker logs → host forwarder.

---

## 1. One-paragraph state-of-the-art

The backend is in **better shape than typical pre-prod**: there's a working multi-stage `Dockerfile` (non-root user, slim base, separate builder), a dev compose stack with healthchecks and `condition: service_healthy` chained `depends_on`, Sentry wiring with PII redaction, request-ID middleware, slowapi rate limits, error-handler middleware, a Celery worker + beat pair, an Alembic chain of 6 migrations, idempotency in Redis with in-flight sentinels (Sprint 5 hardening shows), and a CI workflow that runs lint + tests against real Postgres + Redis services. The **gaps are the production-environment delta**: there is no `docker-compose.prod.yml`, no CD workflow, no encrypted-env policy, no backup cron, no Trivy/Semgrep gates, no `concurrency:` on deploys (because there are no deploys yet), no VPS bootstrap docs, no `gunicorn` worker tuning despite gunicorn being a dependency, and the prod `Dockerfile` runs `uvicorn` directly with no worker count or graceful-shutdown signal handling.

---

## 2. Production-readiness gap matrix

Each row: **Gap → Why it matters → Proposed fix → Effort (S/M/L/XL)**.

### 2.1 Container image (Dockerfile)

| # | Gap | Why it matters | Proposed fix | Effort |
|---|---|---|---|---|
| D-01 | Prod `Dockerfile` runs `uvicorn` directly, no worker count | Single uvicorn process = single CPU core utilised on a 4 vCPU VPS; no graceful drain on `SIGTERM` in some uvicorn versions | Switch CMD to `gunicorn -k uvicorn.workers.UvicornWorker -w ${WEB_CONCURRENCY:-4} --graceful-timeout 30 --timeout 60 app.main:app -b 0.0.0.0:8000`. Pin `WEB_CONCURRENCY` per-env. | S |
| D-02 | No HEALTHCHECK in Dockerfile (only in compose) | Compose healthcheck is enough for compose, but Trivy / Docker Hub scanners flag missing HEALTHCHECK. Belt-and-suspenders. | Add `HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/v1/health', timeout=4)"` | S |
| D-03 | `apt-get install curl` in prod runtime image | Curl shipped to prod for no functional reason (build-time use only). Removes ~3 MB and a CVE surface. | Move `curl` into builder stage only; runtime stage installs `libpq5` only. | S |
| D-04 | `POETRY_VERSION=1.8.4` floats with installer | Reproducibility: a Poetry installer URL change can break the build. | Pin to `1.8.4` (done) AND verify checksum on download OR switch to `pip install poetry==1.8.4` in a `RUN` (slower but deterministic). | S |
| D-05 | Image is not pinned by digest | `python:3.11-slim` tag can shift under us; we can't reproduce the exact image weeks later. | Pin to `python:3.11-slim@sha256:…`. Renovate or Dependabot can keep it fresh. | S |
| D-06 | No `Dockerfile` ARG for `APP_VERSION` / `GIT_SHA` | We can't tell from a running container which commit produced it. | `ARG GIT_SHA`; `ENV APP_GIT_SHA=$GIT_SHA`; surface in `/health` response. | S |
| D-07 | `useradd -m` creates a `/home/appuser` we don't use | Minor: extra image bytes; minor: `/home` shouldn't be writable in prod. | `useradd -r -u 1000 -d /app appuser` (system account, no home). | S |
| D-08 | No `.dockerignore` exclude for `tests/`, `htmlcov/`, `.coverage*`, `*.db`, `docs/` | Today the prod Dockerfile only `COPY ./app` so it's safe — but the dev Dockerfile does `COPY . .`. Future regression risk. | Tighten `.dockerignore` to exclude tests, coverage artifacts, docs, htmlcov, all `*.db`, `.env*`, `.git`, `*.md`, `htmlcov/`. | S |

### 2.2 Docker Compose — production

| # | Gap | Why it matters | Proposed fix | Effort |
|---|---|---|---|---|
| C-01 | **No `docker-compose.prod.yml` exists** | The only compose file is dev (bind-mounts source, `Dockerfile.dev`, exposed Postgres on `5433`, exposed Redis on `6380`). Cannot deploy as-is. | Create `docker-compose.prod.yml` covering: api + worker + beat + db + redis. See §3 for the full spec. | M |
| C-02 | Dev compose exposes Postgres on `0.0.0.0:5433` and Redis on `0.0.0.0:6380` | If reused for prod, these become public Postgres/Redis endpoints on the VPS. | Prod compose: drop `ports:` for db and redis entirely; only the `api` and `worker` see them via service-name DNS. | S |
| C-03 | No `mem_limit` on any service | Single OOM event kills the wrong container under pressure. | Defaults: api 900m, worker 700m, beat 200m, db 1200m, redis 256m. Override via env per VPS size. | S |
| C-04 | No log rotation on any service | json-file driver default = unbounded. Disk fills in ~weeks. | Every service: `logging: { driver: json-file, options: { max-size: 10m, max-file: 3 } }`. | S |
| C-05 | API healthcheck uses `curl -f` | `curl` is no longer installed in the slim runtime if we drop it (D-03). | Switch to `python -c "import urllib.request; urllib.request.urlopen(...)"` — works on any Python image. | S |
| C-06 | Worker / beat have no healthcheck | Beat crashes silently are a fintech footgun (no reconciliation runs = stuck-pending bills). | Worker: `celery -A app.workers.celery_app inspect ping -d celery@$HOSTNAME` (interval 30s, start_period 60s). Beat: simpler — `pgrep -f 'celery beat'`. | S |
| C-07 | Image source baked to `Dockerfile.dev` | Prod compose must NOT build dev image. | Prod compose pulls `${API_IMAGE:-timpbills-api:latest}` and never `build:`. CD ships the image. | S |
| C-08 | No `container_name` collision strategy for staging+prod on one VPS | If staging and prod ever co-tenant a VPS, container names collide. | Prefix with `${COMPOSE_PROJECT_NAME:-timpbills}_<service>`. Set `COMPOSE_PROJECT_NAME=timpbills-staging` per env. | S |
| C-09 | No `restart: unless-stopped` on `beat` (currently set) — actually it IS set, audit passed. | — | — | — |
| C-10 | DB stores data in named volume but no off-host backup | `postgres_data` is local; VPS reinstall = data loss. | Cron pg_dump → encrypted off-host (Backblaze B2 per PRD §11). See §2.6. | M |
| C-11 | No PgBouncer | Pure FastAPI + Celery worker = ~12 long-lived Postgres connections per `WEB_CONCURRENCY=4` + worker concurrency. Tolerable at MVP scale. | Defer to v2 / when connection count > 50. Documented as a known migration point. | — |

### 2.3 CI workflow (`.github/workflows/ci.yml`)

| # | Gap | Why it matters | Proposed fix | Effort |
|---|---|---|---|---|
| CI-01 | Lint runs inline in test job; not parallelized | Slower feedback loop. | Split into `lint`, `test`, `semgrep-scan` parallel jobs; `build` needs all three. | M |
| CI-02 | No mypy in CI (Makefile has it; CI doesn't) | Type-error regressions slip in. | Add `poetry run mypy app` to the lint job. | S |
| CI-03 | No Semgrep | Source-code SAST gate missing. Fintech requires it. | New `semgrep-scan` job running `p/python`, `p/security-audit`, `p/owasp-top-ten`, `p/jwt`, `p/secrets`. Block on ERROR. | S |
| CI-04 | No Trivy | No CVE gate on dependencies or image. | New `trivy-scan` job after `build`. Upload SARIF to GitHub Security tab. `continue-on-error: true` initially; tighten once findings triaged. | S |
| CI-05 | No image build in CI | Prod image is never built/pushed today. | Add `build` job using `docker/build-push-action@v5` → push to GHCR tagged by `${{ github.sha }}` AND `staging` / `production` on the respective branches. | M |
| CI-06 | Codecov upload uses deprecated v4 syntax | Will eventually break. | Switch to `codecov/codecov-action@v5` with `token: ${{ secrets.CODECOV_TOKEN }}`. | S |
| CI-07 | CI test job uses bcrypt 4.0.x via pyproject — but secret `SECRET_KEY: test-secret-key-for-ci` is short | App likely rejects short secrets at boot in production (it doesn't today; flag as P3 hardening). | No-op today; track if app gains a length check. | — |
| CI-08 | No alembic-from-scratch test | Migrations could be broken at HEAD without anyone noticing until first prod deploy. | Add a step BEFORE `pytest`: `poetry run alembic upgrade head` against the CI Postgres. Fails fast. | S |
| CI-09 | `pull_request` trigger runs on `main` + `develop` | Forks could submit PRs that trigger secrets-needing jobs. | Add `if: github.event.pull_request.head.repo.full_name == github.repository` on jobs that touch secrets. Today CI uses no secrets so this is documented hardening. | S |
| CI-10 | No concurrency control | Two PR pushes in quick succession run duplicate jobs. | `concurrency: { group: ci-${{ github.ref }}, cancel-in-progress: true }`. | S |

### 2.4 CD workflow

| # | Gap | Why it matters | Proposed fix | Effort |
|---|---|---|---|---|
| CD-01 | **No CD workflow exists** | Every prod deploy is a manual SSH session. Error-prone. | New `cd.yml`: `workflow_run` on CI success → staging-deploy → staging-smoke → production-deploy (manual approval via Environment protection). | XL |
| CD-02 | No staging environment configured | Per PRD §12 there's supposed to be one. | Provision staging VPS (separate Contabo box or same box different ports). Create GitHub Environment `staging`. | M (VPS) + S (GH config) |
| CD-03 | No `concurrency: { group: deploy-<env>, cancel-in-progress: false }` on deploy job | Two simultaneous prod deploys can race and corrupt state. | Standard pattern; non-negotiable on the prod job. | S |
| CD-04 | Deploy strategy not chosen: GHCR-pull-on-VPS vs scp-tarball | Affects rollback, auth complexity, deploy latency. | **Recommend GHCR-pull** (CI already builds the image; VPS pulls by SHA tag). Rollback = pull previous SHA. Adebayo's template default is scp-tarball, but pulling is simpler here. **Discuss before coding.** | brainstorm-first |
| CD-05 | No pre-deploy migration step | A migration that fails mid-deploy leaves the app down. | Run `alembic upgrade head` in a one-shot container BEFORE swapping the running api. Fail fast = no app swap. | S |
| CD-06 | No post-deploy health gate | Could declare "deployed" while the app is 500-ing. | `curl -fsS https://<host>/api/v1/health` with retries (10 × 6s) before declaring success. | S |
| CD-07 | No rollback path | If a deploy fails, manual intervention required. | Either keep last image as `${slug}-api:previous` tag OR record prior SHA in a file on the VPS that the deploy script reads when invoked with `--rollback`. | M |
| CD-08 | No staging E2E gate before prod | Prod is one approval click from a broken staging. | Smoke test job: hits `/api/v1/health`, runs `pytest tests/e2e -q` against staging if present, exit 0 only if both pass. | M |

### 2.5 Secrets & env strategy

| # | Gap | Why it matters | Proposed fix | Effort |
|---|---|---|---|---|
| S-01 | No encrypted-env workflow | `.env.staging` / `.env.production` will live somewhere; today that "somewhere" is undefined. | Adopt the `scripts/env.sh` pattern from `backend-devops-template` — AES-256-CBC + PBKDF2(100k). Commit `.env.<env>.enc` only. Key in GH Environment secret `ENV_ENCRYPTION_KEY` + local `.env.key` (gitignored). | M |
| S-02 | No per-env GitHub Environment | Today the repo has one CI workflow with inline test envs. Prod-grade ops needs `staging` / `production` Environments. | Create them. Move `VPS_HOST`, `VPS_SSH_KEY`, `VPS_USERNAME`, `DEPLOY_PATH`, `APP_URL`, `API_PORT`, `ENV_ENCRYPTION_KEY` to per-env secrets. Repo-level: `CI_SECRET_KEY`, `CI_*` test data only. | S |
| S-03 | `FCM_CREDENTIALS_PATH` points at an absolute host path | Will not exist on the VPS; deploy will silently fall back to FakePushClient. | Pivot to `FCM_CREDENTIALS_JSON` (inline JSON via env) — already supported by `app/core/config.py` with validation. Document the base64 wrap if line breaks are a concern. | S |
| S-04 | `SECRET_KEY` rotation policy undocumented | When (not if) we suspect a leak, ops needs a runbook. | Write a 5-step rotation runbook in `docs/runbooks/rotate-secret-key.md`. | S |
| S-05 | No pre-commit secret scan | A future leak isn't blocked at the source. | Add `gitleaks` to `.pre-commit-config.yaml`. | S |
| S-06 | `.env.example` ships sample values that look real (`changethis`, `replace-with-…`) but no validator refuses them at boot in production | A copy-paste error reaches prod silently. | Pydantic validator: if `ENVIRONMENT=production` AND any "well-known placeholder" present → refuse to start. Denylist: `changethis`, `replace-with-…`, `test-secret-key-for-ci`, `your.email@example.com`. | S |

### 2.6 Backups

| # | Gap | Why it matters | Proposed fix | Effort |
|---|---|---|---|---|
| B-01 | **No backup story** | Fintech with no backups = lost wallet balances on disk failure. PRD §11 mandates Backblaze B2. | Daily cron on VPS: `pg_dump -Fc | age -e -i /etc/timpbills/age.pub > /backups/timpbills-$(date +%Y%m%d).pgdump.age` → `rclone copy` to B2. | M |
| B-02 | No restore drill script | Untested backups are wishes. | `scripts/restore_db.sh <backup-file>` — drops connections, restores into a staging-clone DB, verifies row counts. | M |
| B-03 | No retention tier policy | Unbounded B2 spend OR too-short retention. | Local on VPS: 7 days. B2: 30 days hot + monthly archive for 1 year. | S |
| B-04 | No pre-restore safety snapshot | Restoring the wrong file = double-disaster. | `restore_db.sh` always pg_dumps current state to `/backups/_pre_restore_$(date +%Y%m%d%H%M).pgdump` before applying. | S |
| B-05 | No backup-status monitoring | Silent backup failure for weeks. | `scripts/backup_status.sh` outputs latest local + B2 timestamps; cron → curl to UptimeRobot heartbeat OR push to Sentry as a daily check-in event. | S |
| B-06 | Quarterly drill not scheduled | Trust without verification. | Calendar reminder + `docs/runbooks/restore-drill.md`. | S |

### 2.7 Observability

| # | Gap | Why it matters | Proposed fix | Effort |
|---|---|---|---|---|
| O-01 | Loguru `logs/app.log` file sink runs inside the container | If the host doesn't bind-mount `./logs`, logs vanish on container recreate. | Strip the file sink when `ENVIRONMENT in {staging, production}`; rely on stdout → docker → host (`/var/log/docker/`). | S |
| O-02 | Logs are not JSON in production | Greppable but not parseable; future log shipping needs JSON. | Conditional `serialize=True` on the loguru handler when `ENVIRONMENT != development`. | S |
| O-03 | `request_id` middleware is present but no log binding shown | Need to verify every log line within a request includes the request_id. | Read `app/middleware/request_id.py` + audit the loguru config to ensure context propagation. Likely already done — verify before recommending changes. | S |
| O-04 | Sentry traces sample rate is 5% — fine, but `profiles_sample_rate=0` | Profiles are useful for the first month post-launch. | Bump to 0.05 in production for 30 days, then re-evaluate. | S |
| O-05 | No UptimeRobot endpoint configured | PRD §11 says use it; nothing's set up. | Create monitor for `https://api.timpbills.com/api/v1/health` (60s interval) and `https://staging-api.timpbills.com/api/v1/health` (60s). Page to email + SMS. | S (config only) |
| O-06 | No structured access log middleware separate from loguru | Today's `LoggingMiddleware` logs to loguru with `f"{method} {path} ... {status_code}"` — fine for grep, weak for analytics. | Optional: replace with a structured log call emitting `{method, path, status, latency_ms, user_id, request_id}`. Defer to post-launch. | M (defer) |
| O-07 | No metric scraping endpoint | Future ops will want Prometheus-style `/metrics`. | `prometheus-fastapi-instrumentator`. Defer to v2 unless Adebayo wants it for launch — Sentry covers crashes; UptimeRobot covers liveness; the gap is request-rate / error-rate dashboards. | L (defer) |

### 2.8 Idempotency / runaway-tx safeguards

| # | Gap | Why it matters | Proposed fix | Effort |
|---|---|---|---|---|
| I-01 | Idempotency state lives in Redis only | Redis maxmemory eviction = lost in-flight sentinels = a replay could double-process | Audit Redis config: `maxmemory-policy noeviction` (NOT `allkeys-lru`) for the prod instance, OR allocate a dedicated DB index that's never evicted. **Currently the only safeguard.** | S (config) |
| I-02 | No alert on `IDEMPOTENCY_CONFLICT` rate spike | Spike = a mobile retry bug; we want to know early. | Sentry alert rule: > 10 `IdempotencyConflict` events per hour. | S |
| I-03 | `IN_FLIGHT_TTL_SECONDS = 60` | Lone-VTPass-request slower than 60s = sentinel expires and a retry sneaks through | Audit empirically: what's the p95 latency of a VTPass call in production traffic? If >40s, raise to 120s. Track. | M (track post-launch) |
| I-04 | Stuck-pending transaction alarm | Bill stuck in `PROCESSING` > 30 min should page someone | New Celery task `alarm_stuck_pending` daily; emits Sentry-level error if any tx > 30min in non-terminal state. | M |
| I-05 | No max-retries on `reconcile_pending_*` tasks | A truly broken VTPass requery could loop forever every 2 min. | `tenacity` retry annotations + max_retries on the task itself. Check current code — likely already handled. | S (verify) |

### 2.9 VPS provisioning

| # | Gap | Why it matters | Proposed fix | Effort |
|---|---|---|---|---|
| V-01 | No VPS bootstrap script | Re-creating the VPS is undocumented. | `scripts/vps_bootstrap.sh` — runs as root once on a fresh Ubuntu 24.04 box. Installs docker, docker-compose-plugin, ufw, fail2ban, unattended-upgrades, creates `deploy` user, configures SSH key-only login. | M |
| V-02 | No firewall policy | Default Contabo image has all ports open. | UFW: deny in default, allow `22/tcp`, `80/tcp`, `443/tcp`. **Postgres / Redis MUST NOT be open externally.** | S |
| V-03 | No SSH hardening | Root password login is the largest blast radius. | `/etc/ssh/sshd_config`: `PermitRootLogin no`, `PasswordAuthentication no`, `PubkeyAuthentication yes`. Restart sshd AFTER deploy user verified. | S |
| V-04 | No fail2ban | Brute-force attempts unblocked. | Default jail.local — `sshd` jail with bantime 24h after 5 attempts. | S |
| V-05 | No unattended-upgrades for OS security patches | A kernel CVE sits unpatched. | `unattended-upgrades` package configured for security-only auto-updates with auto-reboot disabled (we reboot on our terms). | S |
| V-06 | No filesystem layout convention | Random directories sprawl across `/`. | `/opt/timpbills` (prod), `/opt/timpbills-staging`, both owned by `deploy:deploy`. Backups: `/backups`. Logs: `/var/log/timpbills`. Documented. | S |
| V-07 | No `deploy` user with `docker` group | Today's deploys would need root. | Create `deploy` user; add to `docker` group; SSH-only login. | S |
| V-08 | No swap configured | Contabo default has no swap; an OOM panic kills the box. | 2GB swap file as a buffer. Not a substitute for mem_limits — a backstop. | S |

### 2.10 Pre-prod smoke test plan

| # | Gap | Why it matters | Proposed fix | Effort |
|---|---|---|---|---|
| T-01 | No documented smoke-test list | Manual "did it work" varies between deploys. | `docs/runbooks/smoke-tests.md` — 8 checks: health endpoint, auth register, OTP verify (fake), login, wallet GET, Paystack init (sandbox), webhook signature verification (synthetic POST), `/api/v1/docs` 200. | S |
| T-02 | No staging-only seed data | Manual user creation = inconsistent. | `scripts/seed_staging.py` — idempotent — creates 3 personas (KYC0, KYC1, KYC2) with known credentials. Run after every staging deploy. | M |
| T-03 | No load baseline | First production traffic spike = surprise. | k6 script: 50 concurrent users, 10 minutes, against staging, after a deploy. PRD §13 has the load profile (100 concurrent, 1000 txns/hour). | M |
| T-04 | No DR drill | Quarterly restore drill not yet scheduled. | Calendar; runbook `docs/runbooks/dr-drill.md`. | S |

### 2.11 Documentation

| # | Gap | Why it matters | Proposed fix | Effort |
|---|---|---|---|---|
| DOC-01 | No `docs/deployment/` directory | All this knowledge sits in this plan; needs to live somewhere durable post-rollout. | Once Phase 4 lands: split this plan into `DEPLOYMENT_GUIDE.md`, `RUNBOOKS/<…>.md`, `CICD_PIPELINE.md` per the template structure. | M |
| DOC-02 | No incident-response runbook | When (not if) a prod incident hits, ops needs a playbook. | `docs/runbooks/incident-response.md` — sev levels, paging, comms templates, post-mortem template. | M |
| DOC-03 | No on-call rotation (solo dev) | Adebayo IS the rotation today. Document that, plus the "what if Adebayo is unreachable" backup path. | One paragraph in the README; not a blocker. | S |

---

## 3. `docker-compose.prod.yml` — proposed shape

This is a sketch — not for execution yet — to ground the discussion.

```yaml
# docker-compose.prod.yml — Timpbills production. Pulled image only; no build:.
# Set COMPOSE_PROJECT_NAME per-env (timpbills, timpbills-staging) before invoking.
services:
  api:
    image: ${API_IMAGE:-ghcr.io/<owner>/timpbills-backend:latest}
    container_name: ${COMPOSE_PROJECT_NAME:-timpbills}_api
    ports:
      - "${API_PORT:-8000}:8000"
    env_file: .env
    depends_on:
      db: { condition: service_healthy }
      redis: { condition: service_healthy }
    restart: unless-stopped
    mem_limit: ${API_MEM_LIMIT:-900m}
    healthcheck:
      test: ["CMD", "python", "-c",
             "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/v1/health', timeout=4)"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 40s
    logging:
      driver: json-file
      options: { max-size: "10m", max-file: "3" }

  worker:
    image: ${API_IMAGE:-ghcr.io/<owner>/timpbills-backend:latest}
    command: celery -A app.workers.celery_app worker -l info --concurrency=${WORKER_CONCURRENCY:-2}
    container_name: ${COMPOSE_PROJECT_NAME:-timpbills}_worker
    env_file: .env
    depends_on:
      db: { condition: service_healthy }
      redis: { condition: service_healthy }
    restart: unless-stopped
    mem_limit: ${WORKER_MEM_LIMIT:-700m}
    healthcheck:
      test: ["CMD-SHELL", "celery -A app.workers.celery_app inspect ping -d celery@$$HOSTNAME || exit 1"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 60s
    logging:
      driver: json-file
      options: { max-size: "10m", max-file: "3" }

  beat:
    image: ${API_IMAGE:-ghcr.io/<owner>/timpbills-backend:latest}
    command: celery -A app.workers.celery_app beat -l info
    container_name: ${COMPOSE_PROJECT_NAME:-timpbills}_beat
    env_file: .env
    depends_on:
      db: { condition: service_healthy }
      redis: { condition: service_healthy }
    restart: unless-stopped
    mem_limit: ${BEAT_MEM_LIMIT:-200m}
    healthcheck:
      test: ["CMD-SHELL", "pgrep -f 'celery.*beat' || exit 1"]
      interval: 30s
      timeout: 5s
      retries: 3
    logging:
      driver: json-file
      options: { max-size: "10m", max-file: "3" }

  db:
    image: postgres:15-alpine
    container_name: ${COMPOSE_PROJECT_NAME:-timpbills}_db
    # NO ports: — internal only.
    environment:
      POSTGRES_USER: ${POSTGRES_USER}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      POSTGRES_DB: ${POSTGRES_DB}
    volumes:
      - postgres_data:/var/lib/postgresql/data
      - ./backups:/backups   # for in-container pg_dump → host /backups
    command:
      - postgres
      - -c
      - shared_buffers=${PG_SHARED_BUFFERS:-256MB}
      - -c
      - max_connections=${PG_MAX_CONNECTIONS:-100}
      - -c
      - work_mem=${PG_WORK_MEM:-8MB}
    restart: unless-stopped
    mem_limit: ${DB_MEM_LIMIT:-1200m}
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U $${POSTGRES_USER} -d $${POSTGRES_DB}"]
      interval: 10s
      timeout: 5s
      retries: 5
      start_period: 30s
    logging:
      driver: json-file
      options: { max-size: "10m", max-file: "3" }

  redis:
    image: redis:7-alpine
    container_name: ${COMPOSE_PROJECT_NAME:-timpbills}_redis
    # NO ports: — internal only.
    command:
      - redis-server
      - --maxmemory
      - ${REDIS_MAX_MEMORY:-200mb}
      - --maxmemory-policy
      - noeviction   # NOT allkeys-lru — see I-01
      - --requirepass
      - ${REDIS_PASSWORD}
    volumes:
      - redis_data:/data
    restart: unless-stopped
    mem_limit: ${REDIS_MEM_LIMIT:-256m}
    healthcheck:
      test: ["CMD", "redis-cli", "-a", "${REDIS_PASSWORD}", "ping"]
      interval: 10s
      timeout: 3s
      retries: 5
    logging:
      driver: json-file
      options: { max-size: "10m", max-file: "3" }

volumes:
  postgres_data:
  redis_data:
```

**Note on I-01:** PRD §11 explicitly says `redis: maxmemory-policy allkeys-lru`. **That conflicts with our idempotency model** — `allkeys-lru` can evict an in-flight sentinel under memory pressure, breaking the double-charge guarantee. Recommend `noeviction` for Redis db 0 (or split idempotency into a dedicated Redis instance with `noeviction`, leaving the rest free to evict). **This is a deliberate deviation from the PRD that I'll flag for Adebayo's call.**

---

## 4. Recommended execution phases

Each phase is a self-contained chunk Adebayo authorizes one at a time. **Do not** start without explicit "go on Phase N".

| Phase | Scope | Why this order | Effort |
|---|---|---|---|
| **Phase 1 (this doc)** | Audit + plan | Establish shared understanding before changing anything | DONE |
| **Phase 2** | Production Dockerfile + `docker-compose.prod.yml` + `.dockerignore` tightening + `.env.example` updates | Foundation — nothing else works without a deployable image and compose. Local-only changes; no infra commits. | M |
| **Phase 3** | Encrypted-env system: `scripts/env.sh`, generate `.env.staging.enc` and `.env.production.enc` stubs, document key handling, rotate `SECRET_KEY` and `RESEND_API_KEY` | Closes the P0 secrets finding before any deploy happens | M |
| **Phase 4** | CI workflow rebuild: lint + mypy + test + alembic-from-scratch + semgrep + build + trivy with proper gating + concurrency | Modern CI before we ship. Gives Phase 5 a green-or-block signal. | M |
| **Phase 5** | CD workflow + GitHub Environments + staging deploy script + smoke gate + production deploy with approval + rollback path | The actual deploy pipeline. Depends on Phase 4 green. | L |
| **Phase 6** | VPS bootstrap script + firewall + ssh hardening + `deploy` user + swap + provisioning docs | Hands off to staging environment provisioning. Coordinates with `nginx-ssl-engineer` for the edge tier. | L |
| **Phase 7** | Backups: pg_dump cron + age encryption + B2 sync + retention + restore_db.sh + backup_status.sh + quarterly drill schedule | Hardens DR posture. Coordinates with `database-ops-engineer` for restore-drill ownership. | M |
| **Phase 8** | Observability hardening: JSON logs, request-id audit, Sentry rate review, UptimeRobot setup, stuck-pending alarm, idempotency-conflict alert | Final pre-launch polish. | M |
| **Phase 9** | Smoke-test runbook, load-test baseline, DR drill, incident-response runbook, production cutover checklist | Pre-cutover dress rehearsal. | M |

### Recommended first three to authorize

1. **Phase 2** — Dockerfile + docker-compose.prod.yml + .dockerignore. Pure file authoring; no commits; no infra; reviewable in one sitting. Unblocks everything else.
2. **Phase 3** — Encrypted env + secret rotation. **Closes the only P0** in this audit. Do this before any push to a VPS.
3. **Phase 4** — CI hardening. Gives Phase 5 a green-or-red signal to gate prod deploys on. Concretely: adds mypy, alembic-fresh-DB, semgrep, trivy, build job pushing to GHCR.

After Phase 4 is green, we can decide whether Phase 5 (CD) ships before or after Phase 6 (VPS), depending on whether the staging VPS is provisioned. The two phases can run in parallel if `nginx-ssl-engineer` is ready.

---

## 5. Coordination with sibling agents

- **`nginx-ssl-engineer`** owns:
  - nginx config (`/etc/nginx/sites-available/timpbills.conf`)
  - Let's Encrypt cert issuance + auto-renew
  - HTTP → HTTPS redirect, HSTS, rate-limit-at-edge
  - Upstream config pointing to `127.0.0.1:${API_PORT}` from this plan
  - **Coordination point:** I tell them which port the compose stack binds; they tell me when TLS is live so I can flip smoke tests from `http://` to `https://`.

- **`database-ops-engineer`** owns:
  - Postgres tuning beyond the basic command-line flags above (shared_buffers > 256MB, work_mem, autovacuum, etc.)
  - Restore drill execution + sign-off
  - Schema migration review for destructive ops
  - **Coordination point:** I author `restore_db.sh` and the backup cron; they review and own the quarterly drill.

- **This (DevOps) agent** owns:
  - Everything in this plan
  - Will NOT touch nginx configs unless explicitly asked
  - Will NOT touch Alembic migrations or schema directly

---

## 6. Decisions log (Adebayo, 2026-05-19)

The original "open questions" have been answered. Locked decisions:

1. **Deploy strategy → GHCR-pull.** CI builds image, tags by commit SHA, pushes to GHCR; VPS `docker pull`s. No scp-tarball.
2. **Redis eviction → `noeviction`** (recommended). Protects idempotency cache from silent dropouts. Tune cache misses separately if they become a real problem.
3. **Staging on same VPS as production.** Same physical box, different `COMPOSE_PROJECT_NAME` (`timpbills` vs `timpbills-staging`), different bound ports, separate DB volumes. Cost-optimised; accept blast-radius trade-off.
4. **Gunicorn workers → `WEB_CONCURRENCY=4`** (recommended). Shared box runs app + Celery + Postgres + Redis; 4 is the safe default.
5. **Registry → GHCR.** Authenticate via `GITHUB_TOKEN`; no extra secret to manage.
6. **Domains provisioned: YES** — `timpbills.com` and `api.timpbills.com` already exist. DNS work unblocks `nginx-ssl-engineer` immediately. Confirm A-record / CNAME targets when the VPS IP is known.
7. **Resend key rotation → DEFER.** Key has not left local disk (no Notion / Slack / shared doc exposure). Phase 3 still encrypts it going forward via `.env.production.enc`; rotation can happen at a planned maintenance window post-launch if ever needed. **`SECRET_KEY` (JWT signing) is still rotated as part of Phase 3** — that one always changes when moving to production-grade env handling.

---

*End of plan. Awaiting Adebayo's review and Phase 2 authorization.*
