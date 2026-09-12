# Admin: edit basic user info

## What shipped
Ops admins can edit a user's basic identity fields (full name, email, phone)
from the admin console user-profile page.

- **Backend:** `PATCH /api/v1/admin/users/{user_id}`
  (`app/api/v1/endpoints/admin.py`), backed by a new
  `AdminService.update_user(...)` in `app/services/admin_service.py`.
- Returns the SAME payload as `GET /admin/users/{id}` (via
  `AdminService.get_user_detail`) so the console can swap its user-detail
  state directly from the response.

## Why
The console could read a user's profile and resend a verification email, but
had no way to correct a mistyped name, email, or phone. Support had to fall
back on asking the user to self-edit, which is not always possible (e.g. the
user is locked out of an old email/phone). This closes that gap on the
trusted admin surface.

## How
- **Endpoint gating** mirrors `admin_trigger_refund` / `admin_resend_verification_email`:
  `dependencies=[Depends(require_admin), Depends(require_admin_csrf)]` plus an
  `admin` actor param. require_admin resolves before CSRF, so an
  unauthenticated PATCH is 401 `ADMIN_AUTH_REQUIRED`, not 403.
- **Request schema** — `app/schemas/admin_user_update.py::AdminUserUpdateRequest`,
  `extra="forbid"`, all fields optional (`full_name` 2..80, `email` `EmailStr`,
  `phone` free-form str). Handler uses `model_dump(exclude_unset=True)` for true
  PATCH semantics (absent key left untouched). Unlike the self-service
  `UserUpdateRequest` (/auth/me), this schema INTENTIONALLY permits email/phone
  because the admin is trusted (no OTP step).
- **Service behavior + key decisions:**
  | Field | Side effect |
  |---|---|
  | `full_name` | `.strip()`, set on user |
  | `email` | `normalize_email`, uniqueness check, set + `email_verified = False` |
  | `phone` | `normalize_to_e164`, uniqueness check, set + `is_phone_verified = False` + `tokens_revoked_at = now` + `token_store.revoke_all` |
  - Email/phone side effects mirror the user's own verified-change flows: a new
    email must be re-verified; a new phone clears phone-verification AND signs the
    user out of every session. The phone path reproduces the
    `AuthService.confirm_phone_change` side effects (token revocation) WITHOUT the
    OTP round-trip, because the admin is the trusted actor.
  - **Ordering:** DB `commit()` happens first, THEN `revoke_all` (a Redis side
    effect), so a rollback never kills tokens for an un-applied change. Same
    ordering as `confirm_phone_change`.
  - **`token_store` wiring:** injected in the handler via the existing
    `get_token_store` dep (which depends on `get_redis`) and passed into the
    service, so tests transparently get the fake Redis from `admin_ctx`.
- **Error mapping** (service raises stable `ValueError` codes, handler maps to HTTP
  via `_UPDATE_USER_ERROR_STATUS`): `NO_FIELDS` 400, `USER_NOT_FOUND` 404,
  `EMAIL_ALREADY_IN_USE` / `PHONE_ALREADY_IN_USE` 409, `INVALID_PHONE` 422.
  Pydantic errors (bad email shape, extra key, name too short) already surface as
  422 `VALIDATION_ERROR` via the global handler.
- **Audit log-line decision:** the handler/service emit one structured
  `log.info` (`app/core/logger.py`) capturing `actor.id`, `actor.email`, the target
  user id, and the LIST OF CHANGED FIELD NAMES ONLY (e.g. `["email","phone"]`).
  The new email/phone VALUES are deliberately NOT logged — PII stays out of the
  ops log stream. No `transaction_events` row (that audit surface is tx-scoped;
  this touches no transaction).

## What's involved
| Path | Change |
|---|---|
| `app/api/v1/endpoints/admin.py` | New `admin_update_user` PATCH handler + error-code maps; imports `get_token_store`, `TokenStore`, `AdminUserUpdateRequest` |
| `app/services/admin_service.py` | New async `update_user(...)`; imports `log`, `normalize_email`, `normalize_to_e164`, `InvalidPhoneFormat`; TYPE_CHECKING `AdminUser`, `TokenStore` |
| `app/schemas/admin_user_update.py` | New `AdminUserUpdateRequest` schema |
| `tests/api/test_admin_update_user.py` | New 10-case API test module |
| `docs/fe-integration-guide-admin-edit-user.md` | New FE contract (captured-live) |
| `docs/checklist/master-build-checklist.md` | New master checklist (created) |

No migration (no schema change — reuses existing `users` columns).

## Verification
- `tests/api/test_admin_update_user.py` (10): name change returns detail;
  email change flips `email_verified`; phone change flips `phone_verified` AND
  revokes tokens (asserts `tokens_revoked_at` set + refresh key cleared);
  duplicate email 409; duplicate phone 409; invalid phone 422; unknown 404;
  empty body 400 `NO_FIELDS`; missing CSRF 403; unauthenticated 401.
- CI repro (local, project toolchain):
  - `poetry run ruff check app tests` -> All checks passed.
  - `poetry run pytest -q` -> 1032 passed, 1 skipped, 1 xfailed, **3 failed**.
    The 3 failures (`tests/core/test_admin_config_defaults.py`,
    `tests/core/test_config_dojah.py`) are PRE-EXISTING local `.env` config drift,
    confirmed red on a clean stash BEFORE this change — unrelated to this work.
- FE-guide response bodies were captured live against the running app via the
  test client, not written from the schema (see the guide's verification table).

## Operate / roll back
- No config, migration, or infra change. Roll back by reverting the commit(s);
  nothing to undo in the DB or Redis.
- Operational note: a phone change signs the user out (all tokens revoked). This
  is intended — surface it in the console UI so the operator knows the user must
  re-login.

## Follow-ups
- No `transaction_events` audit row (non-tx action). If a general admin-action
  audit trail lands later, include name/email/phone edits (field names only).
- `EmailStr` rejects special-use TLDs (e.g. `.test`) with a 422 `VALIDATION_ERROR`
  BEFORE the service runs — documented in the FE guide as a validation trap.
