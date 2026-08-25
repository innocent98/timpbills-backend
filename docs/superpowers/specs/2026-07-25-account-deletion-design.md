# Public Account Deletion — Design

**Date:** 2026-07-25
**Status:** Approved (pending spec review)
**Repos:** `timpbills-backend` (FastAPI), `timpbills-marketing` (Next.js App Router)

## 1. Problem & Goal

Google Play requires a way for users to request account deletion, reachable
without installing the app — a public web page plus a supporting API. The API
must authenticate with **email or phone + password only** (no bearer token, no
OTP).

Timpbills is a licensed fintech, so deletion cannot destroy transaction/AML
records. The resolution is **anonymize-and-retain**: personal data is erased,
the financial ledger survives with all identity detached.

### Success criteria
- A logged-out user can visit `https://timpbills.com/delete-account`, enter
  email-or-phone + password, and schedule deletion.
- Personal data is erased within 30 days; the ledger is retained anonymized.
- A non-zero wallet balance blocks the request until withdrawn.
- The flow is safe against accidental/malicious use (reversible 30-day window +
  notification).

## 2. What already exists (reused, not rebuilt)

| Capability | Location |
|---|---|
| Authenticated soft-delete `DELETE /users/me` (sets `is_active=false`, `deleted_at=now`, `tokens_revoked_at=now`, revokes tokens) | `app/api/v1/endpoints/users.py:184` |
| 30-day re-registration block keyed on `deleted_at` (`PHONE_RECENTLY_DELETED`/`EMAIL_RECENTLY_DELETED`) | `app/services/auth_service.py:253` |
| Password verify primitive `verify_password_async` | `app/core/security.py:58` |
| Email-or-phone OR-match lookup (as used by forgot/reset password) | `app/services/auth_service.py:1189` |
| Login rejects `is_active=false` → `ACCOUNT_DISABLED` | `app/services/auth_service.py:815`, `app/api/deps.py:234` |
| Admin "deleted" user filter | `app/services/admin_service.py:469` |
| Notification dispatch `dispatch_delay(...)` + `NotificationEvent` enum | `app/workers/tasks/notification_tasks.py:87`, `app/services/notification_service.py:48` |
| Success/error envelope + `_ERROR_MAP`/`_raise` pattern | `app/utils/responses.py:4`, `app/api/v1/endpoints/auth.py:83` |

**Missing (this project builds it):** a public no-token deletion endpoint; the
actual PII purge (deferred as "Sprint 8"); the non-zero-balance guard; the
marketing UI.

## 3. Architecture

One shared service method is the single place that starts a deletion. Both the
existing authenticated route and the new public route call it. A scheduled
sweep completes the erasure 30 days later.

```
Public page (marketing)  ──POST /account/deletion-request──┐
                                                           ▼
DELETE /users/me (existing, authenticated) ──►  AccountDeletionService.request_deletion(user)
                                                           │  sets deleted_at, is_active=false,
                                                           │  revokes tokens, sends notice
                                                           ▼
                                              (30-day grace; login blocked)
                                                           │
                          Celery beat ──► anonymize_deleted_accounts sweep
                                                           │  scrub PII, keep ledger,
                                                           ▼  stamp anonymized_at
                                                    (account anonymized)
```

### 3.1 Data model change

Add one nullable column to `users`:

- `anonymized_at TIMESTAMPTZ NULL` — set when the sweep has scrubbed PII.
  Distinguishes "soft-deleted, inside grace" (`deleted_at` set, `anonymized_at`
  null) from "anonymized" (both set).

Migration: `app/db/models/user.py` + a new Alembic revision (down_revision =
current head). Forward-only.

The 30-day window reuses the existing `deleted_at` column (already drives the
re-registration block), so grace-period timing and the re-registration block
stay in lockstep for free.

### 3.2 `AccountDeletionService`

New `app/services/account_deletion_service.py` (sync SQLAlchemy, matches repo
style). Methods:

**`request_deletion(*, user: User) -> datetime`**
1. If `user.deleted_at` is already set and `anonymized_at` is null → idempotent:
   return the existing `deleted_at + 30d` (no re-notify).
2. Load the wallet `FOR UPDATE`; if `balance > 0` → raise
   `ValueError("WALLET_NOT_EMPTY")` (endpoint maps to 409, message includes the
   naira amount).
3. Set `deleted_at=now`, `is_active=False`, `tokens_revoked_at=now`; commit.
4. `token_store.revoke_all(user_id)` (mirror `soft_delete_me`).
5. `dispatch_delay(event=account_deletion_requested, ...)` — email + SMS.
6. Return `deleted_at + timedelta(days=30)`.

**`resolve_and_verify(*, identifier: str, password: str) -> User`** (used by the
public endpoints)
- Normalize: if it looks like a phone, E.164-normalize; OR-match
  `User.email == identifier` OR `User.phone == normalized` (mirror
  `auth_service.py:1189`).
- If no user OR `verify_password_async` fails → raise
  `ValueError("INVALID_CREDENTIALS")` (single generic error — no enumeration).
- Return the user (allowed even when `is_active=false`, so a pending-deletion
  account can still cancel).

**`cancel_deletion(*, user: User) -> None`**
- If `anonymized_at` is set → raise `ValueError("ALREADY_ANONYMIZED")` (too late).
- Clear `deleted_at`, set `is_active=True`; commit. (Tokens stay revoked; the
  user logs in fresh.)

**Balance guard also applies to the existing authenticated `DELETE /users/me`.**
`soft_delete_me` is refactored to call `request_deletion`, so it gains the
`WALLET_NOT_EMPTY` guard and the notification. This is an intended behavior
change (no one should delete away their balance); existing tests that soft-delete
a user with a zero balance are unaffected.

### 3.3 Public endpoints

New `app/api/v1/endpoints/account.py`, `APIRouter(prefix="/account", tags=["account"])`,
included in `app/api/v1/api.py`. Public pattern (no `get_current_user`),
`request: Request`, slowapi rate limit.

**`POST /api/v1/account/deletion-request`** — `@limiter.limit("3/minute")`
- Body `AccountDeletionRequest { identifier: str, password: str }`.
- `resolve_and_verify` → `request_deletion` → `200 { scheduled_deletion_at }`.
- Errors: `INVALID_CREDENTIALS` (401), `WALLET_NOT_EMPTY` (409).

**`POST /api/v1/account/deletion-request/cancel`** — `@limiter.limit("3/minute")`
- Body `AccountDeletionRequest { identifier, password }`.
- `resolve_and_verify` → `cancel_deletion` → `200 { cancelled: true }`.
- Errors: `INVALID_CREDENTIALS` (401), `ALREADY_ANONYMIZED` (409).

Schemas in `app/schemas/account.py`. Error mapping via the endpoint's local
`_ERROR_MAP`/`_raise` (same shape as `auth.py:83`).

### 3.4 Anonymization sweep

New Celery beat task `anonymize_deleted_accounts` in
`app/workers/tasks/reconcile_tasks.py` (alongside the DVA sweep) or a new
`app/workers/tasks/account_tasks.py`. Runs daily.

Select `users` where `deleted_at <= now - 30d AND anonymized_at IS NULL`, and for
each (batched, `FOR UPDATE`):

**Scrub `users` (keep the row + id):**
- `email` → `f"deleted-{id}@deleted.invalid"` (unique, satisfies not-null)
- `phone` → deterministic non-colliding placeholder derived from id (unique,
  not-null); never a real-looking number
- `full_name` → `"Deleted User"`
- `password_hash` → a constant unusable value
- `pin_hash`, `date_of_birth`, `gender`, `address`, `avatar_url`,
  `referral_code` → null
- `anonymized_at` → now

**Delete PII child rows:** `push_tokens`, `otp_codes`. **Scrub** `kyc_records`
PII fields (or delete the rows — decided in the plan; the ledger does not depend
on them).

**Keep, untouched (now identity-detached):** `wallet` (balance is 0 by guard),
`transactions`, `virtual_accounts` (also null the `paystack_customer_code`),
`wallet_credit_keys`, `idempotency_keys`, `notification_logs` (already
`ON DELETE SET NULL`).

Idempotent: `anonymized_at IS NULL` filter means a re-run never re-processes.

**Consequence for re-registration:** after anonymization the email/phone become
placeholders, so the original email/phone no longer match a deleted row and the
person can sign up fresh. Inside the 30 days the original values still match and
re-registration stays blocked. Consistent and correct.

### 3.5 Notifications

Add `account_deletion_requested` to `NotificationEvent`
(`app/services/notification_service.py:48`), a `transaction_alerts`-category
entry, push/SMS copy, and an email template. Copy states the scheduled deletion
date and the one way to cancel: return to the deletion page and use "Cancel a
pending deletion" (normal login is blocked once the account is deactivated, so
the cancel endpoint is the only self-service reversal). No em/en dashes in any
of this user-facing copy.

## 4. Marketing UI (`timpbills-marketing`)

- Page: `app/(marketing)/delete-account/page.tsx` (inherits marketing
  header/footer via the route group).
- Client form: `app/components/DeleteAccountForm.tsx`, modeled on
  `ContactForm.tsx` but actually wired. Fields: identifier (email or phone),
  password, a required "I understand my account will be scheduled for deletion"
  checkbox, submit. A secondary "Cancel a pending deletion" action posts to the
  cancel endpoint.
- API base: new `NEXT_PUBLIC_API_BASE` env (public API root). A tiny fetch
  helper parses the `{ success, data, error }` envelope and surfaces
  `error.message`.
- States: idle / submitting / success (shows the scheduled date and points to
  the "Cancel a pending deletion" action as the way to reverse it) / error
  (invalid credentials; `WALLET_NOT_EMPTY` shows the balance and asks them to
  withdraw first).
- Copy contains no em/en dashes.

## 5. CORS / infra

Add the marketing production origin (e.g. `https://timpbills.com`, plus the
staging marketing origin) to `BACKEND_CORS_ORIGINS`
(`app/core/config.py:29`, currently localhost-only) in the staging and
production encrypted env, re-encrypted via `scripts/env.sh`.

## 6. Security posture

Public + password-only is, by the product decision, the accepted shape. Layers:
- slowapi rate limit (`3/minute`) per IP on both endpoints.
- Generic `INVALID_CREDENTIALS` — no account enumeration.
- Email + SMS notice on request, so a credential-theft victim is alerted.
- 30-day reversible grace is the real backstop; self-service cancel + support.
- CAPTCHA is noted as future hardening, out of scope here.

## 7. Testing

**Backend (TDD):**
- `resolve_and_verify`: email match, phone match, wrong password, unknown
  identifier → all bad paths return the same generic error; inactive
  (pending-deletion) user still resolves.
- `request_deletion`: happy path sets tombstone + returns +30d; non-zero balance
  → `WALLET_NOT_EMPTY`; idempotent second call; token revocation invoked;
  notification dispatched.
- `cancel_deletion`: reactivates before anonymization; `ALREADY_ANONYMIZED`
  after.
- Endpoints: 200 shapes, 401/409 error envelopes, rate-limit wiring.
- Sweep: a >30d soft-deleted user is anonymized (PII scrubbed, `anonymized_at`
  set, ledger rows intact, push/otp rows gone); a <30d user is untouched; re-run
  is a no-op; re-registration allowed post-anonymization, blocked during grace.
- Regression: existing `DELETE /users/me` still works with zero balance and now
  returns `WALLET_NOT_EMPTY` with a positive balance.

**Marketing:** component test / manual — happy path, invalid credentials,
`WALLET_NOT_EMPTY`, cancel path.

## 8. Out of scope

- Hard row deletion (blocked by `RESTRICT` FKs on wallet/transactions/
  virtual_accounts and by AML retention; anonymize-and-retain is the design).
- OTP/CAPTCHA on the deletion form.
- In-app (Flutter) deletion UI — the app already has authenticated
  `DELETE /users/me`; this project is the public web path.

## 9. Open decisions deferred to the plan

- `kyc_records`: scrub PII fields vs delete rows (either satisfies the goal).
- Exact daily beat schedule time.
- Phone/email placeholder format (must be unique, not-null, non-real).
