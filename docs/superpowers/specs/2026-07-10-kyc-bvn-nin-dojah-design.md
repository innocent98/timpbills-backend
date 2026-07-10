# KYC — BVN / NIN Verification via Dojah (Tier 1→2→3)

**Date:** 2026-07-10
**Status:** Approved design → implementation
**Scope:** Cross-stack (timpbills-backend + timpbills mobile)

## 1. Summary

Phone verification (Tier 0 → Tier 1) is already live. This work delivers the
remaining KYC upgrades:

- **Tier 1 → Tier 2** via **BVN** verification (Dojah)
- **Tier 2 → Tier 3** via **NIN** verification (Dojah)

Verification is **synchronous** — a Dojah BVN/NIN lookup returns a match result
in ~1–3 seconds. The mobile app shows a brief loading state, then a success or
failed screen. The handoff's elaborate "pending" screen is retained only as a
slow-path fallback surfaced via `GET /kyc/status`.

**Liveness / selfie checks are out of scope.** BVN/NIN number-match against the
user's date of birth only.

### Design decisions (locked)

| # | Decision | Choice |
|---|----------|--------|
| 1 | KYC landing screen | Keep + upgrade the existing flat `KycTierPage`; adopt only the net-new handoff screens. Do **not** adopt the handoff's gradient hub (off-brand). |
| 2 | Verification model | **Synchronous**. No Celery/webhook/polling. Pending screen = slow-path fallback only. |
| 3 | Limits enforcement | Enforce the 4-tier **max-wallet-balance** cap only. Per-txn + daily are **display-only** this sprint. |
| 4 | KYC record storage | **Minimal record, no raw PII.** No encryption helper built. Store reference + status + masked id only. |
| 5 | Provider | Dojah, behind a `KycProvider` protocol + `FakeKycProvider`. Ship on the fake until creds arrive. |
| 6 | DOB | Required for the Dojah match. Expose a DOB field in the form when the user has none; prefill when set. |
| 7 | Tier 3 per-txn display value | **₦5,000,000** (PRD.txt §13, authoritative). Handoff's ₦200k treated as a stale copy-paste. |

### Flagged open items (non-blocking)

- Exact Dojah endpoint paths + response shape — confirm against Dojah docs when
  creds arrive; fully isolated inside the adapter.
- Whether Dojah **sandbox** creds are available now. Until then, the factory
  returns `FakeKycProvider` and the entire flow is exercisable end-to-end.

## 2. Tier model

| Tier | Requirement | Per-txn (display) | Daily (display) | Max balance (**enforced**) |
|------|-------------|-------------------|-----------------|----------------------------|
| 0 | Registration only | ₦50,000 | ₦50,000 | ₦50,000 |
| 1 | Phone (OTP) — **live** | ₦50,000 | ₦50,000 | **₦300,000** (was ₦200,000) |
| 2 | BVN (Dojah) | ₦200,000 | ₦200,000 | ₦500,000 |
| 3 | NIN (Dojah) | ₦5,000,000 | ₦5,000,000 | **Unlimited** (new) |

**PRD conflicts resolved (per standing instruction to flag explicitly):**
- PRD.txt §4 ("BVN/NIN → Tier 2", 3-tier) is stale; §13's 4-tier table governs.
- Tier 3 per-txn: PRD ₦5M chosen over handoff ₦200k.

## 3. Backend design (timpbills-backend)

### 3.1 Dojah adapter — `app/integrations/dojah/`

Mirrors `app/integrations/paystack/` layout:

- `base.py` — `KycProvider` protocol:
  - `verify_bvn(*, bvn: str, date_of_birth: date, full_name: str) -> KycMatchResult`
  - `verify_nin(*, nin: str, date_of_birth: date, full_name: str) -> KycMatchResult`
- `schemas.py` — `KycMatchResult` (matched: bool, provider_reference: str,
  failure_reason: str | None), plus internal Dojah request/response models.
- `client.py` — `DojahClient` (real HTTP; endpoint paths TBD from Dojah docs,
  isolated here).
- `fake.py` — `FakeKycProvider`: **deterministic**. Rule: `matched = (dob
  matches AND id passes a length/checksum rule)`; a designated failing test id
  returns `matched=False`. No network. Powers tests + dev.
- `factory.py` — `get_kyc_provider()` returns `FakeKycProvider` when
  `settings.DOJAH_API_KEY is None` or `settings.FORCE_FAKE_PROVIDERS`, else
  `DojahClient`.

**Config additions** (`app/core/config.py`):
```
DOJAH_API_KEY: str | None = None
DOJAH_APP_ID: str | None = None
DOJAH_BASE_URL: str = "https://sandbox.dojah.io"
```

### 3.2 `KycService` — `app/services/kyc_service.py`

`verify_bvn(user, bvn, date_of_birth)` (and symmetric `verify_nin`):

1. **Tier gate.** BVN requires `user.kyc_level == tier_1`; NIN requires
   `tier_2`. Else `409 kyc_tier_precondition` (already at/above, or too low).
2. **DOB resolution.**
   - `user.date_of_birth` set → use it (ignore any supplied dob).
   - null + `date_of_birth` supplied → persist to user, use it.
   - null + none supplied → `422 date_of_birth_required`.
3. Insert `kyc_records` row: status=`pending`, type, provider=`dojah`,
   `tier_before`, `masked_id` (last 2 digits).
4. Call the adapter. On `matched=True`: set record `status=success`,
   `tier_after`, upgrade `user.kyc_level`, refresh wallet cap
   (`WalletService`/`_KYC_CAPS`). On `matched=False`: `status=failed` +
   `failure_reason`, tier unchanged.
5. Return the outcome DTO.

Idempotency: a fresh successful record supersedes; re-verifying an already-upgraded tier returns `409`.

### 3.3 Endpoints — `app/api/v1/endpoints/kyc.py`

Authenticated (`get_current_user`). **No `pin_token`** — KYC is not a money op.

| Method | Path | Body | Response |
|--------|------|------|----------|
| POST | `/api/v1/kyc/bvn` | `{ "bvn": "<11 digits>", "date_of_birth": "YYYY-MM-DD"? }` | `KycVerifyResponse` |
| POST | `/api/v1/kyc/nin` | `{ "nin": "<11 digits>", "date_of_birth": "YYYY-MM-DD"? }` | `KycVerifyResponse` |
| GET | `/api/v1/kyc/status` | — | `KycStatusResponse` |

### 3.4 API contract (frozen — both agents build against this)

`KycVerifyResponse` (200):
```json
{
  "status": "success",              // "success" | "failed" | "pending"
  "tier": 2,                        // numeric tier AFTER this call
  "verification_type": "bvn",       // "bvn" | "nin"
  "reference": "KYC-7741Q",
  "failure_reason": null            // string when status == "failed"
}
```

`KycStatusResponse` (200):
```json
{
  "tier": 2,
  "records": [
    { "verification_type": "bvn", "status": "success", "reference": "KYC-7741Q",
      "created_at": "2026-07-10T12:00:00Z", "failure_reason": null }
  ]
}
```

Error envelope (existing project style — `{ "code", "message" }`):

| HTTP | code | When |
|------|------|------|
| 422 | `date_of_birth_required` | DOB missing and not on file |
| 422 | `invalid_bvn` / `invalid_nin` | Not 11 digits |
| 409 | `kyc_tier_precondition` | Wrong current tier for this step |
| 502 | `kyc_provider_error` | Dojah unreachable/5xx (record left `pending`; retry via status) |

### 3.5 Migrations — `alembic/versions/`

1. **Add `tier_3` to `kyc_level_enum`.** Postgres `ALTER TYPE ... ADD VALUE`
   cannot run inside a transaction block — use the non-transactional pattern
   (`op.execute` with `connection.execution_options(isolation_level=...)` /
   `COMMIT` bridge as used for enum edits) so alembic doesn't wrap it.
2. **Create `kyc_records`:**

| Column | Type | Notes |
|--------|------|-------|
| id | UUID | PK |
| user_id | UUID | FK → users, indexed |
| verification_type | VARCHAR(8) | `bvn` / `nin` |
| provider | VARCHAR(16) | `dojah` |
| provider_reference | VARCHAR | Dojah reference / our `KYC-xxxx` |
| status | VARCHAR(12) | `pending` / `success` / `failed` |
| tier_before | INT | |
| tier_after | INT | nullable (null while pending/failed) |
| masked_id | VARCHAR(8) | last 2 digits only, e.g. `•••••••••17` |
| failure_reason | TEXT | nullable |
| created_at / updated_at | TIMESTAMPTZ | TimestampMixin |

**No raw BVN/NIN, no Dojah raw payload persisted.**

### 3.6 Wallet cap update — `app/services/wallet_service.py`

```
_KYC_CAPS = {
  tier_0: 50_000,
  tier_1: 300_000,   # was 200_000
  tier_2: 500_000,
  tier_3: None,      # unlimited → skip cap check when None
}
```
Credit path: when cap is `None`, bypass the `new_balance > cap` check. Existing
`KycCapExceeded` path unchanged for capped tiers.

`/auth/me` already emits `kyc_level` numeric — `tier_3 → 3`, no change beyond
the enum supporting the value.

## 4. Mobile design (timpbills)

### 4.1 Upgrade `KycTierPage` (kept)

- 4-tier limits (per-txn / daily / max) from §2.
- Step 3 becomes **actionable**: "Verify BVN" for tier 1 → `/profile/kyc/bvn`;
  "Verify NIN" for tier 2 → `/profile/kyc/nin`. Remove "Coming soon" lock.
- Bottom CTA routes to the correct upgrade screen by tier.

### 4.2 New screens (from `handoff/src/timpbills-kyc.jsx`, flat aesthetic)

- `KycBvnPage` — step indicator, 11-digit BVN field, NIBSS privacy note, CTA.
- `KycNinPage` — step indicator, 11-digit NIN field, "What you unlock" table, CTA.
- Verification **states** folded in-page (loading → success/failed); pending is
  the slow-path fallback (poll `GET /kyc/status`, "Refresh status" ghost button).
- `KycLimitReachedSheet` — **full-width** bottom sheet (per standing rule),
  Now/After tier compare, "Verify BVN to continue" + "Maybe later".

### 4.3 DOB handling

Form reads `me.dateOfBirth`:
- null → render a **required** date-picker field; submit sends `date_of_birth`.
- set → **prefill** (display, not re-sent unless changed).

### 4.4 Wire limit-reached

Fund-wallet already receives `422 KYC_LIMIT_EXCEEDED`. Catch it → present
`KycLimitReachedSheet` → route to `/profile/kyc/bvn`.

### 4.5 Data layer

- DTOs: `KycVerifyResponse`, `KycStatusResponse` (freezed + json).
- `KycRepository` (real) + `FakeKycRepository` (mirrors existing
  `fake_auth_repository` pattern) so FE builds against the contract before BE lands.
- Riverpod controller; **invalidate `meControllerProvider` on success** so tier
  updates app-wide.
- Routes: add `/profile/kyc/bvn`, `/profile/kyc/nin` to `routes.dart` +
  `app_router.dart`.

## 5. Data flow (BVN happy path)

```
KycTierPage → KycBvnPage → [DOB field if me.dateOfBirth == null]
  → POST /kyc/bvn { bvn, date_of_birth? }
  → KycService: gate tier_1, resolve DOB, record=pending
  → Dojah/Fake match
      matched  → record=success, kyc_level=tier_2, cap=₦500k → success screen
                 → invalidate me → app-wide tier refresh
      no-match → record=failed → failed screen ("check number and DOB") → retry
```

NIN path is symmetric (tier_2 → tier_3, cap → unlimited).

## 6. Testing

**Backend**
- Tier gating (BVN needs tier_1; NIN needs tier_2; wrong tier → 409).
- DOB: required-when-missing (422), persist-when-supplied, prefill-when-set.
- Success upgrades tier + refreshes cap; failure preserves tier.
- `FakeKycProvider` match/no-match determinism.
- Endpoint auth + validation (11-digit, missing DOB).
- Wallet-cap tests updated: tier_1 = ₦300k, tier_3 unlimited (no `KycCapExceeded`).
- Migration smoke: enum has `tier_3`, `kyc_records` present.

**Mobile**
- `KycTierPage` renders all 4 tiers + correct limits; step 3 actionable per tier.
- BVN/NIN form: 11-digit validation, conditional DOB field required vs prefilled.
- State screens (loading/success/failed) render from controller state.
- `KycLimitReachedSheet` full-width; routes to BVN.
- Controller invalidates `me` on success.

## 7. Parallel execution

Contract in §3.4 is frozen. Two agents run concurrently:

- **backend-engineer:** adapter (base/client/fake/factory/schemas), config,
  `KycService`, `/kyc` endpoints, migrations (tier_3 + kyc_records), wallet-cap
  update, tests.
- **mobile-engineer:** `KycTierPage` upgrade, BVN/NIN pages + state screens, DOB
  conditional field, `KycLimitReachedSheet` + fund-flow wiring, `KycRepository` +
  `FakeKycRepository` + controller + DTOs, routes, tests.

Mobile stubs against `FakeKycRepository`, so neither side blocks the other.
