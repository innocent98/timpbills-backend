# Phone-only authentication + PIN-based cold-start login — design

**Status:** approved (brainstorm 2026-05-26)
**Owner:** Adebayo
**Scope:** Phase A (phone-only auth + phone verification) + Phase B (PIN-based subsequent login). Phase C (biometric) is out of scope — biometric unlocks the saved credentials that PIN currently unlocks; it's mobile-only work for a later sprint.

## 1. Summary

Replace the current "email or phone + password" login model with a phone-only login model, add mandatory phone verification at registration alongside the existing email verification, force every user to set a 4-digit PIN, and add a new `POST /auth/pin-login` endpoint that mobile uses on app cold-start (`{refresh_token, pin}` → fresh access+refresh tokens). Pattern matches PiggyVest / PalmPay / Kuda.

## 2. Goals

- One identifier (phone) at every login surface.
- Both email *and* phone verified before any tokens are issued.
- PIN mandatory for every user (forced setup post-verification).
- Cold-start login uses PIN against the device's stored refresh_token — no password re-entry on every app open.
- All existing users force-migrated through phone-verify + PIN-setup on their next login.
- No new database tables for Phase A+B.

## 3. Non-goals

- Server-side "trusted devices" table with device-ID binding (Phase D feature; current refresh-token rotation is the trust signal).
- Device limit per user (Phase D).
- Biometric unlock (Phase C, mobile-only).
- Fallback SMS provider (Phase D).
- `/auth/logout-everywhere` endpoint (Phase D).
- Termii-managed OTP (vendor lock-in; we keep owning OTP lifecycle).

## 4. Architecture

### 4.1 Auth gates

Tokens are issued only when **all three** are true:

1. `User.email_verified == True`
2. `User.is_phone_verified == True`
3. `User.pin_hash IS NOT NULL`

Any false gate → no tokens. The response carries a `next_action` field telling mobile which screen to route to:
`tokens_issued` | `email_verification_required` | `phone_verification_required` | `pin_setup_required`.

A server-side dependency `require_full_auth_gates` wraps `get_current_user` and rejects (`403 VERIFICATION_REQUIRED`) any protected endpoint hit while gates fail. Allowlist (endpoints that work pre-gate-pass):

- `/auth/me`
- `/auth/phone/send-otp`, `/auth/phone/verify`, `/auth/phone/resend`
- `/auth/email/verify`, `/auth/email/resend`
- `/auth/pin/set`
- `/auth/logout`

All money / bill / wallet / transaction endpoints are blocked until all three gates pass.

### 4.2 User entity changes

No schema migration. The columns already exist:

- `User.email` — unchanged
- `User.email_verified` — unchanged (verified at registration)
- `User.phone` — column unchanged; **stored format normalised to E.164** going forward (one-time Alembic data migration)
- `User.is_phone_verified` — now required True before tokens issue
- `User.pin_hash` — stays nullable at column level (deletion-recovery window) but gates refuse tokens when null

### 4.3 Endpoint topology

| Endpoint | Method | Purpose | Auth |
|---|---|---|---|
| `/auth/register` | POST | Create user; send email OTP + phone OTP | none |
| `/auth/email/verify` | POST | Verify email OTP | none |
| `/auth/email/resend` | POST | Re-send email OTP | none |
| `/auth/phone/verify` | POST | Verify phone OTP (new path during signup; same as today during change-phone) | none for signup, authed for change-phone |
| `/auth/phone/resend` | POST | Re-send phone OTP | none |
| `/auth/pin/set` | POST | Set first PIN, issue full tokens | scoped `pin_setup` JWT (see §6) |
| `/auth/login` | POST | Phone + password → tokens iff all gates pass, else `next_action` | none |
| **`/auth/pin-login`** | POST | `{refresh_token, pin}` → fresh access+refresh | none (the refresh_token + pin are the credentials) |
| `/auth/refresh` | POST | Refresh-token rotation | none |
| `/auth/logout` | POST | Revoke refresh-token + jti blocklist | authed |
| `/auth/pin/change` | POST | Rotate PIN | authed + step-up PIN |
| `/auth/password/change` | POST | Rotate password | authed |
| `/auth/password/forgot` | POST | Send phone OTP for reset | none |
| `/auth/password/reset` | POST | Phone OTP + new password | none |

### 4.4 Approach choice (recorded)

Approach A (incremental — same paths, additive endpoints) was chosen over namespace versioning or single-endpoint discrimination. Rationale: smallest churn for a small user base (~34 backfilled at design time), independent rate-limiting per path, clearer code paths.

## 5. Phone format normalisation

Single helper `app/utils/phone.py::normalize_to_e164(raw: str) -> str`. Accepted at input:

| Input | Output |
|---|---|
| `08012345678` | `+2348012345678` |
| `2348012345678` | `+2348012345678` |
| `+2348012345678` | `+2348012345678` |
| Anything else | raises → `400 INVALID_PHONE_FORMAT` |

All endpoints normalise before lookup. The User.phone column is migrated to E.164 once via Alembic (§13).

## 6. Scoped `pin_setup` JWT

A short-lived (10 min) JWT with claim `scope: pin_setup` and a fresh `jti`. Issued by whichever endpoint completes the email-AND-phone-verified pair *while pin_hash is still null*:

- By `/auth/email/verify` if phone was already verified
- By `/auth/phone/verify` if email was already verified
- By `/auth/login` if password OK and both verifications OK but pin_hash is null (registration-recovery for users who somehow have no PIN)

Whichever endpoint emits it, the response shape is `{ ..., next_action: "pin_setup_required", pin_setup_token: "<JWT>" }`.

`/auth/pin/set` accepts **only** this scoped token (rejects refresh and access tokens). After successful PIN set:

1. Token's jti added to `TokenRevocationService` blocklist (one-time use).
2. Full access + refresh token pair issued.

This closes the squat hole where an attacker who knows a victim's phone could pre-set the victim's PIN.

## 7. Registration flow (new user, happy path)

```
1. POST /auth/register {phone, email, full_name, password, referral_code?}
   → normalise phone → uniqueness checks → INSERT User → INSERT two OtpCode rows →
     send email OTP via Resend, phone OTP via Termii (channel=dnd)
   → 201 { user_id, email, phone, referred_by, next_action: "verify_email_and_phone" }
   → NO TOKENS

2. POST /auth/email/verify {email, code}
   → if both verifications now True AND pin_hash IS NULL:
     → 200 { email_verified: True, phone_verified: True,
             next_action: "pin_setup_required",
             pin_setup_token: <scoped JWT, 10 min TTL> }
   → else:
     → 200 { email_verified: True, phone_verified: <current>, next_action }

3. POST /auth/phone/verify {phone, code}
   → if both verifications now True AND pin_hash IS NULL:
     → 200 { phone_verified: True, next_action: "pin_setup_required",
             pin_setup_token: <scoped JWT, 10 min TTL> }
   → else:
     → 200 { phone_verified: True, email_verified: <current>, next_action }

4. POST /auth/pin/set
   Headers: X-Pin-Setup-Token: <scoped JWT>
   Body: { pin }
   → verify gates (email + phone both true, pin_hash null) → hash PIN with argon2id →
     blocklist the scoped jti → issue full access + refresh
   → 200 { tokens: {access, refresh, expires_in}, pin_set: True }
```

Steps 2 and 3 may happen in either order; mobile can send them in parallel or in sequence — UX choice.

## 8. Existing-user migration flow

Two entry points, both safe.

### 8.1 Existing refresh token still valid

```
App opens → mobile uses cached refresh_token → /auth/refresh succeeds →
            mobile calls /auth/me → reads state →
              if !phone_verified → routes to "Verify your phone" →
                mobile calls /auth/phone/resend with phone from /me →
                user enters OTP → /auth/phone/verify →
                response includes pin_setup_token if !pin_set
              if !pin_set → routes to "Set your PIN" → /auth/pin/set
            → home
```

### 8.2 Refresh token expired or revoked

```
App opens → no valid refresh_token → mobile shows phone+password login →
            POST /auth/login {phone, password} →
              server validates password, sees !phone_verified →
              fires sms_provider.send_otp inline (subject to 60s cooldown) →
              200 { next_action: "phone_verification_required",
                    phone_otp_sent: True } (no tokens) →
            mobile routes to OTP screen →
            POST /auth/phone/verify → response with pin_setup_token if !pin_set →
            POST /auth/pin/set → tokens issued → home
```

If the existing user happens to already have `pin_set=True` (set voluntarily under the old optional-PIN model), step "/auth/pin/set" is skipped — `/auth/phone/verify` returns full tokens directly.

## 9. PIN-login (cold-start)

```
POST /auth/pin-login
Body: { refresh_token: str, pin: str }

Server, in order:
 1. Decode refresh_token (HS256). Fail → 401 INVALID_TOKEN.
 2. payload.typ must == "refresh". Else → 401 INVALID_TOKEN.
 3. Extract user_id from payload.sub, jti from payload.jti.
 4. RedisTokenStore.is_valid(user_id, jti). Fail → revoke_all(user_id) (replay defense) → 401.
 5. Load User by user_id. Not found → 401 USER_NOT_FOUND.
 6. user.is_active must be True. Else → 403 ACCOUNT_DISABLED.
 7. tokens_revoked_at vs payload.iat check (existing logic). Predated → 401.
 8. PIN lockout check (`pin_locked:{user_id}` Redis key). Locked → 423 PIN_LOCKED.
 9. user.pin_hash must not be NULL. Else → 400 PIN_NOT_SET.
10. argon2id verify(pin, user.pin_hash). Fail → increment attempts, possibly lock, 401 INVALID_PIN.
11. Transparent rehash if pin_needs_rehash(user.pin_hash).
12. Rotate refresh_token (revoke old jti, issue new pair, save new jti).
13. Return { tokens, pin_set: True }.
```

PIN lockout reuses existing `PinService` Redis keys (`pin_attempts:{user_id}`, `pin_locked:{user_id}`) — 5 wrong attempts = 30-min freeze. No duplicate lockout machinery.

## 10. Refresh-token lifetime + logout

- **Refresh-token TTL**: unchanged at 30 days. Active users rotate well before expiry via pin-login or refresh. Inactive ≥31 days → token expires → mobile clears → phone+password login.
- **`/auth/logout` (single device)**: unchanged. Revokes current access jti to blocklist + refresh jti from RedisTokenStore. Mobile clears local refresh_token.
- **Logout-everywhere**: deferred to Phase D.

## 11. Credential-change interactions

| Action | Refresh tokens revoked? |
|---|---|
| Password change (`/auth/password/change`) | **Yes** — all sessions, all devices, force re-login |
| PIN change (`/auth/pin/change`) | **No** — devices keep working with refresh_token; user enters new PIN on next cold-start |
| Phone change (`/auth/phone/change-confirm`) | **Yes** — force re-login |

PIN change not revoking sessions is deliberate. If user suspects PIN compromise, separate "logout everywhere" (Phase D) is the right action.

## 12. SMS + email integration

### 12.1 Termii — channel switch + template

Switch from `channel: "generic"` to `channel: "dnd"` for OTP messages. The `generic` channel is silently blocked for phones on the NCC DND registry (most Nigerian numbers); `dnd` is the transactional channel.

Cost: ~₦5/SMS via `dnd` vs ~₦2.5 via `generic`. Acceptable trade for guaranteed delivery on auth flows.

Sender ID stays configurable via `TERMII_SENDER_ID` (default `Timpbills`). **Operational action item:** confirm with Termii that the `Timpbills` alpha sender ID is registered + approved on the production account.

Template (one segment, ~80 chars, GSM-7 compatible):

```
Your Timpbills code is {code}. It expires in 5 minutes. Do not share this code.
```

### 12.2 Resend (email) — unchanged

Existing integration. New volume: every registration now sends email OTP **and** phone OTP in parallel.

### 12.3 OTP TTL standardised to 5 minutes

Phone-change drops from 10 min → 5 min (`PHONE_CHANGE_TTL_SECONDS` constant updated). All OTPs (register, login migration, password reset, phone change) use 5 min.

### 12.4 Resend cooldown

Per-identity, per-purpose 60-second cooldown enforced at the OtpCode row:

```python
last = db.query(OtpCode).filter(user_id=u.id, purpose=p).order_by(created_at.desc()).first()
if last and (now() - last.created_at).total_seconds() < 60:
    raise ValueError("OTP_COOLDOWN_ACTIVE")
```

Returns `429 OTP_COOLDOWN_ACTIVE` with `Retry-After` header.

## 13. Data migration (Alembic)

One new Alembic migration: normalise existing `User.phone` to E.164. Idempotent.

```python
# alembic/versions/YYYYMMDD_normalize_phone_e164.py
from app.utils.phone import normalize_to_e164
from sqlalchemy import text

def upgrade():
    conn = op.get_bind()
    rows = conn.execute(text("SELECT id, phone FROM users WHERE phone NOT LIKE '+%'"))
    for row in rows:
        try:
            new = normalize_to_e164(row.phone)
        except ValueError:
            # log corrupt row; manual cleanup
            continue
        conn.execute(text("UPDATE users SET phone=:p WHERE id=:id"),
                     {"p": new, "id": row.id})

def downgrade(): pass  # irreversible; normalisation is forward-only
```

No new tables, no schema changes. Existing `is_phone_verified=True` users (tier_1) need no migration.

## 14. Rate-limiting (final cut)

| Endpoint | Limit |
|---|---|
| `/auth/register` | 3/min per IP |
| `/auth/login` | 5/min per IP |
| `/auth/pin-login` | 10/min per IP + 5-attempt per-user PIN lockout (30-min freeze) |
| `/auth/pin/set` | 3/min per IP |
| `/auth/phone/verify`, `/auth/email/verify` | 5/min per IP + OtpCode attempts cap (3) |
| `/auth/phone/resend`, `/auth/email/resend` | 3/min per IP + 60s server cooldown per identity |
| `/auth/password/forgot` | 3/min per IP |
| `/auth/refresh`, `/auth/logout` | unlimited |

## 15. Error handling (selected codes)

| Code | HTTP | Mobile action |
|---|---|---|
| INVALID_PHONE_FORMAT | 400 | Inline field error |
| PHONE_ALREADY_IN_USE | 409 | Send to login |
| USER_ALREADY_EXISTS | 409 | Send to login |
| PHONE_RECENTLY_DELETED | 409 | "Try again after X days" |
| INVALID_OTP | 400 | Show attempts remaining |
| OTP_ATTEMPTS_EXCEEDED | 429 | Force resend |
| OTP_EXPIRED | 410 | Show resend |
| OTP_COOLDOWN_ACTIVE | 429 | Show countdown |
| GATES_NOT_MET | 400 | Send back to verification |
| VERIFICATION_REQUIRED | 403 | Route to failed-gate's screen |
| INVALID_PIN | 401 | Show attempts remaining |
| PIN_LOCKED | 423 | Show lockout banner |
| INVALID_TOKEN | 401 | Fall back to password login |
| ACCOUNT_DISABLED | 403 | Show "Account disabled" |

## 16. Deploy / rollout plan

Six steps, additive backend changes + a soft-mode flag for safe rollout.

### Step 1 — Backend Phase A release (additive, no breakage)
- Add `/auth/phone/verify` (signup variant), `/auth/pin/set` with scoped token contract
- `/auth/login` response gains `next_action`
- Phone normalisation helper + Alembic data migration
- Termii channel → `dnd`
- New setting `AUTH_STRICT_GATES: bool = False` (soft mode)
- `require_full_auth_gates` dep: soft → logs warning + allows; hard → 403
- Deploy → **zero user impact** (old mobile still works; new endpoints unused)

### Step 2 — Mobile Phase A release
- Phone-only login UI, phone OTP flow, set-PIN screen
- Routes by `next_action` field
- TestFlight / Play internal → public

### Step 3 — Flip the strict-gates flag
- After ~80% of mobile users updated (tracked via app-version analytics, typically 1–2 weeks)
- Set `AUTH_STRICT_GATES = True` in production env
- Old mobile versions can still register/login/verify; only money endpoints refuse until gates pass

### Step 4 — Backend Phase B release
- Add `/auth/pin-login` endpoint
- No breaking changes

### Step 5 — Mobile Phase B release
- PIN screen on cold-start, forgot-PIN fallback to password
- Roll out same pattern as Phase A mobile

### Step 6 — Cleanup (months later)
- Remove `AUTH_STRICT_GATES` flag (assume strict always)
- Remove email-as-identifier dead-code paths

## 17. Feature flags (settings)

```python
class Settings(BaseSettings):
    AUTH_STRICT_GATES: bool = False
    AUTH_PIN_LOGIN_ENABLED: bool = True
```

Two booleans. Heavy feature-flag systems are overkill for this rollout.

## 18. User communication

Pre-rollout:
- In-app banner on 1–2 mobile versions before flag flip: "We're upgrading login security. On your next login, you'll be asked to verify your phone and create a 4-digit PIN."
- Push notification at flag-flip moment.
- Help-centre article with screenshots of the migration flow.

Skipping this generates a flood of "I can't log in" support tickets at flip time.

## 19. Rollback plan

| Failure | Detection | Rollback |
|---|---|---|
| `/auth/pin-login` bug locks users out | Endpoint 401 spike; tickets | Mobile already falls back to password on pin-login failure. Server: revert image. |
| Phone normalisation regression | Login failure spike for affected phones | Fix forward; migration is idempotent on re-run |
| Termii `dnd` channel delivery issue | OTP failure rate > 5% | Env-toggle `TERMII_OTP_CHANNEL` back to `generic` (no redeploy) |
| Strict-gate too aggressive | 403 spike from older mobile clients | Flip `AUTH_STRICT_GATES = False` → soft mode resumes immediately |
| Scoped pin_setup_token exploit | Anomalous endpoint patterns | Revoke endpoint (503) pending fix; user can re-set PIN via forgot-PIN |
| Mass registration drop | `/register → /pin/set` completion < 60% | Investigate funnel; targeted fix |

## 20. Monitoring

| Metric | Alert threshold |
|---|---|
| Termii OTP send success rate | < 95% |
| OTP send latency p95 | > 3s |
| `/auth/pin-login` 401 rate (excl. legitimate wrong-PIN) | > 10% |
| `/auth/login` `next_action` distribution | Spike in `phone_verification_required` after flag-flip is expected; should settle in 7 days |
| Registration completion funnel | < 60% completion |
| Strict-gate 403 rate post-flag-flip | Sustained → mobile not ready, flip flag back |

Loguru structured logs + `/health` endpoint counters; Grafana dashboard is a Phase D nice-to-have.

## 21. Test plan

### Unit
- Phone normalisation: every accepted variant + every reject case
- Scoped JWT issue + verify + one-time-use blocklist
- PIN-login orchestration (each error branch)
- OTP cooldown logic

### Integration
- Full new-user happy path: register → verify email → verify phone → pin/set → home
- Existing-user migration: seeded user with `email_verified=True, is_phone_verified=False, pin_hash=NULL` → login → phone OTP → pin/set → home
- Existing-user migration with PIN already set: skips pin/set
- Cold-start pin-login → home
- Forgot PIN → password → pin/set → home
- `/auth/pin/set` rejected without scoped token (with refresh, with access, with no token)
- `/auth/pin/set` rejected when gates not met
- PIN change does NOT revoke other sessions
- Password change DOES revoke other sessions
- Strict-gate enforcement when `AUTH_STRICT_GATES = True`
- Soft-mode warning logged when `AUTH_STRICT_GATES = False`

### E2E (against real DB + fake Termii/Resend)
- Registration funnel end-to-end
- SMS provider receives normalised phone

### Migration
- Alembic migration is idempotent (re-run on already-migrated data is no-op)
- Migration handles corrupt phone values gracefully (log + skip)

## 22. Risks accepted (not solved in Phase A+B)

1. **Termii outage = registration funnel stops.** Fallback SMS provider is Phase D.
2. **Lost-phone scenario without phone-change flow**: admin reset path covers it; out of scope here.
3. **PIN compromise without device theft**: PIN-change alone doesn't revoke other devices. Mitigated when `/auth/logout-everywhere` ships (Phase D).
4. **Mass migration support load**: a percentage of users will hit edge cases (phones in odd formats, lapsed Termii balance, etc.). Mitigated by communications (§18) and monitoring (§20).

---

## Open items requiring operational confirmation before implementation

- Confirm `Timpbills` alpha sender ID is approved on Termii production account
- Confirm Termii balance threshold + auto-recharge configuration before flag-flip
- Confirm Resend domain + sender are configured for production volumes
