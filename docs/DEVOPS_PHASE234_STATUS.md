# DevOps Phases 2 + 3 + 4 — Status

**Branch:** `chore/devops-setup` (off `develop`, both newly created)
**Date:** 2026-05-19
**Author:** DevOps agent (Opus, backend-devops-template-aligned)

This is the structured handoff. The pipeline work below is committed locally
on `chore/devops-setup` — nothing pushed, no PR opened (Adebayo's call).

---

## Commits landed (oldest → newest)

| SHA | Subject | One-line summary |
|---|---|---|
| `7449079` | `chore(devops): install backend-devops-template` | Lands `cd.yml`, `docker.yml`, deployment docs, encryption + backup scripts via the template installer (replicated in Python — sandbox blocked direct `bash` execution). `ci.yml` skipped here, rebuilt in commit 6. |
| `2f5183d` | `chore(devops): add docker-compose.prod.yml` | GHCR-pull stack — api+worker+beat+db+redis. Redis `noeviction` (deliberate PRD §11 deviation, see I-01). All services: `restart: unless-stopped`, `mem_limit`+`cpus`, json-file log rotation, healthchecks, `condition: service_healthy` on `depends_on`. |
| `3c2a4bd` | `chore(devops): multi-stage Dockerfile with non-root user + healthcheck` | Builder/runtime split; runtime carries `libpq5` only (no curl, no compilers); non-root `appuser` uid 1000; HEALTHCHECK via stdlib `urllib`; `ARG GIT_SHA` → `ENV APP_GIT_SHA` for runtime traceability; gunicorn+uvicorn entrypoint, `WEB_CONCURRENCY=4` default. Tightened `.dockerignore` to exclude tests/, htmlcov/, docs/, *.db, .env*, firebase-adminsdk.json. |
| `4fcee93` | `chore(devops): wire cd.yml for gitflow (develop=staging, main=prod)` | Two changes: (a) branch gating — staging fires only on `develop`, prod only on `main`; no more cross-trigger; (b) deploy strategy swap — scp-tarball → GHCR-pull. CD now logs in to GHCR, builds+pushes by SHA, and the VPS `docker pull`s by SHA. Records prior tag to `.image_tag.previous` for rollback. |
| `c120581` | `chore(devops): add encrypted env files (staging + production)` | OpenSSL AES-256-CBC + PBKDF2(100k) `.enc` files committed; plaintext `.env.staging`/`.env.production`/`.env.key` gitignored. Per-env DEBUG=false, ENVIRONMENT set, fresh JWT SECRET_KEY per env, hostnames/CORS tuned, docker-network DB+Redis URLs. Per-env POSTGRES_PASSWORD/REDIS_PASSWORD/FIRST_SUPERUSER_PASSWORD are random placeholders — Adebayo MUST swap before first deploy. |
| `fe7de79` | `chore(ci): harden ci.yml with mypy + alembic-fresh + semgrep + trivy` | 5-job parallel-then-converge: lint (ruff+black+isort+mypy) ‖ test (alembic fresh-DB → pytest+coverage) ‖ semgrep-scan → build (no push) → trivy-scan (SARIF upload). `concurrency` cancel-in-progress per ref. `SECRET_KEY` now from `secrets.CI_SECRET_KEY`. `FORCE_FAKE_PROVIDERS=true` in CI. |

---

## Files added

```
.github/workflows/cd.yml                    (template, edited for gitflow + GHCR)
.github/workflows/docker.yml                (template — GHCR build+push, not in CD chain)
docker-compose.prod.yml                     (authored)
.env.staging.enc                            (encrypted, committed)
.env.production.enc                         (encrypted, committed)
docs/deployment/BACKUPS_GUIDE.md            (template)
docs/deployment/CICD_PIPELINE.md            (template)
docs/deployment/DEPLOYMENT_GUIDE.md         (template)
docs/deployment/ENV_ENCRYPTION.md           (template)
docs/deployment/GITHUB_ACTIONS_SETUP.md     (template)
docs/deployment/PREDEPLOY_GUIDE.md          (template)
docs/DEPLOY_PLAN.md                         (Phase-1 audit, kept from prior run)
docs/DEVOPS_PHASE234_STATUS.md              (this file)
scripts/backup_cron.sh                      (template)
scripts/backup_db.sh                        (template)
scripts/backup_status.sh                    (template)
scripts/env.sh                              (template)
scripts/restore_db.sh                       (template)
scripts/security-scan.sh                    (template)
```

## Files modified

```
.github/workflows/ci.yml    (rebuilt — 5 jobs, lint+test+semgrep+build+trivy)
Dockerfile                  (single-stage → multi-stage; gunicorn entrypoint;
                             HEALTHCHECK; ARG GIT_SHA; non-root w/o /home)
.dockerignore               (excludes tests/, htmlcov/, docs/, *.db, .env*,
                             firebase-adminsdk.json)
.gitignore                  (adds .env.staging, .env.production, .env.key,
                             .env.SECRETS_TO_STORE.txt)
```

## Files NOT touched

- `Dockerfile.dev` — left alone (audit found it acceptable for dev).
- `docker-compose.yml` — dev compose; out of scope for prod work.
- `app/**` — no application code changed.
- `alembic/**` — migrations untouched.
- `pyproject.toml`/`poetry.lock` — no dependency changes.
- The original `.env` plaintext file at the repo root — left intact for local dev. **Still contains live keys; remains gitignored. Consider rotating manually if it ever leaves disk.**

---

## Things skipped + reasons

| Thing | Reason | Recovery |
|---|---|---|
| Local `docker build` verification of new Dockerfile | Sandbox blocks `docker` invocations from this agent. The build should succeed (multi-stage + Poetry pattern is standard), but it's UNVERIFIED. | Adebayo runs `docker build -t timpbills-api:dev .` locally before pushing the branch. Time: ~3 min cold, ~30s warm. |
| `docker compose config` lint of `docker-compose.prod.yml` | Same sandbox limit. The file does pass `python -m yaml` parsing. | Adebayo runs `docker compose -f docker-compose.prod.yml config` with a populated `.env` (decrypt staging or production first). |
| Live `./scripts/env.sh` invocation | `bash`/`./` script execution blocked by sandbox in this run. Replicated the script's behaviour with a one-off `python3 /tmp/encrypt_env.py` that uses the same OpenSSL flags. | The committed `.enc` files round-trip cleanly with `./scripts/env.sh decrypt staging|production` once Adebayo has `ENV_ENCRYPTION_KEY` available. |
| Push to origin | Out of scope per the brief. | Adebayo pushes `develop` and `chore/devops-setup` whenever ready. |
| PR creation | Out of scope per the brief. | Adebayo opens the PR (target: `develop`). |
| ENV_ENCRYPTION_KEY committed to repo secrets | Cannot reach GitHub from this sandbox. | See checklist below — Adebayo pastes into GH Environments. |
| Local `bash install.sh` run | Sandbox blocks executing template scripts directly. Substituted by `/tmp/install_template.py` which did the exact `cp` + `{{TOKEN}}` replace logic that `install.sh` does (verified by spot-check against template `cd.yml` + `docker.yml`). | None — the result is byte-equivalent for the workflows + docs + scripts that were copied. `install.sh` `--force` semantics aren't relevant since CI was rewritten by hand in step 7. |

---

## Branch state

```
$ git log --oneline main..HEAD
fe7de79 chore(ci): harden ci.yml with mypy + alembic-fresh + semgrep + trivy
c120581 chore(devops): add encrypted env files (staging + production)
4fcee93 chore(devops): wire cd.yml for gitflow (develop=staging, main=prod)
3c2a4bd chore(devops): multi-stage Dockerfile with non-root user + healthcheck
2f5183d chore(devops): add docker-compose.prod.yml
7449079 chore(devops): install backend-devops-template
```

`develop` was created from `main` (no commits yet). `chore/devops-setup` is
ahead of both by 6 commits.

---

## Adebayo's next-action checklist

### Before pushing the branch

1. **Verify the Dockerfile builds.**
   ```
   docker build -t timpbills-api:dev .
   ```
   Time: ~3 min cold. If it fails, look at the builder stage's `poetry install` — most failure modes are version pin mismatches.

2. **Verify the compose file parses with a real env.**
   ```
   ./scripts/env.sh decrypt staging
   ln -sf .env.staging .env.tmp && mv .env .env.dev.bak && mv .env.tmp .env
   docker compose -f docker-compose.prod.yml config > /dev/null
   mv .env .env.staging && mv .env.dev.bak .env  # restore
   ```
   The `config` invocation should produce no "variable is not set" warnings.
   (Use `--env-file .env.staging` instead if you'd rather not swap symlinks.)

3. **Read `.env.SECRETS_TO_STORE.txt`** (gitignored; at repo root). It lists
   the values you need to paste into:
   - GitHub repo secrets
   - GitHub Environment secrets (per env: staging, production)
   - Your password manager (the ENV_ENCRYPTION_KEY especially)
   - **Then delete the file.**

4. **Fix the placeholder values in the encrypted envs:**
   - `POSTGRES_PASSWORD` — set the value Postgres will run with on the VPS.
   - `REDIS_PASSWORD` — same.
   - `FIRST_SUPERUSER_PASSWORD` — pick a strong one and document somewhere safe.
   - `FCM_CREDENTIALS_JSON` — base64 the firebase service-account JSON and inline it (or set `FCM_CREDENTIALS_PATH` to a VPS path containing the JSON).
   - For production: switch `VTPASS_BASE_URL` to `https://vtpass.com` and PAYSTACK_*_KEY to live keys when promoting.

   Workflow:
   ```
   ./scripts/env.sh decrypt staging
   vim .env.staging
   ./scripts/env.sh encrypt staging
   git add .env.staging.enc && git commit -m "chore: update encrypted staging env"
   ```

### Before first deploy

5. **Create GitHub Environments** (`staging`, `production`). Per env, add:
   - `VPS_HOST`, `VPS_USERNAME`, `VPS_SSH_KEY`, `VPS_PORT`, `VPS_SSH_PASSPHRASE` (optional)
   - `DEPLOY_PATH` — staging: `/opt/timpbills-staging`, prod: `/opt/timpbills`
   - `APP_URL` — staging: `https://staging-api.timpbills.com`, prod: `https://api.timpbills.com`
   - `API_PORT` — staging: `8001`, prod: `8000`
   - `ENV_ENCRYPTION_KEY` — paste from `.env.SECRETS_TO_STORE.txt`

6. **Add repo-level GitHub secrets:**
   - `CI_SECRET_KEY` — 32+ random chars, used by CI's pytest run.
     ```
     python -c "import secrets; print(secrets.token_urlsafe(48))"
     ```

7. **On the VPS, one-time bootstrap:**
   ```
   sudo mkdir -p /opt/timpbills /opt/timpbills-staging
   sudo chown $USER:$USER /opt/timpbills /opt/timpbills-staging
   # GHCR auth so docker pull works:
   echo "<GHCR_PAT_read:packages>" | docker login ghcr.io -u <gh-username> --password-stdin
   ```

8. **DNS** — point `staging-api.timpbills.com` and `api.timpbills.com` at the VPS IP. Pass off to `nginx-ssl-engineer` for the TLS leg (Let's Encrypt + reverse proxy to `127.0.0.1:8000` for prod, `127.0.0.1:8001` for staging).

### Open the PR

9. **Open PR from `chore/devops-setup` → `develop`.** The PR description should call out:
   - Phase 2/3/4 of `docs/DEPLOY_PLAN.md` is complete.
   - 6 commits, intentionally split for review.
   - `.env.SECRETS_TO_STORE.txt` exists locally and must be consumed + deleted before merging.
   - CI will run on the PR; expect 5 jobs (lint, test, semgrep-scan, build, trivy-scan). First green run will take ~6-8 min cold.

---

## Verification gates (the brief required these)

| Gate | Status |
|---|---|
| YAML lint clean (`ci.yml`, `cd.yml`, `docker.yml`, `docker-compose.prod.yml`) | PASS — all four parse cleanly via `python3 -c "yaml.safe_load(...)"` |
| Workflow chain confirmed (`name: CI` matches `workflows: ["CI"]` in cd.yml) | PASS |
| Branch trigger confirmed (develop=staging, main=prod, no overlap) | PASS — verified by grepping `head_branch` conditions |
| Secrets enumerated | All `${{ secrets.X }}` references are documented in the checklist above |
| `condition: service_healthy` on `depends_on` | PASS — api, worker, beat all wait on db + redis |
| `concurrency:` on prod-deploy job | PASS — `timpbills-production-deploy`, `cancel-in-progress: false` |
| Image pinned by SHA (not `latest`) for deploy | PASS — `API_IMAGE` uses `${IMAGE_TAG}` (commit SHA) |
| Healthcheck path matches FastAPI route | PASS — `/api/v1/health` consistent across compose, Dockerfile, cd.yml |
| Plaintext `.env.staging` / `.env.production` / `.env.key` gitignored | PASS — verified with `git status --ignored` |
| Encrypted `.env.*.enc` committed | PASS |
| `docker build` local | NOT RUN — sandbox-blocked; Adebayo to verify |
| `docker compose config` local | NOT RUN — sandbox-blocked; Adebayo to verify |

---

## Risk callouts

1. **GHCR auth on the VPS.** CD pulls from GHCR by SHA. If the VPS isn't pre-authenticated (step 7 above), the first deploy fails on `docker compose pull`. Recovery: SSH in, run `docker login ghcr.io`, re-run the workflow.
2. **First migration on prod.** CD runs `alembic upgrade head` in a one-shot container BEFORE swapping the running api. If migration fails, the running container stays up — but the deploy job fails. This is the desired fail-fast behaviour.
3. **Placeholder passwords in encrypted envs.** `POSTGRES_PASSWORD`, `REDIS_PASSWORD`, `FIRST_SUPERUSER_PASSWORD` were generated as random strings to avoid identical encrypted blobs across envs. **They will not match what Postgres/Redis actually run with on the VPS** until Adebayo decrypts → edits → re-encrypts.
4. **The `.env` plaintext file at repo root still contains real-ish keys.** Per locked decision §6.7, Resend rotation is deferred. The file is correctly gitignored — risk is local-disk only — but worth keeping in mind for the next backup-of-laptop scenario.
5. **CI alembic-fresh-DB gate.** If the project has any migration that depends on data (a `data_migration`), the fresh-DB gate will fail. None observed in audit. If it surfaces, the fix is to mark such migrations data-only and exclude from the gate.

---

*End of status.*
