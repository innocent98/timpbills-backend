# Admin API + Notification Logs — Design Spec

**Date:** 2026-06-04
**Status:** Approved for planning
**Scope:** Cross-stack — `timpbills-backend` (FastAPI) + `timpbills-marketing` (Next.js `platform-admin`)
**PRD basis:** PRD.txt §16 (Admin System), §15 (Notifications); TECHNICAL_PRD.md §7.8 (Admin endpoints), §6.2 (operational tables)

---

## 1. Background & Motivation

Of the original 12-sprint PRD, Phase 1 (fintech MVP) and Sprint 5b (referral) are
fully shipped on both stacks. The two remaining **unblocked** workstreams — the rest
require external accounts not yet provisioned — are:

- **Smile ID KYC** (replacing Dojah) — account/API not ready. Deferred.
- **Amadeus flights/booking** — account/API not ready. Deferred.

This sprint delivers the two launch-hardening items that need nothing external:

1. **Admin API** — the backend currently exposes a single endpoint
   (`POST /admin/refunds/{reference}/trigger`) gated by an `is_admin` flag. The
   `platform-admin` dashboard in `timpbills-marketing` is a complete UI prototype
   (overview, transactions, users, refunds, bookings) but runs on **mock data**
   (`app/platform-admin/data.ts`: *"Front-end prototype: no backend wiring"*). We build
   the backend API and wire the dashboard to it.
2. **`notification_logs`** — every push/email/SMS send is currently fire-and-forget
   with no audit trail. We add the table, the write path, and a read endpoint.

### Non-goals (deferred to v2, per PRD §16)
- RBAC role enforcement (the `role` column ships now, unused, to avoid a v2 backfill).
- User management writes (suspend, edit, KYC override).
- Booking management (resend ticket, cancel) — and the booking **read** API too, because
  no `bookings` table exists until the flights phase lands.
- Dedicated admin-action audit table + 2FA.

---

## 2. Admin Authentication — opaque server-side session (cookie)

Admin auth is **fully separate** from the mobile Bearer-JWT path. Rationale: the admin
surface is a browser-based money-ops tool where XSS-resistance and instant revocation
matter more than statelessness.

### 2.1 `admin_users` table (new)

| Column | Type | Notes |
|---|---|---|
| `id` | UUID (PK) | server-generated |
| `email` | VARCHAR, unique, not null | login identifier |
| `password_hash` | VARCHAR, not null | argon2id via existing `core/security.py` |
| `full_name` | VARCHAR, not null | display |
| `role` | ENUM(`superadmin`,`support`) | **only `superadmin` used in v1**; present so v2 RBAC needs no backfill |
| `is_active` | BOOLEAN, default true | inactive admins are rejected at auth |
| `last_login_at` | TIMESTAMP, nullable | updated on successful login |
| `created_at` / `updated_at` | TIMESTAMP | `TimestampMixin` |

### 2.2 Session mechanism (opaque, Redis-backed)

- **No JWT for admins.** `POST /admin/login` validates email + password, then creates a
  session record in Redis: key `admin_session:{sid}` → JSON `{admin_id, role, created_at}`,
  with a **sliding TTL** (default 8h, refreshed on each authenticated request).
- `sid` is a 256-bit URL-safe random token (`secrets.token_urlsafe(32)`).
- Response sets cookie:
  `admin_session=<sid>; HttpOnly; Secure; SameSite=Lax; Domain=.timpbills.com; Path=/; Max-Age=<ttl>`.
  `Domain=.timpbills.com` makes browser→API a **same-site** request (dashboard on
  `timpbills.com`, API on `api.timpbills.com`), so `SameSite=Lax` holds.
- `POST /admin/logout` deletes the Redis key and clears the cookie.

### 2.3 `require_admin` dependency (rewritten)

1. Read `admin_session` cookie. Missing → 401 `ADMIN_AUTH_REQUIRED`.
2. Redis lookup. Missing/expired → 401 `ADMIN_SESSION_EXPIRED`.
3. Load `admin_users` row by `admin_id`. Missing → 401.
4. `is_active` false → 403 `ADMIN_DISABLED`.
5. Refresh the session TTL, return the admin row.

The existing `POST /admin/refunds/{reference}/trigger` migrates from the old
`is_admin`-flag dep onto this one. Audit rows continue to record the admin actor.

### 2.4 CSRF protection (write endpoints)

State-changing admin routes (refund trigger, requery, logout) require a **double-submit
CSRF token**: `/admin/login` also sets a non-HttpOnly `admin_csrf` cookie; mutating
requests must echo it in an `X-CSRF-Token` header. The server compares header vs cookie.
Read endpoints (`GET`) are exempt.

### 2.5 Rate limiting

`POST /admin/login`: 5/min per IP + 10/hour per email (mirrors `/auth/login`, slowapi).

### 2.6 Seeding & migration

- `scripts/create_admin.py` — CLI that prompts for email + password + full_name,
  argon2-hashes, inserts an `admin_users` row. Idempotent (skips if email exists).
- The Alembic migration that creates `admin_users` also **drops the dead `is_admin`
  column from `users`** (added in 202604281200; nothing else references it once
  `require_admin` is rewritten). Runbook note: create the first admin via the CLI
  immediately post-migration.

---

## 3. Read Endpoints

All under `/admin`, all gated by `require_admin`. Response envelope matches the existing
`success()` / error shape. Pagination follows the existing `/transactions` convention
(`limit`/`offset` + `total`).

### 3.1 `GET /admin/overview?days=7`

Computes **only metrics backed by real data**. Returns:

```json
{
  "range_days": 7,
  "volume_ngn": "42800000.00",
  "transaction_count": 18204,
  "success_rate": 0.974,
  "refund_count": 262,
  "refund_total_ngn": "318000.00",
  "service_mix": [{"type": "airtime", "pct": 0.38}, ...],
  "daily_volume": [{"date": "2026-05-29", "success": 2410, "failed": 64}, ...],
  "needs_attention": {
    "refunds_awaiting": 6,          // status in (refund_pending, refund_failed)
    "transactions_pending_over_5min": 3
  }
}
```

**Explicitly omitted** (UI drops these tiles): `avg_processing_time` (we don't persist
per-txn latency) and the Amadeus-latency alert (no flights). No fabricated numbers.

### 3.2 `GET /admin/transactions` + `GET /admin/transactions/{reference}`

- **List**: paginated; filters `type`, `status`, `date_from`, `date_to`, `user_id`,
  `q` (matches reference or customer name). Each row: `reference`, `type`, `status`,
  `amount`, `customer_name`, `created_at`.
- **Detail**: the transaction + event timeline (`transaction_events`) + linked `payment`
  (provider, provider_reference, method, last4, bank) + user summary
  (id, name, email, phone, tier).

### 3.3 `GET /admin/users` + `GET /admin/users/{id}`

- **List**: paginated; filters `q` (name/email/phone), `tier`, `status`. Each row:
  `id`, `full_name`, `email`, `phone`, `kyc_tier`, `wallet_balance`, `status`
  (active/deleted), `created_at`.
- **Detail** (read-only): profile + KYC tier + wallet balance + recent transactions
  (last 10) + referral summary (code, referred count, earned). No write capability.

### 3.4 `GET /admin/refunds`

Paginated list; filter `status` (pending/processed/failed). Each row: refund id,
original txn reference, type, amount, customer, reason, status, age, `manual` flag.
(The trigger endpoint already exists — §4.1.)

### 3.5 `GET /admin/notifications`

Paginated read over `notification_logs`; filters `channel`, `event`, `status`,
`user_id`. Backs a new platform-admin notifications page (§6).

### 3.6 PII handling

Admin endpoints return **full** email/phone/name. The surface is authenticated,
rate-limited, session-audited, and exists for support — masked data would defeat its
purpose. The `platform-admin` prototype masks cosmetically; the API returns real values
and the UI decides. (Per-field access audit logging is a v2 item.)

---

## 4. Write Endpoints (v1)

### 4.1 `POST /admin/refunds/{reference}/trigger` (existing)

Re-gated to the new `require_admin` + CSRF. Behaviour unchanged: idempotent manual
refund with `transaction_events` audit row recording the admin actor.

### 4.2 `POST /admin/transactions/{reference}/requery` (new)

Re-polls the provider for a stuck transaction:
- VTPass bill txns → `BillProvider.requery(request_id=reference)`.
- Paystack wallet-funding txns → `PaymentProvider.verify_payment(reference)`.

If the provider now reports a definitive outcome, the txn transitions accordingly
(`pending → success` or `pending → failed → refund_pending → refunded` via the existing
state machine + refund path). Writes a `transaction_events` audit row with the admin
actor. No-op (200, current state echoed) if the txn is already terminal. Backs the
dashboard "Needs attention → Requery" action.

---

## 5. `notification_logs`

### 5.1 Table

| Column | Type | Notes |
|---|---|---|
| `id` | UUID (PK) | |
| `user_id` | UUID FK → users, nullable | null for system/non-user sends |
| `event` | VARCHAR, not null | e.g. `bill_success`, `wallet_funded`, `otp` |
| `channel` | ENUM(`push`,`email`,`sms`) | one row per channel per dispatch |
| `status` | ENUM(`pending`,`sent`,`failed`) | lifecycle |
| `provider` | VARCHAR | `fcm`, `resend`, `termii` |
| `provider_reference` | VARCHAR, nullable | provider message id when returned |
| `error` | TEXT, nullable | failure detail |
| `created_at` | TIMESTAMP | row creation (dispatch attempt) |
| `sent_at` | TIMESTAMP, nullable | set on success |

Indexes: `user_id`, `event`, `channel`, `status`, `created_at`.

### 5.2 Write path

Inside `NotificationService` dispatch and the Celery notification task: insert a
`pending` row per channel before the provider call, update to `sent` (+ `sent_at`,
`provider_reference`) or `failed` (+ `error`) on result. **OTP SMS sends are logged too**
so the audit is complete across every channel.

---

## 6. Platform-admin wiring (`timpbills-marketing`, Next.js)

> ⚠️ Per repo `AGENTS.md`, this Next.js has breaking changes vs. training data — the
> implementer MUST read the relevant guide in `node_modules/next/dist/docs/` before
> writing routing/data-fetching/cookie code.

- Replace `app/platform-admin/data.ts` mocks with a typed API client.
- **Auth flow (browser → API directly, no BFF proxy):** the admin login page calls
  FastAPI `POST /admin/login` from the browser with `credentials: 'include'`. FastAPI
  sets the `admin_session` (HttpOnly) + `admin_csrf` cookies scoped to `.timpbills.com`,
  so the browser carries them on subsequent same-site requests to `api.timpbills.com`.
  Client-side fetches use `credentials: 'include'`; server components that fetch during
  SSR forward the incoming request's cookies to the API. Next.js is not an auth proxy —
  it never holds or re-issues the session token.
- Correct the UI to match the real API: drop the **avg-processing-time** KPI tile and the
  **Amadeus-latency** alert; the **bookings** page shows a "ships with flights" deferred
  state (kept, not wired).
- Add a **notifications** page backed by `GET /admin/notifications`.
- Wire write actions (refund trigger, requery) with the CSRF header.

---

## 7. Error Codes (additions)

| Exception / case | HTTP | Code |
|---|---|---|
| No admin session cookie | 401 | `ADMIN_AUTH_REQUIRED` |
| Session missing/expired in Redis | 401 | `ADMIN_SESSION_EXPIRED` |
| Bad login credentials | 401 | `ADMIN_INVALID_CREDENTIALS` |
| Admin row `is_active=false` | 403 | `ADMIN_DISABLED` |
| CSRF token missing/mismatch | 403 | `CSRF_FAILED` |
| Requery on terminal txn | 200 | (no-op, current state echoed) |

---

## 8. Testing

**Backend:**
- Admin auth: login success/failure, inactive-admin reject, session expiry, logout
  invalidates, CSRF enforced on writes / exempt on reads, rate limit.
- Each read endpoint: happy path, every filter, pagination bounds, 401 (no session) /
  403 (disabled).
- `requery`: pending→success, pending→failed→refunded, terminal no-op.
- `notification_logs`: row written `pending`→`sent` on success, `pending`→`failed` on
  provider error, across all three channels incl. OTP.
- `overview`: metric correctness against a seeded dataset (success rate, service mix,
  daily series, needs_attention counts).

**Marketing:** light component/smoke on the API client + auth wiring (internal tool).

---

## 9. Scope Boundaries

| | v1 (this sprint) | v2 (deferred) |
|---|---|---|
| Roles | single `superadmin` (column present) | RBAC enforcement (`support` etc.) |
| Users | read-only | suspend / edit / KYC override |
| Bookings | none (no model yet) | read + management (with flights) |
| Audit | `transaction_events` for refunds; `notification_logs` | dedicated admin-action audit table |
| Auth | opaque Redis session + CSRF | + 2FA |
