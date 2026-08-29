# Admin: resend verification email

## What shipped
Ops admins can trigger a verification-email resend for a user whose email is
unverified, from the admin console user-profile page.

- **Backend:** `POST /api/v1/admin/users/{user_id}/resend-verification-email`
  (`app/api/v1/endpoints/admin.py`).
- **Frontend (marketing `/platform-admin`):** a "Resend verification email" button
  on the Verification card, shown only when email is unverified.

## Why
No way for support to re-trigger the verification email for a stuck user — they'd
have had to ask the user to hit the app's own resend, which isn't always possible.

## How
- Endpoint gating mirrors `admin_trigger_refund`:
  `dependencies=[Depends(require_admin), Depends(require_admin_csrf)]` + an
  `admin` actor param. Injects `db` and reuses `AuthService.send_email_otp` via the
  existing `get_auth_service` dep (no new DI, no service changes).
- Behavior:
  | Case | Response |
  |---|---|
  | Unverified user | 200 `{success:true, data:{sent:true, email}}` + OTP email queued |
  | Already verified | 200 `{success:true, data:{sent:false, reason:"already_verified", email}}` |
  | Unknown / malformed id | 404 `USER_NOT_FOUND` |
- Trap handled: `User.id` is `UUID(as_uuid=True)`, so the `str` path param is coerced
  via `uuid.UUID(user_id)` (matching `AdminService.get_user_detail`); malformed/unknown
  → clean 404, not a 500. `send_email_otp`'s own guards are caught defensively (race
  where the user verifies mid-call → the same `already_verified` shape).
- Frontend reuses the console's existing admin `call()` wrapper — `credentials:
  "include"` (admin_session cookie) + `X-CSRF-Token` from the `admin_csrf` cookie
  (double-submit), identical to the refund/requery actions. Button has
  loading / success / already-verified / error states and hides once verified.

## Verification
- `tests/api/test_admin_resend_verification.py` (3): unverified → sent + email
  queued; already-verified → sent:false, no email; unknown id → 404.
- Full backend suite: 1019 passed. Ruff clean.
- Frontend: `tsc --noEmit` + eslint clean on the 3 changed files.

## Follow-ups
- No audit row (resending an email isn't a transaction; `transaction_events` is
  tx-scoped). If a general admin-action audit trail is added later, include this.
