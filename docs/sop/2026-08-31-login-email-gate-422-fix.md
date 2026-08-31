# Email verification 422 — login email-gate carries no email/code

## What shipped (backend part)
`login()` now returns the user's `email` and sends a fresh email OTP inline when
the email gate is the first unverified one — so a returning, unverified user can
actually complete email verification. (Mobile part — threading the email into the
verify-email screen — ships from the `timpbills` repo separately.)

## Why
A returning email-unverified user logs in (phone + password) → `login()` returned
`next_action="email_verification_required"` but **no email** and **no fresh code**.
The mobile verify-email screen sources the address from a route query param and
only registration supplied it, so the login/detour paths sent `{"email":"","code":
"123456"}` → **422** (empty string fails `EmailStr`). Users were stranded: no email
to submit, and the original code likely expired.

## How
- `LoginResponse` (schemas/auth.py): added `email: str` (always populated) and
  `email_otp_sent: bool | None = None` (mirrors `phone_otp_sent`).
- `auth_service.login()`: every `LoginResponse(...)` return sets `email=user.email`;
  the email gate now calls a new `_mint_and_send_email_otp(user)` — symmetric with
  `_mint_and_send_phone_otp`: same `_check_otp_cooldown` guard, mints an
  `email_verification` OtpCode, sends via the email provider. `email_otp_sent` is
  `True` on send, `False` when cooldown/cap/error blocks it (a defensive
  `except` ensures a send-path error can never turn a valid login into a 500).
- Backward compatible: additive response fields; existing clients ignore them.

## Contract for the mobile client
`LoginResponse` JSON now includes `"email": "<user email>"` and
`"email_otp_sent": true|false|null`. The client must route to the verify-email
screen with this `email`, and read the code from the email the *login* triggered
(login mints a fresh, latest-first OTP; the register-time code may no longer be
newest).

## Verification
- New/updated tests: login email-gate returns `email` + `email_otp_sent=True` and
  queues an email; a rapid second login within cooldown returns `False` and queues
  nothing. Files: tests/services/test_auth_service_login.py,
  tests/api/test_login_next_action.py.
- Full backend suite: 1022 passed (3 unrelated stray-`.env` config failures pass in CI). Ruff clean.

## Operate / recover
- Immediate unblock for a stuck user (no deploy needed):
  `docker exec -e TARGET_EMAIL="…" -it timpbills_api python -c '…set email_verified=True…'`.
- The new admin "Resend verification email" action also works.

## Follow-ups
- Mobile: thread `?email=` through every route to the verify screen (login response
  for the login gate; `/auth/me` for set-pin / post-auth / phone-verify detours) +
  a `/auth/me` fallback in the verify page so it never submits an empty email.
