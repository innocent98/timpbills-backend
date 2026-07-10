# KYC — BVN / NIN Verification via Dojah (Tier 1→2→3) with Liveness + Face-Match

**Date:** 2026-07-10
**Status:** Approved design → implementation
**Scope:** Cross-stack (timpbills-backend + timpbills mobile)

## 1. Summary

Phone verification (Tier 0 → Tier 1) is already live. This work delivers:

- **Tier 1 → Tier 2** via **BVN** verification (Dojah)
- **Tier 2 → Tier 3** via **NIN** verification (Dojah)

Each upgrade requires **BVN/NIN + a live selfie with liveness detection and
1:1 face-match** against the photo on the government record. Number + DOB alone
is weak assurance (leaked-prone data); biometric proof-of-personhood is what
protects the higher wallet limits (₦500k, then unlimited) and aligns with CBN
tiered-KYC. Dojah provides Face Match (selfie vs BVN/NIN photo) and active
Liveness as first-class products.

**Verification is effectively synchronous** for the user: the Dojah widget
captures + processes on-device in seconds, then our backend confirms the result
server-side and returns success/failed. No user-facing polling in the happy path.

### Integration approach

Liveness capture must use **Dojah's on-device active-liveness logic**, so the
mobile app embeds the **Dojah Flutter SDK / widget**. Consequence:

- The **mobile** widget performs selfie + liveness + face-match (+ the BVN/NIN
  ID check) and returns a **`reference_id`** (plus a client-side status) via callback.
- The **backend** never trusts the client's "verified" claim. It takes the
  `reference_id` and **independently fetches the authoritative result from Dojah
  server-side**, validates it, and only then upgrades the tier.
- A **Dojah webhook** is the source-of-truth reconciliation backstop (mirrors the
  existing Paystack webhook pattern) for the case where the app dies after the
  widget completes but before it calls us. The synchronous fetch-by-reference is
  the UX accelerator; the webhook is the safety net. Both write **idempotently**
  to the same `kyc_records` row.

### Design decisions (locked)

| # | Decision | Choice |
|---|----------|--------|
| 1 | KYC landing screen | Keep + upgrade the existing flat `KycTierPage`; adopt only the net-new handoff screens. No gradient hub (off-brand). |
| 2 | Verification model | Synchronous for the user (widget → server-side confirm). Webhook = reconciliation backstop, not user-facing polling. |
| 3 | Biometrics | **Liveness + face-match on BOTH Tier 2 (BVN) and Tier 3 (NIN).** Built once via the Dojah Flutter SDK. |
| 4 | Confirm authority | **Server-side** Dojah result (fetch-by-reference / webhook) is the source of truth. The client SDK callback is never trusted as primary. |
| 5 | Limits enforcement | Enforce the 4-tier **max-wallet-balance** cap only. Per-txn + daily are **display-only** this sprint. |
| 6 | Record storage | **Minimal record, no raw PII, no selfie stored.** Reference + status + masked id + liveness/face booleans only. |
| 7 | Provider abstraction | `KycProvider` protocol + `FakeKycProvider`. Ship on the fake until Dojah creds land. |
| 8 | DOB | Required before verification (user instruction). Expose a DOB field when the user has none; prefill when set. Passed to Dojah + persisted to profile. |
| 9 | Tier 3 per-txn display value | **₦5,000,000** (PRD.txt §13, authoritative). |

### Flagged open items (non-blocking; isolated in adapters)

- Exact **Dojah Flutter SDK** package name/version, its config surface, whether it
  accepts a pre-supplied BVN/NIN to skip the widget's own entry step, and the
  callback payload shape.
- Dojah's **verification-fetch-by-reference** endpoint path + response schema, and
  webhook payload/signature scheme.
- Whether Dojah **sandbox** creds/app-id/public-key are available now. Until then
  the factory returns `FakeKycProvider` and the mobile SDK wrapper is mocked, so
  the full flow is exercisable end-to-end.

## 2. Tier model

| Tier | Requirement | Per-txn (display) | Daily (display) | Max balance (**enforced**) |
|------|-------------|-------------------|-----------------|----------------------------|
| 0 | Registration only | ₦50,000 | ₦50,000 | ₦50,000 |
| 1 | Phone (OTP) — **live** | ₦50,000 | ₦50,000 | **₦300,000** (was ₦200,000) |
| 2 | BVN + selfie/liveness/face-match | ₦200,000 | ₦200,000 | ₦500,000 |
| 3 | NIN + selfie/liveness/face-match | ₦5,000,000 | ₦5,000,000 | **Unlimited** (new) |

Only **max-balance** is enforced (updated caps); per-txn/daily are display-only.

**PRD conflicts resolved:** §4 (3-tier) is stale; §13's 4-tier table governs.
Tier 3 per-txn ₦5M chosen over the handoff's ₦200k.

## 3. Backend design (timpbills-backend)

### 3.1 Dojah adapter — `app/integrations/dojah/`

Mirrors `app/integrations/paystack/` (base / client / fake / factory / schemas /
signature):

- `base.py` — `KycProvider` protocol:
  - `fetch_verification(*, reference_id: str) -> KycVerificationResult`
- `schemas.py` — `KycVerificationResult`:
  `verification_type` (bvn/nin), `status` (success/pending/failed),
  `id_verified: bool`, `liveness_passed: bool`, `face_match: bool`,
  `masked_id: str` (last 2), `provider_reference: str`, `failure_reason: str | None`,
  plus the identity fields (name/DOB) needed to cross-check against the user.
- `client.py` — `DojahClient.fetch_verification` (real HTTP to Dojah's
  verification-status endpoint; path isolated here).
- `fake.py` — `FakeKycProvider`: **deterministic by reference_id** — e.g.
  `PASS*` → all-true success, `FAILFACE*` → face_match=False, `FAILLIVE*` →
  liveness_passed=False, `PENDING*` → pending. No network.
- `factory.py` — `get_kyc_provider()` → `FakeKycProvider` when
  `DOJAH_API_KEY is None` or `FORCE_FAKE_PROVIDERS`, else `DojahClient`.
- `signature.py` — verify the Dojah webhook signature (mirror Paystack's HMAC pattern).

**Config additions** (`app/core/config.py`):
```
DOJAH_API_KEY: str | None = None        # server-side secret
DOJAH_APP_ID: str | None = None         # widget init (client-shared)
DOJAH_PUBLIC_KEY: str | None = None     # widget init (client-shared)
DOJAH_BASE_URL: str = "https://api.dojah.io"
DOJAH_WEBHOOK_SECRET: str | None = None
```

### 3.2 `KycService` — `app/services/kyc_service.py`

`confirm_verification(user, verification_type, reference_id, date_of_birth)`:

1. **Tier gate.** BVN requires `user.kyc_level == tier_1`; NIN requires `tier_2`.
   Else `409 kyc_tier_precondition`.
2. **DOB resolution.** set → use; null + supplied → persist; null + none → `422
   date_of_birth_required`.
3. Upsert `kyc_records` (keyed on `provider_reference`): status=`pending`, type,
   `tier_before`, `masked_id`.
4. **Fetch authoritative result** via the adapter (server-side).
5. **Validate:** `status==success` AND `id_verified` AND `liveness_passed` AND
   `face_match` AND `verification_type` matches AND the returned identity matches
   the user (name/DOB). Any false → record `failed` + `failure_reason`, tier
   unchanged.
6. On full pass → record `success`, `tier_after`, upgrade `user.kyc_level`,
   refresh wallet cap. **Idempotent:** a second confirm (or the webhook) for the
   same reference is a no-op if already applied.

The webhook handler and this endpoint share the same `confirm_verification`
core so both paths converge on identical validation + idempotency.

### 3.3 Endpoints — `app/api/v1/endpoints/kyc.py`

Authenticated (`get_current_user`). **No `pin_token`** — KYC is not a money op.

| Method | Path | Body | Response |
|--------|------|------|----------|
| POST | `/api/v1/kyc/verify` | `{ "verification_type": "bvn"\|"nin", "reference_id": str, "date_of_birth": "YYYY-MM-DD"? }` | `KycVerifyResponse` |
| GET | `/api/v1/kyc/status` | — | `KycStatusResponse` (latest records + tier; slow-path fallback) |
| POST | `/api/v1/kyc/webhook` | Dojah payload | 200; signature-verified; reconciliation backstop |

### 3.4 API contract (frozen — both agents build against this)

`KycVerifyResponse` (200):
```json
{
  "status": "success",            // "success" | "failed" | "pending"
  "tier": 2,                      // numeric tier AFTER this call
  "verification_type": "bvn",     // "bvn" | "nin"
  "reference": "KYC-7741Q",
  "liveness_passed": true,
  "face_match": true,
  "failure_reason": null          // e.g. "face_mismatch" | "liveness_failed"
                                  //      | "id_not_verified" when status=="failed"
}
```

`KycStatusResponse` (200):
```json
{
  "tier": 2,
  "records": [
    { "verification_type": "bvn", "status": "success", "reference": "KYC-7741Q",
      "liveness_passed": true, "face_match": true,
      "created_at": "2026-07-10T12:00:00Z", "failure_reason": null }
  ]
}
```

Error envelope (existing `{ "code", "message" }`):

| HTTP | code | When |
|------|------|------|
| 422 | `date_of_birth_required` | DOB missing and not on file |
| 422 | `invalid_reference` | Missing/malformed reference_id |
| 409 | `kyc_tier_precondition` | Wrong current tier for this step |
| 502 | `kyc_provider_error` | Dojah unreachable/5xx (record left `pending`; retry via `GET /kyc/status`) |

Note: a `failed` biometric outcome (face mismatch, liveness fail, id not
verified) returns **HTTP 200** with `status:"failed"` + `failure_reason` — it is
a valid result, not an error.

### 3.5 Migrations — `alembic/versions/`

1. **Add `tier_3` to `kyc_level_enum`** — `ALTER TYPE ... ADD VALUE` cannot run
   in a transaction block; use the non-transactional pattern so alembic doesn't
   wrap it.
2. **Create `kyc_records`:**

| Column | Type | Notes |
|--------|------|-------|
| id | UUID | PK |
| user_id | UUID | FK → users, indexed |
| verification_type | VARCHAR(8) | `bvn` / `nin` |
| provider | VARCHAR(16) | `dojah` |
| provider_reference | VARCHAR | Dojah reference (unique, idempotency key) |
| status | VARCHAR(12) | `pending` / `success` / `failed` |
| liveness_passed | BOOLEAN | nullable |
| face_match | BOOLEAN | nullable |
| tier_before | INT | |
| tier_after | INT | nullable |
| masked_id | VARCHAR(8) | last 2 digits only |
| failure_reason | TEXT | nullable |
| created_at / updated_at | TIMESTAMPTZ | TimestampMixin |

**No raw BVN/NIN, no selfie, no Dojah raw payload persisted.**

### 3.6 Wallet cap update — `app/services/wallet_service.py`

```
_KYC_CAPS = {
  tier_0: 50_000,
  tier_1: 300_000,   # was 200_000
  tier_2: 500_000,
  tier_3: None,      # unlimited → skip cap check when None
}
```
When cap is `None`, bypass the `new_balance > cap` check. `KycCapExceeded`
unchanged for capped tiers. `/auth/me` already emits numeric `kyc_level`
(`tier_3 → 3`); no change beyond the enum supporting the value.

## 4. Mobile design (timpbills)

### 4.1 Dojah SDK

- Add the **Dojah Flutter SDK** dependency (confirm exact package/version).
- Wrap it behind a `DojahKycService` interface (launch widget for a
  `verification_type` with pre-filled user data → returns `reference_id` +
  client status). Wrapping keeps the SDK mockable in tests and isolates the
  open items.
- **Permissions:** iOS `NSCameraUsageDescription` (Info.plist), Android
  `CAMERA` (manifest). Handle permission-denied gracefully.

### 4.2 Upgrade `KycTierPage` (kept)

- 4-tier limits (per-txn / daily / max) from §2.
- Step 3 actionable: "Verify BVN" (tier 1 → `/profile/kyc/bvn`), "Verify NIN"
  (tier 2 → `/profile/kyc/nin`). Remove "Coming soon".
- Bottom CTA routes by tier.

### 4.3 New screens (from `handoff/src/timpbills-kyc.jsx`, flat aesthetic)

- `KycBvnPage` / `KycNinPage` — branded entry (step indicator, 11-digit field,
  privacy/unlock content, DOB field per §4.4). CTA → launch `DojahKycService`
  widget for selfie + liveness + face-match.
- On widget callback → `POST /kyc/verify { verification_type, reference_id, date_of_birth? }`
  → render verification **state** in-page: brief loading → **success** / **failed**.
  Failed copy is outcome-specific (face mismatch / liveness / id-not-verified /
  DOB). **Pending** is the slow-path fallback (poll `GET /kyc/status`, "Refresh
  status" ghost button).
- `KycLimitReachedSheet` — **full-width** bottom sheet, Now/After compare,
  "Verify BVN to continue" + "Maybe later".

### 4.4 DOB handling

Form reads `me.dateOfBirth`: null → required date-picker field (sent as
`date_of_birth`, also passed into the widget config); set → prefill.

### 4.5 Wire limit-reached

Fund-wallet already receives `422 KYC_LIMIT_EXCEEDED` → present
`KycLimitReachedSheet` → route to `/profile/kyc/bvn`.

### 4.6 Data layer

- DTOs: `KycVerifyResponse`, `KycStatusResponse` (freezed + json).
- `KycRepository` (real) + `FakeKycRepository` (mirrors `fake_auth_repository`).
- Riverpod controller; **invalidate `meControllerProvider` on success**.
- Routes: add `/profile/kyc/bvn`, `/profile/kyc/nin`.

## 5. Data flow (BVN happy path)

```
KycTierPage → KycBvnPage → [DOB field if me.dateOfBirth == null]
  → launch Dojah widget (selfie + liveness + face-match + BVN) → reference_id
  → POST /kyc/verify { "bvn", reference_id, date_of_birth? }
  → KycService: gate tier_1, resolve DOB, upsert record=pending
  → fetch_verification(reference_id) server-side (authoritative)
  → validate id_verified ∧ liveness_passed ∧ face_match ∧ identity-matches-user
       pass → record=success, kyc_level=tier_2, cap=₦500k → success screen
              → invalidate me → app-wide tier refresh
       fail → record=failed(reason) → failed screen → retry
  (Dojah webhook independently reconciles the same reference, idempotently.)
```

NIN path is symmetric (tier_2 → tier_3, cap → unlimited).

## 6. Testing

**Backend**
- Tier gating (BVN needs tier_1; NIN needs tier_2; wrong tier → 409).
- DOB: required-when-missing (422), persist-when-supplied, prefill-when-set.
- Validation matrix: success only when id_verified ∧ liveness ∧ face_match ∧
  type ∧ identity-match; each false → `failed` with the right `failure_reason`.
- Idempotency: endpoint + webhook for the same reference apply once.
- `FakeKycProvider` deterministic by reference (`PASS*`/`FAILFACE*`/`FAILLIVE*`/`PENDING*`).
- Webhook signature verification (valid/invalid).
- Wallet-cap tests updated: tier_1 = ₦300k, tier_3 unlimited.
- Migration smoke: enum has `tier_3`, `kyc_records` present.

**Mobile**
- `KycTierPage` renders 4 tiers + limits; step 3 actionable per tier.
- BVN/NIN form: 11-digit validation, conditional DOB required vs prefilled.
- `DojahKycService` mocked: success/failed/pending callbacks drive the right state.
- Permission-denied path handled.
- `KycLimitReachedSheet` full-width; routes to BVN.
- Controller invalidates `me` on success.

## 7. Parallel execution

Contract (§3.4) is frozen. Two agents run concurrently:

- **backend-engineer:** Dojah adapter (base/client/fake/factory/schemas/signature),
  config, `KycService.confirm_verification`, `/kyc/verify` + `/kyc/status` +
  `/kyc/webhook`, migrations (tier_3 + kyc_records), wallet-cap update, tests.
- **mobile-engineer:** Dojah SDK + `DojahKycService` wrapper + permissions,
  `KycTierPage` upgrade, BVN/NIN pages + state screens, DOB conditional field,
  `KycLimitReachedSheet` + fund-flow wiring, `KycRepository` + `FakeKycRepository`
  + controller + DTOs, routes, tests.

Mobile mocks the Dojah SDK behind `DojahKycService` and stubs `FakeKycRepository`
against the frozen contract, so neither side blocks the other.
