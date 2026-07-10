# KYC BVN/NIN via Dojah (Liveness + Face-Match) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship Tier 1→2 (BVN) and Tier 2→3 (NIN) KYC upgrades, each gated by a real Dojah selfie + liveness + 1:1 face-match, with server-side confirmation and a signed webhook backstop.

**Architecture:** Backend exposes a Dojah `KycProvider` adapter (real `DojahClient`, test-only `FakeKycProvider`) behind a `KycService` with a `start`→`confirm` reference flow; mobile runs the Dojah Flutter widget and calls the backend to confirm. The client callback is never trusted — the backend fetches Dojah's authoritative result by reference (and reconciles via a signed webhook). See spec: `docs/superpowers/specs/2026-07-10-kyc-bvn-nin-dojah-design.md`.

**Tech Stack:** FastAPI + SQLAlchemy + Alembic + Postgres (backend); Flutter + Riverpod + go_router + freezed + `flutter_dojah_kyc` (mobile). UI source of truth: `handoff/src/timpbills-kyc.jsx` (in the Claude Design project).

## Global Constraints

- **Frozen API contract** (spec §3.4) — both tracks build against it verbatim; do not change field names.
- **Tiers are 4:** `tier_0/1/2/3`; numeric in API responses (`kyc_level` int).
- **Enforced wallet caps only:** tier_0 ₦50,000 / tier_1 ₦300,000 / tier_2 ₦500,000 / tier_3 **unlimited (None)**. Per-txn + daily are **display-only**.
- **No raw PII persisted:** never store BVN/NIN, selfie, or Dojah raw payload. `masked_id` = last 2 digits only.
- **Real Dojah is the working path;** `FakeKycProvider` + mocked SDK are **test-only** (selected by `FORCE_FAKE_PROVIDERS` / unset keys).
- **Confirm authority = server-side** (`fetch_verification` by reference + signed `x-dojah-signature` HMAC-SHA256 webhook). Both write idempotently to the same `kyc_records` row (unique `provider_reference`).
- **Face-match pass rule:** `face_match = confidence >= DOJAH_FACE_MATCH_THRESHOLD` (default 70).
- **KYC endpoints are authenticated but require no `pin_token`** (not a money op).
- **DOB required before verification:** expose a field when `user.date_of_birth` is null (persist it); prefill when set. Passed to widget `userData.dob`.
- **Aesthetic:** flat, no gradients (keep the existing `KycTierPage` look); every bottom sheet full-width.
- **Commits:** no `Co-Authored-By` / AI attribution trailers.

---

# Track A — Backend (timpbills-backend)

Run by a `backend-engineer` agent. Postgres must be up (`docker compose up -d db` or the project's local DB). Tests: `poetry run pytest`.

### Task A1: Dojah config settings

**Files:**
- Modify: `app/core/config.py` (add Dojah settings alongside the existing provider block)
- Test: `tests/core/test_config_dojah.py`

**Interfaces:**
- Produces: `settings.DOJAH_API_KEY`, `DOJAH_APP_ID`, `DOJAH_PUBLIC_KEY`, `DOJAH_BVN_WIDGET_ID`, `DOJAH_NIN_WIDGET_ID`, `DOJAH_WEBHOOK_SECRET` (all `str | None = None`), `DOJAH_BASE_URL: str = "https://api.dojah.io"`, `DOJAH_ENVIRONMENT: str = "sandbox"`, `DOJAH_FACE_MATCH_THRESHOLD: int = 70`.

- [ ] **Step 1: Write the failing test**
```python
# tests/core/test_config_dojah.py
from app.core.config import settings

def test_dojah_defaults():
    assert settings.DOJAH_BASE_URL == "https://api.dojah.io"
    assert settings.DOJAH_ENVIRONMENT == "sandbox"
    assert settings.DOJAH_FACE_MATCH_THRESHOLD == 70
    # secrets default to None so the factory picks the fake in tests
    assert settings.DOJAH_API_KEY is None
```
- [ ] **Step 2: Run — expect FAIL** (`poetry run pytest tests/core/test_config_dojah.py -v`) — AttributeError.
- [ ] **Step 3: Add the fields** to the `Settings` class, mirroring the VTPass/Paystack block. (`.env.example` already carries these — do not duplicate.)
- [ ] **Step 4: Run — expect PASS.**
- [ ] **Step 5: Commit** — `feat(kyc): add Dojah config settings`.

### Task A2: `KycVerificationResult` schema + `KycProvider` protocol + `FakeKycProvider`

**Files:**
- Create: `app/integrations/dojah/__init__.py`, `app/integrations/dojah/schemas.py`, `app/integrations/dojah/base.py`, `app/integrations/dojah/fake.py`
- Test: `tests/integrations/dojah/test_fake_provider.py`

**Interfaces:**
- Produces:
  - `KycVerificationResult` (pydantic): `verification_type: Literal["bvn","nin"]`, `status: Literal["success","pending","failed"]`, `id_verified: bool`, `liveness_passed: bool`, `face_match: bool`, `face_match_confidence: int`, `masked_id: str`, `provider_reference: str`, `identity_name: str | None`, `identity_dob: date | None`, `failure_reason: str | None`.
  - `KycProvider` protocol: `def fetch_verification(self, *, reference_id: str) -> KycVerificationResult`.
  - `FakeKycProvider` implementing it, **deterministic by reference prefix**: `PASS`→all true (confidence 95), `FAILFACE`→`face_match=False, confidence=40`, `FAILLIVE`→`liveness_passed=False`, `PENDING`→`status="pending"`. `verification_type` inferred from the reference containing `-BVN-`/`-NIN-`.

- [ ] **Step 1: Write the failing test**
```python
# tests/integrations/dojah/test_fake_provider.py
from datetime import date
from app.integrations.dojah.fake import FakeKycProvider

def test_fake_pass_bvn():
    r = FakeKycProvider().fetch_verification(reference_id="PASS-BVN-123")
    assert r.status == "success" and r.id_verified and r.liveness_passed
    assert r.face_match and r.face_match_confidence >= 70
    assert r.verification_type == "bvn"

def test_fake_face_fail():
    r = FakeKycProvider().fetch_verification(reference_id="FAILFACE-NIN-9")
    assert r.status == "failed" and r.face_match is False
    assert r.face_match_confidence < 70

def test_fake_pending():
    r = FakeKycProvider().fetch_verification(reference_id="PENDING-BVN-1")
    assert r.status == "pending"
```
- [ ] **Step 2: Run — expect FAIL** (module missing).
- [ ] **Step 3: Implement** `schemas.py` (the model), `base.py` (the `Protocol`), `fake.py` (deterministic logic mapping prefix→fields; `masked_id="•••••••••17"` style last-2).
- [ ] **Step 4: Run — expect PASS.**
- [ ] **Step 5: Commit** — `feat(kyc): Dojah result schema, provider protocol, fake double`.

### Task A3: `DojahClient` (real fetch) + webhook signature + factory

**Files:**
- Create: `app/integrations/dojah/client.py`, `app/integrations/dojah/signature.py`, `app/integrations/dojah/factory.py`
- Test: `tests/integrations/dojah/test_signature.py`, `tests/integrations/dojah/test_factory.py`

**Interfaces:**
- Consumes: `settings.*` (A1), `KycVerificationResult`/`KycProvider` (A2).
- Produces:
  - `DojahClient` implementing `KycProvider` — `fetch_verification` GETs Dojah's verification-status endpoint (path isolated here; headers `Authorization: <DOJAH_API_KEY>`, `AppId: <DOJAH_APP_ID>`), maps Dojah status (`Completed/Pending/Failed/Abandoned`) → `success/pending/failed`, extracts component results (id verified, liveness pass, face-match bool + confidence), derives `face_match = confidence >= settings.DOJAH_FACE_MATCH_THRESHOLD`.
  - `verify_dojah_signature(raw_body: bytes, signature: str) -> bool` — HMAC-SHA256 of `raw_body` with `DOJAH_WEBHOOK_SECRET`, constant-time compare (mirror `app/integrations/paystack/signature.py`).
  - `get_kyc_provider() -> KycProvider` — returns `FakeKycProvider` when `settings.FORCE_FAKE_PROVIDERS` or `settings.DOJAH_API_KEY is None`, else `DojahClient`.

- [ ] **Step 1: Write failing tests**
```python
# tests/integrations/dojah/test_signature.py
import hashlib, hmac
from app.integrations.dojah.signature import verify_dojah_signature
from app.core.config import settings

def test_valid_signature(monkeypatch):
    monkeypatch.setattr(settings, "DOJAH_WEBHOOK_SECRET", "shh")
    body = b'{"reference_id":"PASS-BVN-1"}'
    sig = hmac.new(b"shh", body, hashlib.sha256).hexdigest()
    assert verify_dojah_signature(body, sig) is True
    assert verify_dojah_signature(body, "deadbeef") is False
```
```python
# tests/integrations/dojah/test_factory.py
from app.integrations.dojah.factory import get_kyc_provider
from app.integrations.dojah.fake import FakeKycProvider
from app.core.config import settings

def test_factory_uses_fake_when_forced(monkeypatch):
    monkeypatch.setattr(settings, "FORCE_FAKE_PROVIDERS", True)
    assert isinstance(get_kyc_provider(), FakeKycProvider)
```
- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3: Implement** the three files. For `client.py`, keep the exact Dojah endpoint path + field mapping in one private method so it's the only thing to revise when creds/docs are confirmed (spec §1 open items).
- [ ] **Step 4: Run — expect PASS.**
- [ ] **Step 5: Commit** — `feat(kyc): DojahClient, webhook signature, provider factory`.

### Task A4: `KycRecord` model + migration (tier_3 enum + kyc_records)

**Files:**
- Create: `app/db/models/kyc_record.py`, `alembic/versions/202607101000_add_tier3_and_kyc_records.py`
- Modify: `app/db/models/user.py:29-44` (add `tier_3 = "tier_3"` to `KycLevel`), `app/db/models/__init__.py` (register model if applicable)
- Test: `tests/db/test_kyc_record_model.py`

**Interfaces:**
- Consumes: `KycLevel` (existing).
- Produces: `KycRecord` ORM (columns per spec §3.5), `KycLevel.tier_3`. `KycRecord.status` values `pending/success/failed`.

- [ ] **Step 1: Write failing test**
```python
# tests/db/test_kyc_record_model.py
from app.db.models.user import KycLevel
from app.db.models.kyc_record import KycRecord

def test_tier3_exists():
    assert KycLevel.tier_3.numeric == 3

def test_kyc_record_columns():
    cols = {c.name for c in KycRecord.__table__.columns}
    assert {"verification_type","provider","provider_reference","status",
            "liveness_passed","face_match","face_match_confidence",
            "tier_before","tier_after","masked_id","failure_reason"} <= cols
```
- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3: Implement** `KycLevel.tier_3`, the `KycRecord` model (UUID pk, FK user_id indexed, `provider_reference` unique, TimestampMixin). Write the migration: create `kyc_records`, and add the enum value with the **non-transactional** pattern:
```python
def upgrade():
    op.execute("COMMIT")
    op.execute("ALTER TYPE kyc_level_enum ADD VALUE IF NOT EXISTS 'tier_3'")
    op.create_table("kyc_records", ...)  # columns per spec §3.5
```
- [ ] **Step 4: Run** model test + `alembic upgrade head` against a scratch DB. Expect PASS + clean migration.
- [ ] **Step 5: Commit** — `feat(kyc): KycRecord model + tier_3 enum + kyc_records migration`.

### Task A5: Wallet cap update (4 tiers, unlimited tier_3)

**Files:**
- Modify: `app/services/wallet_service.py:15-19` (`_KYC_CAPS`) and the credit cap-check (`app/services/wallet_service.py:71-80`)
- Test: `tests/services/test_wallet_kyc_caps.py` (extend existing wallet cap tests)

**Interfaces:**
- Produces: `_KYC_CAPS = {tier_0: 50_000, tier_1: 300_000, tier_2: 500_000, tier_3: None}`; credit skips the cap check when the resolved cap is `None`.

- [ ] **Step 1: Write failing tests**
```python
def test_tier1_cap_is_300k(...):  # credit up to 300_000 ok, 300_001 raises KycCapExceeded
def test_tier3_unlimited(...):    # credit 10_000_000 succeeds, no KycCapExceeded
```
- [ ] **Step 2: Run — expect FAIL** (tier_1 still 200k; tier_3 KeyError).
- [ ] **Step 3: Implement** the dict + `if cap is not None and new_balance > cap: raise KycCapExceeded`.
- [ ] **Step 4: Run — expect PASS.** Also run the existing wallet suite to catch the ₦200k→₦300k change fallout.
- [ ] **Step 5: Commit** — `feat(wallet): 4-tier caps, unlimited tier_3`.

### Task A6: `KycService` — start + confirm

**Files:**
- Create: `app/services/kyc_service.py`
- Test: `tests/services/test_kyc_service.py`

**Interfaces:**
- Consumes: `get_kyc_provider` (A3), `KycRecord`/`KycLevel` (A4), `WalletService` cap refresh (A5).
- Produces:
  - `KycService.start_verification(user, verification_type, date_of_birth=None) -> str` (returns `reference_id`; raises `KycTierPrecondition`, `DobRequired`).
  - `KycService.confirm_verification(reference_id, *, source: str="api") -> KycRecord` (idempotent; validates the full matrix; upgrades tier + refreshes cap on pass).
  - Exceptions `KycTierPrecondition`, `DobRequired`, `UnknownReference`, `KycProviderError`.

- [ ] **Step 1: Write failing tests** (use `FakeKycProvider` via `FORCE_FAKE_PROVIDERS=True`):
```python
def test_start_requires_tier1_for_bvn(...):        # tier_0 user → KycTierPrecondition
def test_start_dob_required_when_missing(...):     # no dob on user, none passed → DobRequired
def test_start_persists_supplied_dob(...):         # dob saved to user
def test_confirm_pass_upgrades_to_tier2(...):      # PASS-BVN ref → user.kyc_level tier_2, cap 500k
def test_confirm_facefail_keeps_tier(...):         # FAILFACE ref → record failed, tier unchanged
def test_confirm_is_idempotent(...):               # second confirm on same ref = no-op
```
- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3: Implement** per spec §3.2 (mint `reference_id` = `f"KYC-{type.upper()}-{token}"` so the fake + client can round-trip; validation matrix; idempotency guard on `status == "success"`).
- [ ] **Step 4: Run — expect PASS.**
- [ ] **Step 5: Commit** — `feat(kyc): KycService start/confirm with validation + tier upgrade`.

### Task A7: KYC endpoints + request/response schemas

**Files:**
- Create: `app/api/v1/endpoints/kyc.py`, `app/schemas/kyc.py`
- Modify: `app/api/v1/router.py` (register the `/kyc` router)
- Test: `tests/api/test_kyc_endpoints.py`

**Interfaces:**
- Consumes: `KycService` (A6), `get_current_user`, `settings` (A1), `verify_dojah_signature` (A3).
- Produces the frozen contract (spec §3.4): `GET /kyc/config`, `POST /kyc/verify/start`, `POST /kyc/verify/confirm`, `GET /kyc/status`, `POST /kyc/webhook`.

- [ ] **Step 1: Write failing tests** (async client, authed user fixture, `FORCE_FAKE_PROVIDERS`):
```python
def test_config_returns_widget_ids(...):           # 200, keys app_id/public_key/bvn_widget_id/nin_widget_id
def test_start_then_confirm_bvn_pass(...):          # start→reference; confirm PASS→ status success, tier 2
def test_confirm_facefail_returns_200_failed(...):  # 200 status "failed", failure_reason "face_mismatch"
def test_start_wrong_tier_409(...):
def test_webhook_bad_signature_401(...):
def test_webhook_reconciles_pending(...):
```
- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3: Implement** the router + pydantic schemas. Map service exceptions → the error table (spec §3.4). Webhook: read raw body, `verify_dojah_signature`, then `KycService.confirm_verification(ref, source="webhook")`; always 200 to Dojah after signature passes.
- [ ] **Step 4: Run — expect PASS.** Run full backend suite.
- [ ] **Step 5: Commit** — `feat(kyc): /kyc config, verify start/confirm, status, webhook`.

### Task A8: `/auth/me` tier_3 regression

**Files:**
- Test: `tests/api/test_auth_me_tier3.py`

- [ ] **Step 1:** Test a `tier_3` user → `/auth/me` returns `kyc_level == 3`.
- [ ] **Step 2: Run — expect PASS** (existing `.numeric` already handles it; this locks it).
- [ ] **Step 3: Commit** — `test(auth): /auth/me emits kyc_level 3 for tier_3`.

---

# Track B — Mobile (timpbills)

Run by a `mobile-engineer` agent, in parallel with Track A against the frozen contract. Tests: `flutter test`. Codegen: `dart run build_runner build --delete-conflicting-outputs`. UI reference: `handoff/src/timpbills-kyc.jsx`.

### Task B1: KYC DTOs

**Files:**
- Create: `lib/features/kyc/data/dto/kyc_config.dart`, `kyc_verify_response.dart`, `kyc_status_response.dart` (+ generated `.freezed.dart`/`.g.dart`)
- Test: `test/features/kyc/dto_test.dart`

**Interfaces:**
- Produces: `KycConfig { appId, publicKey, bvnWidgetId, ninWidgetId, environment }`; `KycVerifyResponse { status, tier(int), verificationType, reference, livenessPassed, faceMatch, failureReason }`; `KycStatusResponse { tier(int), records: List<KycRecordDto> }`. JSON keys per spec §3.4 (snake_case via `@JsonKey`).

- [ ] **Step 1:** Failing `fromJson` test with a sample `KycVerifyResponse` payload (spec §3.4 JSON).
- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3:** Implement freezed DTOs; run build_runner.
- [ ] **Step 4: Run — expect PASS.**
- [ ] **Step 5: Commit** — `feat(kyc): response/config DTOs`.

### Task B2: `KycRepository` + `FakeKycRepository`

**Files:**
- Create: `lib/features/kyc/data/kyc_repository.dart`, `lib/features/kyc/data/fake_kyc_repository.dart`, `lib/features/kyc/data/kyc_repository_provider.dart`
- Test: `test/features/kyc/fake_repository_test.dart`

**Interfaces:**
- Consumes: DTOs (B1), the app's dio/http client + auth interceptor (mirror `lib/features/auth/data/*_repository.dart`).
- Produces: abstract `KycRepository` — `Future<KycConfig> config()`, `Future<String> startVerification({required String type, DateTime? dob})`, `Future<KycVerifyResponse> confirm(String referenceId)`, `Future<KycStatusResponse> status()`. `FakeKycRepository` returns canned values keyed by a settable outcome (success/faceFail/pending) for widget tests.

- [ ] **Step 1:** Failing test that `FakeKycRepository().confirm("PASS-BVN-1")` yields `status=="success", tier==2`.
- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3:** Implement both + the riverpod provider (mirror `fake_auth_repository.dart` selection pattern).
- [ ] **Step 4: Run — expect PASS.**
- [ ] **Step 5: Commit** — `feat(kyc): repository + fake repository`.

### Task B3: `DojahKycService` SDK wrapper + permissions

**Files:**
- Create: `lib/features/kyc/data/dojah_kyc_service.dart`, `lib/features/kyc/data/fake_dojah_kyc_service.dart`
- Modify: `pubspec.yaml` (add `flutter_dojah_kyc`), `ios/Runner/Info.plist` (`NSCameraUsageDescription`, `NSMicrophoneUsageDescription`), `android/app/src/main/AndroidManifest.xml` (`<uses-permission android:name="android.permission.CAMERA"/>`)
- Test: `test/features/kyc/fake_dojah_service_test.dart`

**Interfaces:**
- Produces: abstract `DojahKycService` — `Future<DojahResult> launch({ required String widgetId, required String referenceId, required KycConfig config, String? dob, String? firstName, String? lastName, String? govId })` returning `DojahResult { referenceId, status(success/error/closed) }`. `FakeDojahKycService` returns a preset result (mockable in widget tests).

- [ ] **Step 1:** Failing test that `FakeDojahKycService(result: success)` returns `status==success` with the passed reference.
- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3:** Implement the wrapper around `flutter_dojah_kyc` (`DojahKYC` with `appId`, `publicKey`, `widget_id`, `userData`, `govData`, `referenceId`; map `onSuccess/onError/onClose`) + the fake. Add the dependency + permissions. Confirm the exact SDK param surface for the installed version (spec §1 open item).
- [ ] **Step 4: Run — expect PASS** (fake path; real SDK smoke-tested on device later).
- [ ] **Step 5: Commit** — `feat(kyc): Dojah SDK wrapper + camera permissions`.

### Task B4: KYC controller

**Files:**
- Create: `lib/features/kyc/presentation/controllers/kyc_controller.dart`
- Test: `test/features/kyc/kyc_controller_test.dart`

**Interfaces:**
- Consumes: `KycRepository` (B2), `DojahKycService` (B3), `meControllerProvider`.
- Produces: `KycController` exposing `verify({required String type, DateTime? dob})` which orchestrates start→launch widget→confirm, emits states `KycState.idle/loading/success/failed(reason)/pending`, and on success `ref.invalidate(meControllerProvider)`.

- [ ] **Step 1:** Failing test wiring `FakeKycRepository` + `FakeDojahKycService(success)` → controller ends in `success` and invalidates `me`.
- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3:** Implement the orchestration + state machine.
- [ ] **Step 4: Run — expect PASS.**
- [ ] **Step 5: Commit** — `feat(kyc): verification controller (start→widget→confirm)`.

### Task B5: Routes

**Files:**
- Modify: `lib/core/routes/routes.dart` (add `kycBvn = '/profile/kyc/bvn'`, `kycNin = '/profile/kyc/nin'`), `lib/core/routes/app_router.dart` (register both, building the pages from B6)
- Test: covered by B6 widget tests (navigation).

- [ ] **Step 1:** Add the route constants + router entries pointing at `KycBvnPage`/`KycNinPage`.
- [ ] **Step 2: Run** `flutter analyze` — expect clean.
- [ ] **Step 3: Commit** — `feat(kyc): bvn/nin routes`.

### Task B6: `KycBvnPage` / `KycNinPage` (entry + conditional DOB + states)

**Files:**
- Create: `lib/features/kyc/presentation/pages/kyc_bvn_page.dart`, `kyc_nin_page.dart`, `lib/features/kyc/presentation/widgets/kyc_state_view.dart`
- Test: `test/features/kyc/kyc_bvn_page_test.dart`

**Interfaces:**
- Consumes: `KycController` (B4), `meControllerProvider` (for `dateOfBirth` prefill). Visual spec: `KycBvnScreen`/`KycNinScreen`/`KycStateScreen` in `handoff/src/timpbills-kyc.jsx` (step indicator, 11-digit field, privacy/unlock content; pending/success/failed states).

- [ ] **Step 1: Write failing widget tests**
```dart
testWidgets('DOB field shown when me.dateOfBirth is null', ...);
testWidgets('DOB field prefilled/hidden when set', ...);
testWidgets('11-digit validation blocks CTA until valid', ...);
testWidgets('success state renders after confirm', ...);  // FakeDojahKycService(success)
testWidgets('face-fail renders failed state with retry', ...);
```
- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3:** Build both pages (flat aesthetic, reuse `KycTierPage` tokens) + shared `KycStateView` (loading/success/failed/pending; pending polls `status()`). On CTA: call `controller.verify(type, dob)`.
- [ ] **Step 4: Run — expect PASS.**
- [ ] **Step 5: Commit** — `feat(kyc): BVN/NIN pages with conditional DOB + state views`.

### Task B7: Upgrade `KycTierPage`

**Files:**
- Modify: `lib/features/profile/presentation/pages/kyc_tier_page.dart`
- Test: `test/features/profile/kyc_tier_page_test.dart`

**Interfaces:**
- Consumes: `meControllerProvider`, routes (B5). Limits table per spec §2.

- [ ] **Step 1: Write failing tests**
```dart
testWidgets('renders 4 tiers with correct per-txn/daily/max', ...);
testWidgets('tier1 user: step 3 = Verify BVN, taps → /profile/kyc/bvn', ...);
testWidgets('tier2 user: step = Verify NIN, taps → /profile/kyc/nin', ...);
```
- [ ] **Step 2: Run — expect FAIL** (current page locks step 3 as "Coming soon").
- [ ] **Step 3:** Update `_LimitsCard` to the 4-tier table (Tier 3 per-txn ₦5M, unlimited balance), make the BVN/NIN step actionable + route the CTA by tier (drop "Coming soon").
- [ ] **Step 4: Run — expect PASS.**
- [ ] **Step 5: Commit** — `feat(kyc): actionable 4-tier KycTierPage`.

### Task B8: `KycLimitReachedSheet` + fund-wallet wiring

**Files:**
- Create: `lib/features/kyc/presentation/widgets/kyc_limit_reached_sheet.dart`
- Modify: the fund-wallet controller/page that handles the `422 KYC_LIMIT_EXCEEDED` response (locate via `grep -rn "KYC_LIMIT_EXCEEDED" lib`)
- Test: `test/features/kyc/kyc_limit_reached_test.dart`

**Interfaces:**
- Consumes: routes (B5). Visual spec: `KycLimitReachedScreen` in the handoff. Must be **full-width** (standing rule).

- [ ] **Step 1: Write failing test** — presenting the sheet shows the Now/After compare; tapping "Verify BVN to continue" navigates to `/profile/kyc/bvn`; the sheet fills full width.
- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3:** Build the sheet (`showModalBottomSheet` full-width) + trigger it where the fund flow catches `KYC_LIMIT_EXCEEDED`.
- [ ] **Step 4: Run — expect PASS.**
- [ ] **Step 5: Commit** — `feat(kyc): limit-reached upgrade sheet wired into fund flow`.

---

## Integration checkpoint (after both tracks land)

- [ ] Backend running with `FORCE_FAKE_PROVIDERS=false` + sandbox Dojah keys in `.env`; mobile pointed at it. Drive Tier 1→2 on a device: enter BVN, complete the Dojah selfie/liveness, land on success, confirm `/auth/me` shows tier 2 and the wallet cap is ₦500k. Repeat NIN → tier 3. (Use `/verify` skill.)
- [ ] Verify a Dojah **webhook** hitting `/kyc/webhook` reconciles a record whose `/confirm` never arrived.

## Self-review notes (author)

- Spec coverage: config (A1), adapter+fake (A2), client+signature+factory (A3), model+migration (A4), caps (A5), service (A6), endpoints incl. config/webhook (A7), me regression (A8); mobile DTOs (B1), repo (B2), SDK wrapper+perms (B3), controller (B4), routes (B5), pages+DOB+states (B6), tier page (B7), limit sheet (B8). All spec §3–§4 sections mapped.
- Contract names (`reference_id`, `verification_type`, `face_match`, `liveness_passed`, `failure_reason`, `tier`) are consistent across A6/A7/B1/B2.
- Open items (exact Dojah endpoint path/field names, SDK param surface, Widget IDs) are isolated in `client.py` (A3) and `dojah_kyc_service.dart` (B3) so they don't block parallel work.
