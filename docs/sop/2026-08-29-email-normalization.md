# Email normalization (case-insensitive) across user auth

## What shipped
User email is now normalized case-insensitively everywhere — boundary + service
+ DB — so `Example@mail.com` and `example@mail.com` are the same account. Mirrors
the existing E.164 phone normalization.

## Why
`EmailStr` never lowercased, register stored `req.email` raw, lookups compared
raw, and `users.email` was a plain **case-sensitive** `String` unique index. So a
user could register twice with different casing, and a cross-case login / email
verification / password reset silently failed to find the account.

## How
- **Boundary** — `app/utils/email.py::normalize_email()` (`strip().lower()`), applied
  via pydantic `field_validator` to every inbound email field in `schemas/auth.py`
  (RegisterRequest, SendEmailOtpRequest, VerifyEmailOtpRequest) and to the email
  arm (`"@" in v`) of ForgotPasswordRequest / ResetPasswordRequest identifiers.
- **Service defense-in-depth** — normalize at the lookup/store boundaries in
  `auth_service.py` (register uniqueness + stored value, send_email_otp,
  verify_email_otp, forgot/reset email arm) and `account_deletion_service.py`.
- **DB** — migration `202608291200_normalize_user_email_citext` (Postgres-only):
  `CREATE EXTENSION citext`, a **guard** that raises listing any case-insensitive
  duplicates (ops merges first — never drops data), `UPDATE users SET email =
  lower(email)`, then `ALTER COLUMN email TYPE citext` (recreates the unique index
  case-insensitively). No-op on SQLite (boundary normalization covers tests). The
  SQLAlchemy model column stays `String` for SQLite portability.

## Verification
- 12 tests (`tests/utils/test_email_normalize.py`, `tests/api/test_email_case_insensitive.py`):
  normalize idempotence; duplicate cross-case register → 409 USER_ALREADY_EXISTS;
  verify / resend / forgot-password resolve under any input casing.
- Full backend suite: 1019 passed (3 unrelated stray-`.env` config failures pass in CI). Ruff clean.
- Migration not run against Postgres locally (no PG in dev) — verified by review + module load.

## Operate / roll back
- **Before prod cutover**, pre-check for duplicates so the deploy's `alembic
  upgrade head` doesn't fail on the guard:
  ```sql
  SELECT lower(email), count(*) FROM users GROUP BY lower(email) HAVING count(*) > 1;
  ```
  If rows return, merge those accounts first. If the guard does fire during deploy,
  it fails at the migration step **before** `up -d`, so the running version stays up.
- Rollback: `downgrade` reverts the column to varchar (lowercased values kept).

## Follow-ups
- Existing OtpCode.email (denormalized copy) is written from the now-normalized
  user.email, so it inherits normalization going forward; no backfill needed.
