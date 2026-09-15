# Disable API docs (Swagger / ReDoc / OpenAPI) on staging and production

## What shipped
The interactive API docs and the OpenAPI schema are now served only in local
development (and tests). On the deployed staging and production servers,
`/api/v1/docs`, `/api/v1/redoc`, and `/api/v1/openapi.json` return 404.

## Why
The Swagger UI, ReDoc, and the raw OpenAPI schema exposed the full API surface
publicly on staging and production. They are a development aid, not a public
feature, so they should be reachable only on a local dev machine.

## How
Added `Settings.docs_enabled` (app/core/config.py): true unless
`ENVIRONMENT` is `staging` or `production` (case/space-insensitive). Local
default is `development`, so docs stay on locally and in tests; `.env.staging`
sets `staging` and `.env.production` sets `production`, so docs go off there.

In `app/main.py`, the `FastAPI(...)` `openapi_url` / `docs_url` / `redoc_url`
are set to their paths when `settings.docs_enabled` else `None`. Setting
`openapi_url=None` also removes the schema the UIs depend on, so the whole
surface is gone (not just the HTML). The `/` root endpoint only advertises the
docs link when docs are enabled.

Gate chosen as "off iff staging/production" (rather than "on iff development")
so any other value (local, test, unset) fails open to docs-on, matching the
requirement to disable specifically on the two deployed servers.

## What's involved
- `app/core/config.py` - new `docs_enabled` property.
- `app/main.py` - gate the three doc URLs + the root docs link on it.
- `tests/core/test_docs_gate.py` - property is False only for staging/production;
  app doc routes track `docs_enabled`.

## Verification
- `poetry run ruff check app tests` -> pass.
- `poetry run pytest tests/core/test_docs_gate.py` -> 2 passed.
- `ENVIRONMENT=production python -c "... settings.docs_enabled"` -> `False`.
- Post-deploy: `GET https://staging-api.timpbills.com/api/v1/docs` and
  `/openapi.json` -> 404; same on production; local dev still serves them.

## Operate / roll back
Revert the two source edits (docs return to being served everywhere). To
temporarily re-enable docs on a deployed box, set `ENVIRONMENT` to a
non-staging/production value (not recommended for prod).

## Follow-ups
None.
