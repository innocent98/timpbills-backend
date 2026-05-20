# Pre-Deploy Guide — Timpbills API

Run this sequence locally before pushing to `main`. If it passes, push with confidence. If it fails, fix locally — don't discover it on staging.

## TL;DR — the checklist

```bash
# 1. Rebuild the image with your latest code
docker compose build api

# 2. Restart api so it uses the new image
docker compose up -d api

# 3. Apply migrations and seed (same commands CD will run)
docker compose exec api alembic upgrade head
docker compose exec api python /app/scripts/seed_data.py  # skip if no seed script

# 4. Run migrations + seed AGAIN (idempotency check — CD re-runs on every deploy)
docker compose exec api alembic upgrade head
docker compose exec api python /app/scripts/seed_data.py

# 5. Smoke test the health endpoint
curl -s http://localhost:8000/api/v1/health

# 6. Run the full test suite
poetry run pytest tests/ -q
```

If all six pass, push. If any fail, fix locally first.

## Why "run step 3 twice"?

CD runs migrations and seeds on **every deploy**, not just the first one. If your seed script crashes when a record already exists, your deploy will fail on the second push to the same environment. Idempotency is non-negotiable for CD.

Test for it: run the migration + seed, then run them again. Both must succeed.

## Common pitfalls

### 1. Local container running stale code

After editing source files you must `docker compose build api && docker compose up -d api` before testing. Containers don't hot-reload from mounted volumes unless you've set up a volume mount for `app/` (production uses `COPY`, not volumes).

### 2. Empty tables hide bugs

Local DB tests pass; staging tests fail. Almost always this is because local tables were empty. Seed locally (`python scripts/seed_data.py` or similar) before testing to reproduce staging state.

### 3. `.env.<env>.enc` out of sync

If you edit `.env.staging` locally but forget to re-encrypt to `.env.staging.enc`, CD ships the old env. Re-encrypt:

```bash
ENV_ENCRYPTION_KEY='<passphrase>' ./scripts/encrypt-env.sh staging
git add .env.staging.enc
git commit -m "chore: bump staging env"
```

### 4. `workflow_run` uses the default branch's `cd.yml`

Changes to `.github/workflows/cd.yml` on `develop` don't take effect for auto-triggered staging deploys until merged to `main`. For early testing of CD changes, use **Actions → Run workflow** (workflow_dispatch) on the feature branch — that uses the branch's current CD file.

### 5. Migrations that add NOT NULL without a default

Adding a NOT NULL column to a non-empty table fails unless:
- the column has a server default, OR
- the column allows NULL, you backfill, then tighten to NOT NULL in a later migration.

The latter is a **two-phase migration**. Do the phases in separate alembic revisions, and backfill data between them. (Luran's original CD did this for encryption rollout — the template collapses to a single `alembic upgrade head` because most projects don't need it. Re-introduce the pattern if you have one.)

### 6. Secrets referenced but not set

CI job passes locally but fails on GitHub with `KeyError` or `AttributeError` on config load. Check that every secret referenced by name in the workflow YAML (e.g. `${{ secrets.CI_OPENAI_API_KEY }}`) is actually configured in GitHub. Missing secrets resolve to empty strings — often silently until the app tries to use them.

## Golden local smoke test

This is the smallest meaningful end-to-end check — a login that exercises the DB, crypto, and request pipeline in one call:

```bash
curl -s -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  --data-raw '{"email":"test@example.com","password":"TestPass123!"}' \
  | head -c 120
# Should start with {"access_token":"..."
```

Adapt the endpoint/body to your auth flow. If this works, most of the rest is plumbing.
