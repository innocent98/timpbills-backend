# Phone-only authentication + PIN-based cold-start login

**Released:** 2026-05-26 (backend)
**Spec:** [docs/superpowers/specs/2026-05-26-phone-only-auth-design.md](../superpowers/specs/2026-05-26-phone-only-auth-design.md)
**Plan:** [docs/superpowers/plans/2026-05-26-phone-only-auth.md](../superpowers/plans/2026-05-26-phone-only-auth.md)

## What changed

### Authentication contract

- `POST /auth/login` now accepts **phone only** (the `identifier` field is gone). Email-as-login is removed.
- Both **email and phone** must be verified before tokens are issued. Existing email-verified users get migrated through phone verification on their next login.
- Every user must set a 4-digit **PIN** before reaching the home screen. Existing users without a PIN get gated through a one-time set-PIN screen on next login.
- New endpoint: `POST /auth/pin-login` — cold-start authentication. Mobile passes the persisted refresh-token + PIN; server returns fresh access + rotated refresh tokens. Reuses the existing 5-attempts/30-min PIN lockout.
- New endpoint: `POST /auth/phone/verify` — public phone verification for signup + migration. Distinct from the existing authenticated `/auth/phone/verify-otp` (in-session Tier-1 upgrade).
- `POST /auth/pin/set` now requires a **scoped `pin_setup` JWT** (header: `X-Pin-Setup-Token`) issued by `/auth/email/verify`, `/auth/phone/verify`, or `/auth/login`. One-time use via Redis-backed jti blocklist.

### Response shapes

All auth endpoints that previously issued tokens now return a `next_action` discriminator:

| next_action | Meaning | Mobile screen |
|---|---|---|
| `verify_email_and_phone` | New register — both OTPs pending | Send to verification screen |
| `email_verification_required` | Phone verified, email pending | Email OTP screen |
| `phone_verification_required` | Email verified, phone pending. Inline OTP send if cooldown allows. | Phone OTP screen |
| `pin_setup_required` | Both verified, no PIN. `pin_setup_token` field carries the scoped JWT. | Set-PIN screen |
| `tokens_issued` | All gates passed. `tokens` field carries the access + refresh pair. | Home |

### Gate enforcement

- New dependency `require_full_auth_gates` rejects 403 `VERIFICATION_REQUIRED` (with `details.which: "email"|"phone"|"pin_setup"`) on every money endpoint when any gate fails.
- Gate enforcement is **soft by default** during rollout: `AUTH_STRICT_GATES=false` logs a warning and lets the request through. Flip to `true` after mobile adoption.

### Phone format

- All `User.phone` values are now stored as **E.164** (`+234XXXXXXXXXX`).
- One-time Alembic data migration `202605260900_normalize_phone_e164` normalises legacy rows.
- Inputs accepted at every endpoint: 11-digit local (`0801…`), 13-digit international (`234…`), and E.164.
- Bad formats → 400 `INVALID_PHONE_FORMAT`.

### SMS provider

- Termii OTP channel switched from `generic` → **`dnd`** (Do-Not-Disturb registry transactional channel). Essential for production OTP delivery on Nigerian carriers — `generic` is silently blocked for DND-registered numbers. Cost: ~₦5/SMS vs ~₦2.5 (worth it for delivery guarantee on auth flows).
- Non-OTP texts (notifications) stay on `generic`.
- New OTP body template: "Your Timpbills code is XXXXXX. It expires in 5 minutes. Do not share this code."

### Rate limits

- `/auth/pin-login` — 10/min per IP + per-user 5-attempts → 30-min freeze (shared with `/auth/pin/verify`).
- `/auth/pin/set` — 3/min per IP.
- `/auth/login` — 5/min per IP (existing).
- `/auth/phone/verify` — 5/min per IP.
- OTP **cooldown** — 60s between sends for the same `(user, purpose)` pair.
- OTP **daily cap** — 10 OTPs per user per day across all purposes (anti SMS-bombing).

## New settings

| Setting | Default | Purpose |
|---|---|---|
| `AUTH_STRICT_GATES` | `false` | Gate enforcement mode. Flip to `true` after ~80% mobile adoption of new flows. |
| `AUTH_PIN_LOGIN_ENABLED` | `true` | Kill switch for `/auth/pin-login`. Set to `false` to force fallback to phone+password. |
| `TERMII_OTP_CHANNEL` | `"dnd"` | Termii channel for OTP delivery. `"generic"` is the rollback option. |
| `OTP_RESEND_COOLDOWN_SECONDS` | `60` | Per-`(user, purpose)` cooldown between sends. |
| `OTP_RESEND_DAILY_CAP` | `10` | Per-user daily cap across all OTP purposes. |

## Migration

- Existing email-verified users with unverified phones get force-routed through phone OTP on their next login (inline send, subject to cooldown).
- Existing users without a PIN get force-routed through `/pin/set` after passing the verification gates.
- One Alembic data migration runs at deploy time to normalise existing `User.phone` to E.164.
- No new tables. Existing `User.email_verified`, `User.is_phone_verified`, `User.pin_hash` columns carry the gate state.

## Rollout sequence

1. **Backend deploy** with `AUTH_STRICT_GATES=false` (soft mode). Money endpoints continue to serve unmigrated users with a warning log.
2. **Mobile release** with new flows (M1–M7 — separate sprint).
3. **Flip strict mode** to `true` once ~80% of mobile users are on the new build. Unmigrated users hit 403 `VERIFICATION_REQUIRED` on money endpoints, mobile routes them to the appropriate screen using `details.which`.
4. **Cleanup** (later): remove `AUTH_STRICT_GATES` flag, remove any dead code paths (e.g. the `EMAIL_NOT_VERIFIED` error code is no longer raised).

## Rollback playbook

| Symptom | Action |
|---|---|
| OTP delivery rate drops sharply post-deploy | Flip `TERMII_OTP_CHANNEL=generic` (config-only, no redeploy). |
| `/auth/pin-login` 401 rate climbs anomalously | Set `AUTH_PIN_LOGIN_ENABLED=false`. Mobile falls back to password login. |
| Older mobile clients flooded with 403s after strict-mode flip | Set `AUTH_STRICT_GATES=false`. Returns to soft mode. |
| Mass registration drop | Investigate `/auth/register` → `/auth/email/verify` → `/auth/phone/verify` → `/auth/pin/set` funnel. Both OTP send paths and the daily cap are first suspects. |
| Phone normalisation regression | Re-run Alembic migration — it's idempotent. |

## Tests

- 65+ new tests across B1–B15 covering:
  - Phone normalisation (all formats + invalid inputs)
  - Alembic migration (idempotent + corrupt-row tolerance)
  - Scoped pin_setup JWT (round-trip, scope rejection, expiry, replay)
  - OTP cooldown + daily cap (purpose-scoped + cross-purpose)
  - `require_full_auth_gates` (soft + strict modes, all gate orderings)
  - Register → both OTPs → pin/set chain (both verification orderings)
  - `/auth/login` next_action branches + inline OTP send + cooldown
  - `/auth/pin-login` (happy path, replay defense, all error branches)
  - Money endpoint gating (strict 403 + soft pass + allowlist exclusions)
  - Existing-user migration (no phone, has PIN, tier-1 paths) + cold-start `/pin-login` round-trip
- Full suite: 703 passed, 1 xfailed (pre-existing, unrelated).

## Open follow-ups (next phase)

- Mobile work (M1–M7) — separate sprint covering phone-only login screen, phone OTP screen, set-PIN screen, cold-start PIN screen, `/me` state routing, etc.
- Async bill purchase (return 202 immediately, Celery worker calls VTPass) — separate planned project ([memory ref: project_async_bill_purchase.md](../../../../../.claude/projects/-Users-adebayovictor-Documents-mobile-timp/memory/project_async_bill_purchase.md)).
- Dead code cleanup: `EMAIL_NOT_VERIFIED` error code in `_ERROR_MAP` (no longer raised post-B12), the legacy `SetPinRequest` schema (B11 introduced `SetPinFirstTimeRequest`).
- Composite `(user_id, created_at)` index on `otp_codes` for the daily-cap query at scale.
- Consider sharing `_token_iat_predates_revocation` between `app/api/deps.py` and `pin_service.pin_login` (currently duplicated with minor divergence in missing-iat handling).
- `/auth/logout-everywhere` endpoint (Phase D consideration).
