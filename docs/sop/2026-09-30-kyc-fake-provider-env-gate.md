# Gate the KYC fake provider to dev/test (KYC bypass fix) + constant-time phone-change OTP

## What shipped
1. The Dojah KYC factory now refuses the approve-everything `FakeKycProvider`
   outside dev/test. In staging/production, `FORCE_FAKE_PROVIDERS=true` or a
   missing/blank `DOJAH_API_KEY` stops the app from **booting** (Settings
   validation) and, as a backstop, raises `FakeKycInEligibleEnvError` from the
   factory.
2. The phone-change confirm step compares the OTP with `hmac.compare_digest`
   instead of `==`.

Commit: see `git log -- app/integrations/dojah/factory.py` (branch
`claude/suspicious-swartz-4428a7`).

## Why
`get_kyc_provider()` returned `FakeKycProvider()` whenever
`FORCE_FAKE_PROVIDERS` was true **or** `DOJAH_API_KEY is None`, in any
environment. The fake returns SUCCESS for any reference without a
FAILFACE/FAILLIVE/PENDING marker. So in production, a missing or unrendered
`DOJAH_API_KEY` secret silently approved every KYC submission: tier_1 -> tier_2
(BVN) and tier_2 -> tier_3 (NIN, unlimited wallet caps). A KYC/AML bypass with no
error or log signal.

This was documented as intentional ("spec decision #2": real Dojah is the working
path everywhere, so no env gate). That decision is **reversed**: a fallback that
approves identity checks is never a safe default, unlike e.g. a fake SMS sender.

The `is None` check also missed the common "secret templated but empty" case:
`DOJAH_API_KEY=` arrives as `""`, which previously selected the real client with
no key.

Phone-change OTP: `==` on strings short-circuits on the first differing
character, so response timing can in principle leak how many leading digits
matched. Low severity (6 digits, 10-min TTL, endpoint limited to 5/min), but the
fix is free.

## How
- **Same rule as Termii / Paystack / VTPass / email factories.** Fake allowed
  only when `ENVIRONMENT` (stripped, lowercased) is one of
  `dev, development, test, testing, local`. Outside that set:
  `FORCE_FAKE_PROVIDERS=true` -> error; falsy `DOJAH_API_KEY` -> error. Inside it,
  behaviour is unchanged (forced or keyless -> fake).
- **Two layers.**
  - `Settings._refuse_fake_kyc_outside_dev` (`model_validator(mode="after")`) raises at
    settings load. Because API, Celery workers and `alembic` all import
    `settings`, a misconfigured deploy fails at container start, before it
    serves traffic.
  - The factory check is the runtime backstop, for code paths that mutate
    `settings` after load (tests, or a future hot-reload).
- **`FAKE_ELIGIBLE_ENVS` now lives in `app/core/config.py`**, shared by the
  validator and the Dojah factory. The other factories still keep their own
  identical copy; consolidating them was left out of scope.
- The startup validator also rejects `FORCE_FAKE_PROVIDERS=true` outside dev for
  **all** providers. Every provider factory already raised on that at first call,
  so this only moves the failure earlier. No valid config is newly rejected.
- OTP: `hmac.compare_digest(otp.encode(), expected_otp.encode())`. Bytes are used
  because `compare_digest` raises `TypeError` on non-ASCII `str`. The schema
  restricts to digits, but the service is also callable directly.

Rejected: hashing the phone-change OTP at rest in Redis. It's a worthwhile
hardening, but it changes the stored payload format, and in-flight requests
would fail across a deploy. Not needed to close the timing issue.

## What's involved
| File | Change |
|---|---|
| `app/core/config.py` | `FAKE_ELIGIBLE_ENVS` constant; `_refuse_fake_kyc_outside_dev` model validator; Dojah comment updated |
| `app/integrations/dojah/factory.py` | Env-allowlist gate, `FakeKycInEligibleEnvError`, docstring records the spec-decision reversal |
| `app/services/auth_service.py` | `import hmac`; constant-time compare in `confirm_phone_change` |
| `pyproject.toml` | `sqlalchemy = ">=2.0.41,<2.1"` — keeps CI on the psycopg2 driver |
| `.env.example` | Dojah block: key is REQUIRED outside dev/test |
| `tests/integrations/dojah/test_factory.py` | Rewritten: eligible envs -> fake; prod/staging/preview + missing/blank key -> raises; prod + forced -> raises; key present -> `DojahClient` |
| `tests/core/test_config_kyc_fail_fast.py` | New: Settings-load fail-fast cases |
| `tests/api/test_phone_change_flow.py` | New: spy asserts confirm routes through `hmac.compare_digest` |

No API shape change: `INVALID_OTP` etc. are unchanged, so no FE guide update is needed.

## Verification
TDD: the new tests were written first and failed for the expected reasons
(ImportError / DID NOT RAISE / no `hmac` attribute), then passed after the change.

Local reproduction of every `ci.yml` job (Python 3.11, project `.venv`):
- `poetry run ruff check app tests` -> All checks passed.
- `ruff format --check` (advisory in CI) -> new files formatted; 243 pre-existing files already fail repo-wide.
- `mypy app` (advisory) -> no new errors in touched code.
- `alembic upgrade head` on fresh Postgres 15 -> all migrations applied.
- Full `pytest --cov` with CI's env -> **1066 passed, 1 skipped, 1 xfailed**.
- `scripts/security-scan.sh` (semgrep) -> 0 blocking findings.

## Operate / roll back
- **Deploy prerequisite:** staging and production env files **must** set a
  non-empty `DOJAH_API_KEY` and must not set `FORCE_FAKE_PROVIDERS=true`.
  Otherwise the API, worker, beat **and the migration step** exit at startup with
  a `ValidationError` naming the variable. That failure is intended, but check
  the encrypted `.env.staging` / `.env.production` before deploying.
- Roll back: revert the commit. This restores the silent-fake behaviour, so only
  do it as a stopgap while the secret is fixed.

## Follow-ups
- [ ] Apply the same "missing key -> fail at boot" validator to `TERMII_API_KEY`
      and `RESEND_API_KEY`. Their factories already raise at first call, but not at startup.
- [ ] Consolidate the five per-factory `_FAKE_ELIGIBLE_ENVS` copies onto
      `app.core.config.FAKE_ELIGIBLE_ENVS`.
- [ ] Optionally store the phone-change OTP hashed in Redis (needs a
      payload-format migration window).
- [x] **CI latent break (fixed in this PR):** `poetry.lock` is gitignored, so CI
      re-resolves on every run; SQLAlchemy 2.1.x makes `postgresql://` default to
      psycopg 3 (not a dependency) -> alembic/pytest fail with
      `No module named 'psycopg'`. Pinned `sqlalchemy = ">=2.0.41,<2.1"` in
      `pyproject.toml` (resolves 2.0.54); alembic + full suite verified green with
      the plain `postgresql://` URL CI uses. Longer-term: commit `poetry.lock`.
