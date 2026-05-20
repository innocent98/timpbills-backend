# syntax=docker/dockerfile:1.7
# ---------------------------------------------------------------------------
# Timpbills API — production image.
#
# Multi-stage:
#   builder  — Poetry + build deps (curl, gcc, libpq-dev) → installs runtime
#              wheels into /opt/venv. Discarded after copy.
#   runtime  — slim base, libpq5 only (no compiler), non-root `appuser`,
#              gunicorn entrypoint with WEB_CONCURRENCY tunable.
#
# Alpine intentionally avoided: musl forces source-compile of psycopg2 /
# bcrypt / cryptography, producing larger and slower images.
# ---------------------------------------------------------------------------

ARG PYTHON_VERSION=3.11

# ----- builder -------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    POETRY_VERSION=1.8.4 \
    POETRY_HOME="/opt/poetry" \
    POETRY_VIRTUALENVS_CREATE=true \
    POETRY_VIRTUALENVS_IN_PROJECT=false \
    POETRY_NO_INTERACTION=1 \
    VENV_PATH="/opt/venv"

# Build-time deps. curl pulled here for the Poetry installer; libpq-dev for
# psycopg2 (build-time only — runtime uses libpq5).
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        build-essential \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

RUN curl -sSL https://install.python-poetry.org | python3 - --version "${POETRY_VERSION}" \
 && ln -s /opt/poetry/bin/poetry /usr/local/bin/poetry

WORKDIR /app

# Layer-cache: copy lockfiles first, install, then copy source.
COPY pyproject.toml poetry.lock* ./

RUN python -m venv "$VENV_PATH" \
 && . "$VENV_PATH/bin/activate" \
 && poetry install --no-interaction --no-ansi --only main --no-root

# ----- runtime -------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS runtime

ARG GIT_SHA=unknown
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    APP_GIT_SHA=${GIT_SHA} \
    WEB_CONCURRENCY=4

# Runtime-only system deps. libpq5 supplies the Postgres client lib that
# psycopg2 dynamically links against. No compiler, no curl — keeps the
# CVE surface and image size down.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 1000 appuser \
    && useradd  --system --uid 1000 --gid appuser --home-dir /app --no-create-home appuser

WORKDIR /app

# Pull the prebuilt venv from the builder stage.
COPY --from=builder /opt/venv /opt/venv

# App code (only — tests, htmlcov, docs, .env stay out via .dockerignore).
COPY --chown=appuser:appuser ./app /app/app
COPY --chown=appuser:appuser ./alembic /app/alembic
COPY --chown=appuser:appuser ./alembic.ini /app/alembic.ini

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
