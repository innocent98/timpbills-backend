# KYC — BVN / NIN Verification via Dojah (Tier 1→2→3) with Liveness + Face-Match

**Date:** 2026-07-10
**Status:** Approved design → implementation
**Scope:** Cross-stack (timpbills-backend + timpbills mobile)

## 1. Summary

Phone verification (Tier 0 → Tier 1) is already live. This work delivers:

- **Tier 1 → Tier 2** via **BVN** verification (Dojah)
- **Tier 2 → Tier 3** via **NIN** verification (Dojah)

Each upgrade requires **BVN/NIN + a live selfie with liveness detection and
1:1 face-match** against the photo on the government record. This is a **real
Dojah integration** wired to real credentials — not a stub. The
`FakeKycProvider` exists **only as the automated-test double** (the same way
Paystack/Termii/VTPass each ship a fake used solely by the test suite); every
real environment runs the real `DojahClient` + real Dojah Flutter SDK.

### Integration approach (grounded in Dojah's real API/SDK)

- **Mobile** embeds the **Dojah KYC Widget** via the Flutter SDK
  (`flutter_dojah_kyc` webview class `DojahKYC`, which accepts pre-filled
  `userData`/`govData` so our branded entry screen is the single point of
  number entry; `dojah_kyc_sdk_flutter` native is the fallback if the version in
  use supports pre-fill). The published **Widget ID** defines the flow steps
  (BVN/NIN + selfie + liveness + face-match). On completion the SDK returns a
  **reference** via `onSuccess`.
- Dojah is explicit: *"the final verification decision should never rely on SDK
  callbacks alone."* So the **backend independently confirms** the result
  server-side by reference and only then upgrades the tier. The client callback
  is a UX signal + carries the reference — never the source of truth.
- A **Dojah webhook** (`x-dojah-signature` = HMAC-SHA256 of the payload with our
  secret key — same pattern as the existing Paystack `signature.py`) is the
  source-of-truth reconciliation backstop. Synchronous confirm-by-reference
  drives the UX; the webhook is the safety net if the app dies mid-flow. Both
  write **idempotently** to the same `kyc_records` row.

For the user the flow is effectively synchronous: the widget captures +
processes in seconds, the backend confirms, and a success/failed screen shows.
No user-facing polling on the happy path.

### Design decisions (locked)

| # | Decision | Choice |
|---|----------|--------|
| 1 | KYC landing screen | Keep + upgrade the flat `KycTierPage`; adopt only the net-new handoff screens. No gradient hub. |
| 2 | Real vs fake | **Real Dojah is the working path in every environment.** `FakeKycProvider` + mocked SDK are **test-only** doubles. |
| 3 | Biometrics | **Liveness + face-match on BOTH Tier 2 (BVN) and Tier 3 (NIN).** Built once via the Dojah Flutter SDK. |
| 4 | Confirm authority | **Server-side** Dojah result (confirm-by-reference / webhook) is the source of truth. SDK callback never trusted as primary. |
| 5 | Limits enforcement | Enforce 4-tier **max-wallet-balance** cap only. Per-txn + daily are **display-only** this sprint. |
| 6 | Record storage | **Minimal record, no raw PII, no selfie stored.** Reference + status + masked id + liveness/face booleans (+ face-match confidence) only. |
| 7 | Face-match pass rule | `face_match = confidence >= DOJAH_FACE_MATCH_THRESHOLD` (default 70, configurable). |
| 8 | DOB | Required before verification (user instruction). Expose a DOB field when unset; prefill when set. Passed to the widget `userData.dob` + persisted to profile. |
| 9 | Tier 3 per-txn display value | **₦5,000,000** (PRD.txt §13). |
| 10 | Webhook | **Kept** as the signed source-of-truth reconciliation backstop. |

### Flagged open items (non-blocking; isolated in the adapters)

- Confirm the exact Flutter SDK package + version param surface currently in use
  (pre-fill support), and the two **published Widget IDs** (BVN flow, NIN flow)
  from the Dojah dashboard.
- Confirm Dojah's verification-fetch-by-reference endpoint path + the component
  field names in its response (id verified / liveness / face-match+confidence).
  Fully isolated inside `DojahClient`.

## 2. Tier model

| Tier | Requirement | Per-txn (display) | Daily (display) | Max balance (**enforced**) |
|------|-------------|-------------------|-----------------|----------------------------|
| 0 | Registration only | ₦50,000 | ₦50,000 | ₦50,000 |
| 1 | Phone (OTP) — **live** | ₦50,000 | ₦50,000 | **₦300,000** (was ₦200,000) |
| 2 | BVN + selfie/liveness/face-match | ₦200,000 | ₦200,000 | ₦500,000 |
| 3 | NIN + selfie/liveness/face-match | ₦5,000,000 | ₦5,000,000 | **Unlimited** (new) |

Only **max-balance** is enforced; per-txn/daily are display-only. **PRD §4
(3-tier) is stale; §13's 4-tier table governs.** Tier 3 per-txn ₦5M over the
handoff's ₦200k.

## 3. Backend design (timpbills-backend)

### 3.1 Dojah adapter — `app/integrations/dojah/`

Mirrors `app/integrations/paystack/` (base / client / fake / factory / schemas /
signature):

- `base.py` — `KycProvider` protocol:
  - `fetch_verification(*, reference_id: str) -> KycVerificationResult`
- `schemas.py` — `KycVerificationResult`: `verification_type` (bvn/nin),
  `status` (Dojah: Completed/Pending/Failed/Abandoned → mapped to
  success/pending/failed), `id_verified: bool`, `liveness_passed: bool`,
  `face_match: bool`, `face_match_confidence: int`, `masked_id: str` (last 2),
  `provider_reference: str`, `identity` (name/dob for cross-check),
  `failure_reason: str | None`.
- `client.py` — `DojahClient.fetch_verification` (real HTTP; auth via
  `DOJAH_API_KEY` + `DOJAH_APP_ID` headers; endpoint path isolated here).
- `fake.py` — `FakeKycProvider`: **deterministic by reference_id**
  (`PASS*`→all-true, `FAILFACE*`→face_match=False low confidence,
  `FAILLIVE*`→liveness_passed=False, `PENDING*`→pending). **Test-only.**
- `factory.py` — `get_kyc_provider()` → `FakeKycProvider` only when
  `FORCE_FAKE_PROVIDERS` (tests) or keys unset; **real `DojahClient` otherwise.**
- `signature.py` — verify `x-dojah-signature` (HMAC-SHA256 of raw body with
  `DOJAH_WEBHOOK_SECRET`), constant-time compare.

**Config additions** (`app/core/config.py`) — see §8 for `.env.example`:
```
DOJAH_API_KEY: str | None = None          # secret (server-side calls)
DOJAH_APP_ID: str | None = None           # widget + API header
DOJAH_PUBLIC_KEY: str | None = None        # widget init (client-shared)
DOJAH_BVN_WIDGET_ID: str | None = None     # published BVN+selfie+liveness flow
DOJAH_NIN_WIDGET_ID: str | None = None     # published NIN+selfie+liveness flow
DOJAH_WEBHOOK_SECRET: str | None = None    # x-dojah-signature HMAC key
DOJAH_BASE_URL: str = "https://api.dojah.io"
DOJAH_ENVIRONMENT: str = "sandbox"         # sandbox | production
DOJAH_FACE_MATCH_THRESHOLD: int = 70       # 0-100 pass threshold
```

### 3.2 `KycService` — `app/services/kyc_service.py`

Split into **start** and **confirm** so the backend owns the reference:

`start_verification(user, verification_type, date_of_birth)`:
1. **Tier gate.** BVN requires `tier_1`; NIN requires `tier_2`. Else `409`.
2. **DOB resolution.** set → use; null+supplied → persist; null+none → `422`.
3. Create `kyc_records` row (status=`pending`, type, `tier_before`) with a
   backend-minted **`reference_id`** bound to the user. Return `reference_id`.
   (Raw BVN/NIN never reaches our backend — the app passes it to the widget's
   `govData` directly.)

`confirm_verification(reference_id, *, source)` (shared by the confirm endpoint
and the webhook):
4. Load the record by `reference_id` (must belong to the user for the endpoint path).
5. **Fetch the authoritative result** via the adapter.
6. **Validate:** mapped `status==success` ∧ `id_verified` ∧ `liveness_passed` ∧
   `face_match` (confidence ≥ threshold) ∧ `verification_type` matches ∧ returned
   identity matches the user. Any false → record `failed` + `failure_reason`,
   tier unchanged.
7. Pass → record `success`, `tier_after`, upgrade `kyc_level`, refresh wallet cap.
   **Idempotent** — endpoint + webhook converge; second apply is a no-op.

### 3.3 Endpoints — `app/api/v1/endpoints/kyc.py`

Authenticated (`get_current_user`) except the webhook. **No `pin_token`.**

| Method | Path | Body | Response |
|--------|------|------|----------|
| GET | `/api/v1/kyc/config` | — | `{ app_id, public_key, bvn_widget_id, nin_widget_id, environment }` (non-secret widget config; single source of truth for mobile) |
| POST | `/api/v1/kyc/verify/start` | `{ "verification_type": "bvn"\|"nin", "date_of_birth": "YYYY-MM-DD"? }` | `{ "reference_id": "KYC-7741Q" }` |
| POST | `/api/v1/kyc/verify/confirm` | `{ "reference_id": str }` | `KycVerifyResponse` |
| GET | `/api/v1/kyc/status` | — | `KycStatusResponse` (latest records + tier; slow-path/poll fallback) |
| POST | `/api/v1/kyc/webhook` | Dojah payload | 200; `x-dojah-signature` verified; reconciliation backstop |

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
  "failure_reason": null          // "face_mismatch" | "liveness_failed"
                                  //  | "id_not_verified" | "identity_mismatch"
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
| 422 | `DATE_OF_BIRTH_REQUIRED` | DOB missing and not on file (at `/start`) |
| 422 | `INVALID_VERIFICATION_TYPE` | verification_type not bvn/nin |
| 409 | `KYC_TIER_PRECONDITION` | Wrong current tier for this step |
| 404 | `UNKNOWN_REFERENCE` | reference_id not found / not this user (at `/confirm`) |
| 401 | `INVALID_SIGNATURE` | Bad `x-dojah-signature` (webhook) |
| 502 | `KYC_PROVIDER_ERROR` | Dojah unreachable/5xx (record left `pending`; retry via `/status`) |

(Error codes are UPPER_SNAKE to match the rest of the API. Response-body
`failure_reason` values remain lowercase domain strings — `face_mismatch`,
`liveness_failed`, `id_not_verified`, `identity_mismatch`.)

A `failed` biometric outcome returns **HTTP 200** `status:"failed"` +
`failure_reason` — a valid result, not an error.

### 3.5 Migrations — `alembic/versions/`

1. **Add `tier_3` to `kyc_level_enum`** — `ALTER TYPE ... ADD VALUE` can't run in
   a txn block; use the non-transactional pattern.
2. **Create `kyc_records`:**

| Column | Type | Notes |
|--------|------|-------|
| id | UUID | PK |
| user_id | UUID | FK → users, indexed |
| verification_type | VARCHAR(8) | `bvn` / `nin` |
| provider | VARCHAR(16) | `dojah` |
| provider_reference | VARCHAR | unique; backend-minted idempotency key |
| status | VARCHAR(12) | `pending` / `success` / `failed` |
| liveness_passed | BOOLEAN | nullable |
| face_match | BOOLEAN | nullable |
| face_match_confidence | INT | nullable (0–100) |
| tier_before | INT | |
| tier_after | INT | nullable |
| masked_id | VARCHAR(8) | last 2 digits only |
| failure_reason | TEXT | nullable |
| created_at / updated_at | TIMESTAMPTZ | TimestampMixin |

**No raw BVN/NIN, no selfie, no Dojah raw payload persisted.**

### 3.6 Wallet cap update — `app/services/wallet_service.py`

```
_KYC_CAPS = { tier_0: 50_000, tier_1: 300_000, tier_2: 500_000, tier_3: None }
```
`None` → skip the cap check (unlimited). `KycCapExceeded` unchanged for capped
tiers. `/auth/me` already emits numeric `kyc_level` (`tier_3 → 3`).

## 4. Mobile design (timpbills)

### 4.1 Dojah SDK + config

- Add the Dojah Flutter SDK (webview `flutter_dojah_kyc` preferred for pre-fill;
  confirm version). Wrap behind a mockable `DojahKycService` interface.
- On entry, fetch `GET /kyc/config` for `app_id` / `public_key` /
  `bvn_widget_id` / `nin_widget_id` — **one source of truth (backend .env)**,
  no secrets baked into the app.
- **Permissions:** iOS `NSCameraUsageDescription`, `NSMicrophoneUsageDescription`
  (Info.plist); Android `CAMERA` (manifest). Handle denial gracefully.

### 4.2 Upgrade `KycTierPage` (kept)

4-tier limits; step 3 actionable ("Verify BVN" tier 1 → `/profile/kyc/bvn`,
"Verify NIN" tier 2 → `/profile/kyc/nin`); CTA routes by tier.

### 4.3 New screens (`handoff/src/timpbills-kyc.jsx`, flat aesthetic)

- `KycBvnPage` / `KycNinPage` — branded entry (step indicator, 11-digit field,
  privacy/unlock content, DOB field per §4.4). CTA →
  `POST /kyc/verify/start` → launch `DojahKycService` widget with the returned
  `reference_id`, the type's `widget_id`, `userData.dob/name`, `govData.bvn/nin`.
- Widget `onSuccess` → `POST /kyc/verify/confirm { reference_id }` → render state
  in-page: loading → **success** / **failed** (outcome-specific copy). **Pending**
  = slow-path fallback (poll `GET /kyc/status`, "Refresh status" ghost). `onError`
  / `onClose` handled.
- `KycLimitReachedSheet` — **full-width** bottom sheet, Now/After compare,
  "Verify BVN to continue" + "Maybe later".

### 4.4 DOB handling

Form reads `me.dateOfBirth`: null → required date-picker (sent to `/start` +
passed to widget `userData.dob`); set → prefill.

### 4.5 Wire limit-reached

Fund-wallet's `422 KYC_LIMIT_EXCEEDED` → present `KycLimitReachedSheet` → route
to `/profile/kyc/bvn`.

### 4.6 Data layer

DTOs (`KycConfig`, `KycVerifyResponse`, `KycStatusResponse`, freezed+json);
`KycRepository` + `FakeKycRepository`; riverpod controller that **invalidates
`meControllerProvider` on success**; routes `/profile/kyc/bvn`, `/profile/kyc/nin`.

## 5. Data flow (BVN happy path)

```
KycTierPage → KycBvnPage → [DOB field if me.dateOfBirth == null]
  → POST /kyc/verify/start { "bvn", date_of_birth? } → reference_id (record=pending)
  → launch Dojah widget (bvn_widget_id, referenceId, userData.dob, govData.bvn)
      → selfie + liveness + face-match on-device → onSuccess(reference)
  → POST /kyc/verify/confirm { reference_id }
      → KycService.confirm → DojahClient.fetch_verification(reference_id)
      → validate status ∧ id_verified ∧ liveness ∧ face_match(conf≥70) ∧ identity
          pass → record=success, kyc_level=tier_2, cap=₦500k → success screen
                 → invalidate me → app-wide tier refresh
          fail → record=failed(reason) → failed screen → retry
  (Dojah webhook independently reconciles the same reference, idempotently.)
```

NIN is symmetric (tier_2 → tier_3, cap → unlimited).

## 6. Testing

**Backend**
- Tier gating; DOB required/persist/prefill.
- Validation matrix: success only when status ∧ id_verified ∧ liveness ∧
  face_match(conf≥threshold) ∧ type ∧ identity; each false → `failed` + correct reason.
- Idempotency: `/confirm` + webhook for the same reference apply once.
- `FakeKycProvider` deterministic by reference (PASS/FAILFACE/FAILLIVE/PENDING).
- Webhook signature verification (valid/invalid `x-dojah-signature`).
- Threshold boundary (conf == threshold passes; below fails).
- Wallet-cap tests: tier_1 = ₦300k, tier_3 unlimited. Migration smoke.

**Mobile**
- `KycTierPage` 4-tier render; step 3 actionable per tier.
- BVN/NIN form: 11-digit validation, conditional DOB.
- `DojahKycService` mocked: onSuccess/onError/onClose → right state; `/config`
  fetch; permission-denied path.
- `KycLimitReachedSheet` full-width → routes to BVN.
- Controller invalidates `me` on success.

## 7. Parallel execution

Contract (§3.4) frozen. Two agents concurrently:

- **backend-engineer:** Dojah adapter (base/client/fake/factory/schemas/signature),
  config, `KycService` start+confirm, `/kyc/config|verify/start|verify/confirm|
  status|webhook`, migrations (tier_3 + kyc_records), wallet-cap update, tests,
  and the `.env.example` / config entries.
- **mobile-engineer:** Dojah SDK + `DojahKycService` wrapper + permissions,
  `/kyc/config` fetch, `KycTierPage` upgrade, BVN/NIN pages + state screens, DOB
  field, `KycLimitReachedSheet` + fund-flow wiring, DTOs + `KycRepository` +
  `FakeKycRepository` + controller, routes, tests.

Mobile mocks the Dojah SDK behind `DojahKycService` and stubs `FakeKycRepository`
against the frozen contract; neither side blocks the other.

## 8. Credentials to provide (added to `.env.example`)

You must supply these from the Dojah dashboard (sandbox first, then production):

| Var | Secret? | Purpose |
|-----|---------|---------|
| `DOJAH_API_KEY` | **yes** | Server-side verification-status calls |
| `DOJAH_APP_ID` | no | Widget init + API header |
| `DOJAH_PUBLIC_KEY` | no | Widget init (served to mobile via `/kyc/config`) |
| `DOJAH_BVN_WIDGET_ID` | no | Published BVN + selfie + liveness flow ID |
| `DOJAH_NIN_WIDGET_ID` | no | Published NIN + selfie + liveness flow ID |
| `DOJAH_WEBHOOK_SECRET` | **yes** | `x-dojah-signature` HMAC key (Dojah secret key) |

Non-provided (sane defaults): `DOJAH_BASE_URL`, `DOJAH_ENVIRONMENT` (`sandbox`),
`DOJAH_FACE_MATCH_THRESHOLD` (`70`). Two Dojah **flows** must be published on the
dashboard — one BVN(+selfie+liveness), one NIN(+selfie+liveness) — to obtain the
two Widget IDs.
