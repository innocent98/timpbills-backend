# syntax=docker/dockerfile:1.7
# Timpbills API — single Dockerfile, multi-stage.
# Targets:
#   dev      uvicorn --reload, all deps (main + dev) baked in, root user.
#            Selected by docker-compose.yml via `target: dev`.
#   runtime  gunicorn, prod-only deps, non-root appuser. DEFAULT (last stage),
#            so `docker build .` produces the prod image. Production compose
#            pulls this from the registry — no inline build at deploy.
# Alpine avoided: musl breaks psycopg2/bcrypt/cryptography wheels.

ARG PYTHON_VERSION=3.11
ARG POETRY_VERSION=2.2.1


# ───── builder-base ────────────────────────────────────────────────────────
# Shared layer: system build deps + Poetry + lockfile copy. Both dev and
# builder-prod start from here so the Poetry install + lockfile resolution
# layer is cached across both targets.
FROM python:${PYTHON_VERSION}-slim AS builder-base

ARG POETRY_VERSION
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    POETRY_HOME="/opt/poetry" \
    POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_CREATE=true \
    POETRY_VIRTUALENVS_IN_PROJECT=false \
    VENV_PATH="/opt/venv"

# Some Nigerian ISPs (Fastly route) drop the path on deb.debian.org's
# HTTP→HTTPS 307 — force HTTPS sources directly to bypass the redirect.
RUN { [ -f /etc/apt/sources.list.d/debian.sources ] && sed -i 's|http://|https://|g' /etc/apt/sources.list.d/debian.sources; \
      [ -f /etc/apt/sources.list ] && sed -i 's|http://|https://|g' /etc/apt/sources.list; \
      true; } \
 && apt-get update && apt-get install -y --no-install-recommends \
        curl \
        build-essential \
        libpq-dev \
 && rm -rf /var/lib/apt/lists/*

RUN curl -sSL https://install.python-poetry.org | python3 - --version "${POETRY_VERSION}" \
 && ln -s /opt/poetry/bin/poetry /usr/local/bin/poetry

WORKDIR /app

COPY pyproject.toml poetry.lock* ./


# ───── dev ─────────────────────────────────────────────────────────────────
# Hot-reload dev image. Installs all deps (main + dev) into /opt/venv and
# keeps Poetry available so devs can `poetry add` inside the container.
# App source is NOT baked in — docker-compose.yml bind-mounts ./app, ./alembic,
# ./scripts, ./tests, ./pyproject.toml over /app at runtime.
FROM builder-base AS dev

RUN python -m venv "$VENV_PATH" \
 && . "$VENV_PATH/bin/activate" \
 && poetry install --no-interaction --no-ansi --no-root

ENV PATH="/opt/venv/bin:$PATH"
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]


# ───── builder-prod ────────────────────────────────────────────────────────
# Separate from dev so prod can't accidentally inherit dev deps. Same Poetry,
# same lockfile, different install group (--only main).
FROM builder-base AS builder-prod

RUN python -m venv "$VENV_PATH" \
 && . "$VENV_PATH/bin/activate" \
 && poetry install --no-interaction --no-ansi --only main --no-root


# ───── runtime (default, last stage) ───────────────────────────────────────
FROM python:${PYTHON_VERSION}-slim AS runtime

ARG GIT_SHA=unknown
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    APP_GIT_SHA=${GIT_SHA} \
    WEB_CONCURRENCY=4

# Runtime-only system deps. libpq5 supplies the Postgres client lib that
# psycopg2 dynamically links against. No compiler, no curl, no Poetry.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 1000 appuser \
 && useradd  --system --uid 1000 --gid appuser --home-dir /app --no-create-home appuser

WORKDIR /app

# Prebuilt main-only venv from builder-prod.
COPY --from=builder-prod /opt/venv /opt/venv

# App code (only — tests, htmlcov, docs, .env stay out via .dockerignore).
COPY --chown=appuser:appuser ./app /app/app
COPY --chown=appuser:appuser ./alembic /app/alembic
COPY --chown=appuser:appuser ./alembic.ini /app/alembic.ini
# In-container Python management scripts only (create_admin, seed_dev_user,
# reset_kyc) so they can be run via `docker compose exec api python
# scripts/<name>.py`. Host-side shell scripts (backups, env encryption,
# security scan) are intentionally left out of the image.
COPY --chown=appuser:appuser ./scripts/*.py /app/scripts/

# Pre-create writable runtime dirs owned by appuser. /app itself is
# root-owned (from the base image), so the non-root appuser can't create
# subdirs at runtime — loguru's FileSink would crash on first write to
# /app/logs/app.log. /app/secrets/ is the volume mount target for the
# Firebase admin SDK JSON (see docker-compose.prod.yml).
RUN mkdir -p /app/logs /app/secrets \
 && chown -R appuser:appuser /app/logs /app/secrets

USER appuser

EXPOSE 8000

# Belt-and-suspenders alongside docker-compose healthcheck. Uses stdlib
# urllib so we don't ship curl into the runtime image.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import urllib.request,sys; \
    sys.exit(0 if urllib.request.urlopen('http://localhost:8000/api/v1/health', timeout=4).status==200 else 1)"

# Gunicorn + uvicorn workers. Tune via WEB_CONCURRENCY env. Graceful
# shutdown gives in-flight requests 30s to drain on SIGTERM.
CMD ["sh", "-c", "exec gunicorn app.main:app \
  -k uvicorn.workers.UvicornWorker \
  -w ${WEB_CONCURRENCY:-4} \
  -b 0.0.0.0:8000 \
  --graceful-timeout 30 \
  --timeout 60 \
  --access-logfile -"]
