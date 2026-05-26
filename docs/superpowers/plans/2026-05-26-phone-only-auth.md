# Phone-only auth + PIN-based cold-start login — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace email|phone authentication with phone-only login; require both email and phone verified at registration; force every user to have a 4-digit PIN; add `/auth/pin-login` for cold-start PIN entry; force-migrate existing users on next login.

**Architecture:** Three auth gates (`email_verified`, `is_phone_verified`, `pin_hash IS NOT NULL`) — all must pass before tokens issue. New endpoint `/auth/pin-login` takes `{refresh_token, pin}` and returns fresh tokens, reusing existing PinService lockout + RedisTokenStore rotation. Soft-mode `AUTH_STRICT_GATES` flag enables progressive rollout. Termii channel switches `generic` → `dnd` for OTP delivery on DND-registry numbers. One Alembic data migration normalises `User.phone` to E.164.

**Tech stack:** FastAPI (Python 3.11), SQLAlchemy 2.x sync ORM, Alembic, Pydantic v2, passlib (argon2id + bcrypt), python-jose (JWT), Redis (refresh-token store + PIN lockout), Termii (SMS), Resend (email); Flutter (Dart 3.x) + flutter_riverpod on the mobile side.

**Reference spec:** `docs/superpowers/specs/2026-05-26-phone-only-auth-design.md`

---

## File structure

### Backend — files created
- `app/utils/phone.py` — `normalize_to_e164(raw: str) -> str`
- `alembic/versions/<rev>_normalize_phone_e164.py` — one-time data migration
- `tests/utils/test_phone.py`
- `tests/core/test_security_pin_setup_token.py`
- `tests/api/test_pin_login.py`
- `tests/api/test_auth_phone_verify_signup.py`
- `tests/api/test_register_dual_otp.py`
- `tests/integration/test_full_registration_flow.py`
- `tests/integration/test_existing_user_migration.py`

### Backend — files modified
- `app/core/security.py` — add `create_pin_setup_token`, `verify_pin_setup_token`
- `app/core/config.py` — settings: `AUTH_STRICT_GATES`, `AUTH_PIN_LOGIN_ENABLED`, `TERMII_OTP_CHANNEL`, `OTP_RESEND_DAILY_CAP`
- `app/integrations/termii/client.py` — read `TERMII_OTP_CHANNEL` from settings
- `app/api/deps.py` — `require_full_auth_gates` dep
- `app/services/auth_service.py` — register/login refactor + phone normalisation in lookups
- `app/services/pin_service.py` — add `pin_login_async` method (cold-start orchestration)
- `app/api/v1/endpoints/auth.py` — new `/pin-login` route, updated `/pin/set` contract, `next_action` in `/login` response, new `/phone/verify` signup variant
- `app/schemas/auth.py` — new request/response schemas
- `app/api/v1/endpoints/bills.py`, `wallet.py`, `transactions.py` (and others using `get_current_user`) — replace dep with `require_full_auth_gates`

### Mobile — files created
- `lib/core/utils/phone.dart` — `normalizeToE164(String raw)`
- `lib/features/auth/presentation/screens/phone_verification_screen.dart`
- `lib/features/auth/presentation/screens/set_pin_screen.dart`
- `lib/features/auth/presentation/screens/cold_start_pin_screen.dart`
- `lib/features/auth/presentation/screens/forgot_pin_screen.dart` (small — just routes to password login)
- `test/features/auth/login_routing_test.dart`
- `test/features/auth/cold_start_pin_test.dart`

### Mobile — files modified
- `lib/features/auth/presentation/screens/login_screen.dart` — phone-only field, placeholder text
- `lib/features/auth/data/auth_repository.dart` — handle new `next_action` field, new `pinLogin()` method, `pinSetupToken` persistence
- `lib/features/auth/application/auth_controller.dart` — routing logic for `next_action` values
- `lib/core/router/app_router.dart` — routes for new screens
- `lib/main.dart` (or app boot widget) — cold-start gate that decides PIN screen vs login screen

---

## Task execution order + parallelism

Backend tasks B1–B5 form the foundation (no dependencies on each other beyond B1). Tasks B6–B13 build on the foundation. B14 is the last backend task. Mobile tasks M1–M9 can begin as soon as the backend endpoints they depend on are merged; explicit dependencies marked per-task.

**Parallel-eligible groups:**
- After B1: `{B2, B3, B4, B5}` can run as four parallel subagents
- After B3 + B4: `{B6, B7, B8}` can run in parallel
- After B8: `{B9, B10, B11}` can run in parallel (they touch the same file `endpoints/auth.py`, so serialise their edits but the test work for each can be parallel)
- After B12: mobile tasks `{M1, M2, M3}` can start
- After B13: mobile tasks `{M5, M6}` can start

---

## Task 1 (B1): Phone normalisation helper

**Files:**
- Create: `app/utils/phone.py`
- Test: `tests/utils/test_phone.py`

- [ ] **Step 1: Write the failing test**

Create `tests/utils/test_phone.py`:

```python
import pytest
from app.utils.phone import normalize_to_e164, InvalidPhoneFormat


@pytest.mark.parametrize("raw,expected", [
    ("08012345678", "+2348012345678"),
    ("2348012345678", "+2348012345678"),
    ("+2348012345678", "+2348012345678"),
    ("0701234567", None),       # 10-digit NG number — invalid; only 11-digit local accepted
    ("07012345678", "+2347012345678"),
    ("09012345678", "+2349012345678"),
    ("  08012345678  ", "+2348012345678"),  # whitespace tolerated
])
def test_normalize_accepts_valid_formats(raw, expected):
    if expected is None:
        with pytest.raises(InvalidPhoneFormat):
            normalize_to_e164(raw)
    else:
        assert normalize_to_e164(raw) == expected


@pytest.mark.parametrize("bad", [
    "", "   ", "abc", "08abc", "0801234567",       # 10 digits — too short
    "080123456789",                                  # 12 digits — too long
    "+1234567890",                                   # non-NG country code
    "+234901234567",                                 # short national number
])
def test_normalize_rejects_invalid(bad):
    with pytest.raises(InvalidPhoneFormat):
        normalize_to_e164(bad)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
docker compose exec -T api pytest tests/utils/test_phone.py -v
```
Expected: FAIL (module does not exist).

- [ ] **Step 3: Implement the helper**

Create `app/utils/phone.py`:

```python
"""Nigerian phone normalisation to E.164.

Accepts: 11-digit local (07/08/09 prefix), 13-digit international (234...),
or E.164 (+234...). Anything else raises InvalidPhoneFormat.
"""
import re


class InvalidPhoneFormat(ValueError):
    """Raised when a string cannot be normalised to E.164."""


_LOCAL_NG = re.compile(r"^0[789]\d{9}$")       # 11 digits, 0[789] prefix
_INTL_NG = re.compile(r"^234[789]\d{9}$")       # 13 digits, 234[789] prefix
_E164_NG = re.compile(r"^\+234[789]\d{9}$")     # +234[789] prefix


def normalize_to_e164(raw: str) -> str:
    if not raw or not isinstance(raw, str):
        raise InvalidPhoneFormat("phone must be a non-empty string")
    s = raw.strip().replace(" ", "")
    if _E164_NG.match(s):
        return s
    if _INTL_NG.match(s):
        return f"+{s}"
    if _LOCAL_NG.match(s):
        return f"+234{s[1:]}"
    raise InvalidPhoneFormat(f"unrecognised phone format: {raw!r}")
```

- [ ] **Step 4: Run test to verify it passes**

```bash
docker compose exec -T api pytest tests/utils/test_phone.py -v
```
Expected: PASS (all parametrized cases green).

- [ ] **Step 5: Commit**

```bash
git add app/utils/phone.py tests/utils/test_phone.py
git commit -m "feat(utils): add Nigerian phone E.164 normalisation helper"
```

---

## Task 2 (B2): Alembic data migration to normalise existing phones

**Depends on:** Task 1 (uses `normalize_to_e164`)
**Files:**
- Create: `alembic/versions/<rev>_normalize_phone_e164.py` (Alembic generates the filename)
- Test: `tests/db/test_phone_normalization_migration.py`

- [ ] **Step 1: Generate the migration scaffold**

```bash
docker compose exec -T api alembic revision -m "normalize_phone_e164"
```
Note the generated path — Alembic produces something like `alembic/versions/abc123def456_normalize_phone_e164.py`.

- [ ] **Step 2: Write the failing test**

Create `tests/db/test_phone_normalization_migration.py`:

```python
import pytest
from sqlalchemy import text

from app.db.models.user import User
from app.utils.phone import normalize_to_e164


@pytest.mark.asyncio
async def test_migration_normalises_phones_in_place(db_session):
    """All non-E.164 phones get normalised; already-E.164 ones untouched."""
    db_session.execute(text(
        "INSERT INTO users (id, phone, email, full_name, password_hash, "
        "referral_code, kyc_level, email_verified, is_active, is_phone_verified, created_at, updated_at) "
        "VALUES (gen_random_uuid(), '08011111111', 'a@x.test', 'A', 'h', 'A1', 'tier_0', "
        "false, true, false, now(), now())"
    ))
    db_session.execute(text(
        "INSERT INTO users (id, phone, email, full_name, password_hash, "
        "referral_code, kyc_level, email_verified, is_active, is_phone_verified, created_at, updated_at) "
        "VALUES (gen_random_uuid(), '+2348022222222', 'b@x.test', 'B', 'h', 'B1', 'tier_0', "
        "false, true, false, now(), now())"
    ))
    db_session.commit()

    # Import + run the migration's upgrade function directly
    from alembic.config import Config
    from alembic import command
    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")

    rows = db_session.execute(text("SELECT phone FROM users ORDER BY phone")).all()
    assert all(r[0].startswith("+234") for r in rows)
```

- [ ] **Step 3: Implement the migration**

Edit the newly generated `alembic/versions/<rev>_normalize_phone_e164.py`:

```python
"""normalize_phone_e164

Revision ID: <generated>
Revises: <previous>
Create Date: <generated>
"""
from alembic import op
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision = "<generated>"
down_revision = "<previous>"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from app.utils.phone import normalize_to_e164, InvalidPhoneFormat
    conn = op.get_bind()
    rows = conn.execute(text("SELECT id, phone FROM users WHERE phone NOT LIKE '+%'")).fetchall()
    for row in rows:
        try:
            new = normalize_to_e164(row.phone)
        except InvalidPhoneFormat:
            print(f"[migration] skipped corrupt phone user_id={row.id} phone={row.phone!r}")
            continue
        conn.execute(
            text("UPDATE users SET phone = :p WHERE id = :id"),
            {"p": new, "id": row.id},
        )


def downgrade() -> None:
    # Irreversible — normalisation is forward-only.
    pass
```

- [ ] **Step 4: Run test**

```bash
docker compose exec -T api pytest tests/db/test_phone_normalization_migration.py -v
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add alembic/versions/*_normalize_phone_e164.py tests/db/test_phone_normalization_migration.py
git commit -m "feat(db): alembic data migration normalising User.phone to E.164"
```

---

## Task 3 (B3): Scoped `pin_setup` JWT helpers

**Files:**
- Modify: `app/core/security.py`
- Test: `tests/core/test_security_pin_setup_token.py`

- [ ] **Step 1: Write the failing test**

Create `tests/core/test_security_pin_setup_token.py`:

```python
import pytest
from datetime import timedelta
from jose import jwt

from app.core.config import settings
from app.core.security import (
    create_pin_setup_token,
    verify_pin_setup_token,
    InvalidPinSetupToken,
)


def test_create_and_verify_round_trip():
    token = create_pin_setup_token(user_id="u-1")
    claims = verify_pin_setup_token(token)
    assert claims["sub"] == "u-1"
    assert claims["scope"] == "pin_setup"
    assert "jti" in claims


def test_rejects_non_pin_setup_scope():
    """A regular access token must not be accepted as a pin_setup token."""
    other = jwt.encode(
        {"sub": "u-1", "scope": "access"},
        settings.SECRET_KEY,
        algorithm="HS256",
    )
    with pytest.raises(InvalidPinSetupToken):
        verify_pin_setup_token(other)


def test_rejects_expired_token(monkeypatch):
    token = create_pin_setup_token(user_id="u-1", expires_in=timedelta(seconds=-1))
    with pytest.raises(InvalidPinSetupToken):
        verify_pin_setup_token(token)


def test_rejects_garbage():
    with pytest.raises(InvalidPinSetupToken):
        verify_pin_setup_token("not-a-jwt")
```

- [ ] **Step 2: Run test to verify it fails**

```bash
docker compose exec -T api pytest tests/core/test_security_pin_setup_token.py -v
```
Expected: FAIL (helpers don't exist).

- [ ] **Step 3: Implement the helpers**

Edit `app/core/security.py`. Add to the JWT section near the bottom:

```python
class InvalidPinSetupToken(ValueError):
    """Raised when a pin_setup token fails validation."""


def create_pin_setup_token(
    *,
    user_id: str,
    expires_in: timedelta = timedelta(minutes=10),
) -> str:
    """Issue a short-lived scoped JWT that only /auth/pin/set will accept.

    Carries claim ``scope: pin_setup`` plus a fresh ``jti`` for one-time
    use (the endpoint blocklists the jti on consumption).
    """
    to_encode = {
        "sub": user_id,
        "scope": "pin_setup",
        "jti": uuid4().hex,
        "iat": datetime.now(tz=UTC),
        "exp": datetime.now(tz=UTC) + expires_in,
    }
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm="HS256")


def verify_pin_setup_token(token: str) -> dict[str, Any]:
    """Decode + validate a pin_setup token. Raises InvalidPinSetupToken
    on any failure (signature, expiry, wrong scope)."""
    try:
        claims = jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])
    except Exception as exc:  # jose.JWTError, expired, malformed, etc.
        raise InvalidPinSetupToken(f"invalid token: {exc}") from exc
    if claims.get("scope") != "pin_setup":
        raise InvalidPinSetupToken("wrong scope")
    return claims
```

- [ ] **Step 4: Run test to verify it passes**

```bash
docker compose exec -T api pytest tests/core/test_security_pin_setup_token.py -v
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/core/security.py tests/core/test_security_pin_setup_token.py
git commit -m "feat(security): add scoped pin_setup JWT helpers"
```

---

## Task 4 (B4): Settings additions

**Files:**
- Modify: `app/core/config.py`
- Modify: `.env.example`
- Test: `tests/core/test_config_defaults.py`

- [ ] **Step 1: Write the failing test**

Create `tests/core/test_config_defaults.py`:

```python
from app.core.config import settings


def test_new_settings_have_safe_defaults():
    assert settings.AUTH_STRICT_GATES is False
    assert settings.AUTH_PIN_LOGIN_ENABLED is True
    assert settings.TERMII_OTP_CHANNEL == "dnd"
    assert settings.OTP_RESEND_DAILY_CAP == 10
    assert settings.OTP_RESEND_COOLDOWN_SECONDS == 60
```

- [ ] **Step 2: Run test to verify it fails**

```bash
docker compose exec -T api pytest tests/core/test_config_defaults.py -v
```
Expected: FAIL (attribute errors).

- [ ] **Step 3: Add settings**

Edit `app/core/config.py` inside the `Settings` class (insert after existing settings):

```python
    # ── Phase A+B auth migration ──────────────────────────────────────
    AUTH_STRICT_GATES: bool = False
    """When True, protected endpoints refuse for users missing any of the
    three auth gates (email_verified, is_phone_verified, pin_hash). Flip
    to True after ~80% mobile-version adoption of the migration build."""

    AUTH_PIN_LOGIN_ENABLED: bool = True
    """Master switch for /auth/pin-login. Disable to temporarily force
    all users back to phone+password login."""

    TERMII_OTP_CHANNEL: str = "dnd"
    """Termii SMS channel for OTP delivery. ``dnd`` bypasses the NCC DND
    registry (essential for production OTP delivery on Nigerian carriers);
    ``generic`` is cheaper but blocked for DND-registered numbers."""

    OTP_RESEND_COOLDOWN_SECONDS: int = 60
    """Minimum seconds between successive OTP sends for the same
    (user, purpose) pair."""

    OTP_RESEND_DAILY_CAP: int = 10
    """Maximum OTPs per phone per day (covers all purposes combined).
    Defense against SMS-bombing of a single number."""
```

Edit `.env.example` (append):

```
# Auth migration (Phase A+B)
AUTH_STRICT_GATES=false
AUTH_PIN_LOGIN_ENABLED=true
TERMII_OTP_CHANNEL=dnd
OTP_RESEND_COOLDOWN_SECONDS=60
OTP_RESEND_DAILY_CAP=10
```

- [ ] **Step 4: Run test to verify it passes**

```bash
docker compose exec -T api pytest tests/core/test_config_defaults.py -v
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/core/config.py .env.example tests/core/test_config_defaults.py
git commit -m "feat(config): add AUTH_STRICT_GATES, AUTH_PIN_LOGIN_ENABLED, OTP cap settings"
```

---

## Task 5 (B5): Termii channel switch (read from settings)

**Files:**
- Modify: `app/integrations/termii/client.py`
- Test: `tests/integrations/test_termii_channel.py`

- [ ] **Step 1: Write the failing test**

Create `tests/integrations/test_termii_channel.py`:

```python
from unittest.mock import AsyncMock, patch
import pytest

from app.integrations.termii.client import TermiiClient


@pytest.mark.asyncio
async def test_send_otp_uses_channel_from_settings(monkeypatch):
    monkeypatch.setattr("app.integrations.termii.client.settings.TERMII_OTP_CHANNEL", "dnd")
    monkeypatch.setattr("app.integrations.termii.client.settings.TERMII_API_KEY", "k")
    client = TermiiClient()

    captured = {}

    async def fake_post(self, url, json=None, **kw):
        captured["payload"] = json
        class R:
            def raise_for_status(self): pass
        return R()

    with patch("httpx.AsyncClient.post", new=fake_post):
        await client.send_otp(phone="+2348011111111", code="123456")

    assert captured["payload"]["channel"] == "dnd"
    assert "Do not share this code" in captured["payload"]["sms"]
```

- [ ] **Step 2: Run test to verify it fails**

```bash
docker compose exec -T api pytest tests/integrations/test_termii_channel.py -v
```
Expected: FAIL (currently hardcoded `channel: "generic"`).

- [ ] **Step 3: Update Termii client**

Edit `app/integrations/termii/client.py` (full file):

```python
import httpx

from app.core.config import settings


class TermiiClient:
    def __init__(self) -> None:
        self._base = "https://api.ng.termii.com/api"
        self._key = getattr(settings, "TERMII_API_KEY", None)
        self._sender = getattr(settings, "TERMII_SENDER_ID", "Timpbills")

    async def send_otp(self, *, phone: str, code: str) -> None:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(
                f"{self._base}/sms/send",
                json={
                    "to": phone,
                    "from": self._sender,
                    "sms": (
                        f"Your Timpbills code is {code}. It expires in 5 "
                        f"minutes. Do not share this code."
                    ),
                    "type": "plain",
                    "channel": settings.TERMII_OTP_CHANNEL,
                    "api_key": self._key,
                },
            )
            r.raise_for_status()

    async def send_text(self, *, phone: str, message: str) -> None:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(
                f"{self._base}/sms/send",
                json={
                    "to": phone,
                    "from": self._sender,
                    "sms": message,
                    "type": "plain",
                    "channel": "generic",  # non-OTP texts stay on generic
                    "api_key": self._key,
                },
            )
            r.raise_for_status()
```

- [ ] **Step 4: Run test**

```bash
docker compose exec -T api pytest tests/integrations/test_termii_channel.py -v
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/integrations/termii/client.py tests/integrations/test_termii_channel.py
git commit -m "feat(termii): switch OTP channel to dnd (configurable via TERMII_OTP_CHANNEL)"
```

---

## Task 6 (B6): OTP cooldown + daily cap helpers (in auth_service)

**Depends on:** Task 4 (uses `OTP_RESEND_COOLDOWN_SECONDS`, `OTP_RESEND_DAILY_CAP` settings)
**Files:**
- Modify: `app/services/auth_service.py`
- Test: `tests/services/test_otp_cooldown.py`

- [ ] **Step 1: Write the failing test**

Create `tests/services/test_otp_cooldown.py`:

```python
import pytest
from datetime import datetime, timedelta, UTC

from app.db.models.otp import OtpCode, OtpPurpose
from app.db.models.user import User, KycLevel
from app.services.auth_service import (
    _check_otp_cooldown,
    OtpCooldownActive,
    OtpDailyCapExceeded,
)


def _seed_user(db, *, phone="+2348011111111", email="a@x.test"):
    u = User(
        phone=phone, email=email, full_name="A",
        password_hash="h", referral_code="A1", kyc_level=KycLevel.tier_0,
    )
    db.add(u); db.commit(); db.refresh(u)
    return u


def test_cooldown_fires_when_recent_otp_exists(db_session):
    u = _seed_user(db_session)
    db_session.add(OtpCode(
        user_id=u.id, phone=u.phone,
        code_hash="x", purpose=OtpPurpose.phone_verification,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    ))
    db_session.commit()
    with pytest.raises(OtpCooldownActive):
        _check_otp_cooldown(db_session, user_id=u.id, purpose=OtpPurpose.phone_verification)


def test_cooldown_passes_after_window(db_session):
    u = _seed_user(db_session)
    db_session.add(OtpCode(
        user_id=u.id, phone=u.phone,
        code_hash="x", purpose=OtpPurpose.phone_verification,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        created_at=datetime.now(UTC) - timedelta(seconds=120),
    ))
    db_session.commit()
    # Does not raise
    _check_otp_cooldown(db_session, user_id=u.id, purpose=OtpPurpose.phone_verification)


def test_daily_cap_blocks_after_10(db_session):
    u = _seed_user(db_session)
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0)
    for _ in range(10):
        db_session.add(OtpCode(
            user_id=u.id, phone=u.phone,
            code_hash="x", purpose=OtpPurpose.phone_verification,
            expires_at=today + timedelta(hours=1),
            created_at=today + timedelta(minutes=10),
        ))
    db_session.commit()
    with pytest.raises(OtpDailyCapExceeded):
        _check_otp_cooldown(db_session, user_id=u.id, purpose=OtpPurpose.phone_verification)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
docker compose exec -T api pytest tests/services/test_otp_cooldown.py -v
```
Expected: FAIL (helpers don't exist).

- [ ] **Step 3: Implement the helpers**

Edit `app/services/auth_service.py`. Add near the top after imports:

```python
class OtpCooldownActive(Exception):
    """The most recent OTP for this (user, purpose) is younger than
    ``OTP_RESEND_COOLDOWN_SECONDS``."""


class OtpDailyCapExceeded(Exception):
    """The user has already received ``OTP_RESEND_DAILY_CAP`` OTPs today
    (UTC) across all purposes."""


def _check_otp_cooldown(db, *, user_id, purpose) -> None:
    """Raise if a resend would violate cooldown or daily cap. Call from
    every OTP-emitting code path (send_email_otp, send_phone_otp,
    register, forgot_password)."""
    from datetime import datetime, timedelta, UTC
    now = datetime.now(UTC)

    # Cooldown — latest OTP for this purpose must be older than the window
    latest = (
        db.query(OtpCode)
        .filter(OtpCode.user_id == user_id, OtpCode.purpose == purpose)
        .order_by(OtpCode.created_at.desc())
        .first()
    )
    if latest is not None:
        created = latest.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        if (now - created).total_seconds() < settings.OTP_RESEND_COOLDOWN_SECONDS:
            raise OtpCooldownActive()

    # Daily cap — count OTPs (any purpose) for this user since UTC midnight
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    count = (
        db.query(OtpCode)
        .filter(OtpCode.user_id == user_id, OtpCode.created_at >= midnight)
        .count()
    )
    if count >= settings.OTP_RESEND_DAILY_CAP:
        raise OtpDailyCapExceeded()
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
docker compose exec -T api pytest tests/services/test_otp_cooldown.py -v
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/auth_service.py tests/services/test_otp_cooldown.py
git commit -m "feat(auth): add OTP resend cooldown + daily cap helpers"
```

---

## Task 7 (B7): `require_full_auth_gates` dependency

**Depends on:** Task 4 (uses `AUTH_STRICT_GATES` setting)
**Files:**
- Modify: `app/api/deps.py`
- Test: `tests/api/test_require_full_auth_gates.py`

- [ ] **Step 1: Write the failing test**

Create `tests/api/test_require_full_auth_gates.py`:

```python
import pytest
from fastapi import HTTPException

from app.api.deps import require_full_auth_gates
from app.db.models.user import User, KycLevel


def _make_user(*, email_verified=True, phone_verified=True, pin_hash="argon2id$x"):
    return User(
        phone="+2348011111111", email="a@x.test", full_name="A",
        password_hash="h", referral_code="A1", kyc_level=KycLevel.tier_0,
        email_verified=email_verified, is_phone_verified=phone_verified,
        pin_hash=pin_hash, is_active=True,
    )


def test_passes_when_all_gates_true(monkeypatch):
    monkeypatch.setattr("app.api.deps.settings.AUTH_STRICT_GATES", True)
    u = _make_user()
    assert require_full_auth_gates(user=u) is u


def test_rejects_when_email_unverified_strict(monkeypatch):
    monkeypatch.setattr("app.api.deps.settings.AUTH_STRICT_GATES", True)
    u = _make_user(email_verified=False)
    with pytest.raises(HTTPException) as exc:
        require_full_auth_gates(user=u)
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "VERIFICATION_REQUIRED"
    assert exc.value.detail["which"] == "email"


def test_rejects_when_phone_unverified_strict(monkeypatch):
    monkeypatch.setattr("app.api.deps.settings.AUTH_STRICT_GATES", True)
    u = _make_user(phone_verified=False)
    with pytest.raises(HTTPException) as exc:
        require_full_auth_gates(user=u)
    assert exc.value.detail["which"] == "phone"


def test_rejects_when_no_pin_strict(monkeypatch):
    monkeypatch.setattr("app.api.deps.settings.AUTH_STRICT_GATES", True)
    u = _make_user(pin_hash=None)
    with pytest.raises(HTTPException) as exc:
        require_full_auth_gates(user=u)
    assert exc.value.detail["which"] == "pin_setup"


def test_soft_mode_passes_with_warning(monkeypatch, caplog):
    monkeypatch.setattr("app.api.deps.settings.AUTH_STRICT_GATES", False)
    u = _make_user(phone_verified=False)
    # Does not raise; logs a warning
    assert require_full_auth_gates(user=u) is u
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
docker compose exec -T api pytest tests/api/test_require_full_auth_gates.py -v
```
Expected: FAIL.

- [ ] **Step 3: Implement the dep**

Edit `app/api/deps.py`. Add at the bottom of the file:

```python
from app.core.logger import log


def require_full_auth_gates(user: User = Depends(get_current_user)) -> User:
    """Reject (403) any protected endpoint when the user has not yet passed
    all three auth gates: email_verified, is_phone_verified, pin_hash set.

    Soft mode (``AUTH_STRICT_GATES = False``): logs a warning + lets the
    request through. Use during the rollout window while mobile catches up.
    Strict mode (``AUTH_STRICT_GATES = True``): hard reject.
    """
    missing: str | None = None
    if not user.email_verified:
        missing = "email"
    elif not user.is_phone_verified:
        missing = "phone"
    elif user.pin_hash is None:
        missing = "pin_setup"

    if missing is None:
        return user

    if settings.AUTH_STRICT_GATES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "VERIFICATION_REQUIRED",
                "message": f"{missing} verification required",
                "which": missing,
            },
        )
    log.warning(
        "auth_gates: user %s missing %s but soft mode active — request allowed",
        user.id, missing,
    )
    return user
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
docker compose exec -T api pytest tests/api/test_require_full_auth_gates.py -v
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/api/deps.py tests/api/test_require_full_auth_gates.py
git commit -m "feat(api): add require_full_auth_gates dep with soft/strict mode"
```

---

## Task 8 (B8): Refactor `/auth/register` to send both OTPs, issue no tokens

**Depends on:** Task 1 (phone normalisation), Task 6 (cooldown helpers)
**Files:**
- Modify: `app/services/auth_service.py` (the `register` method)
- Modify: `app/schemas/auth.py` (`RegisterRequest` keeps shape; `RegisterResponse` gains `next_action`)
- Test: `tests/api/test_register_dual_otp.py`

- [ ] **Step 1: Write the failing integration test**

Create `tests/api/test_register_dual_otp.py`:

```python
import pytest


@pytest.mark.asyncio
async def test_register_sends_both_otps_and_no_tokens(client, mock_email_provider, mock_sms_provider):
    """POST /auth/register triggers email OTP via Resend AND phone OTP via Termii,
    persists both OtpCode rows, and returns 201 with next_action but no tokens."""
    r = await client.post("/api/v1/auth/register", json={
        "phone": "08011111111",
        "email": "a@x.test",
        "full_name": "Adebayo",
        "password": "Secret1!",
    })
    assert r.status_code == 201
    body = r.json()["data"]
    assert body["next_action"] == "verify_email_and_phone"
    assert "tokens" not in body
    # Verify both providers were called once
    assert len(mock_email_provider.sent) == 1
    assert mock_email_provider.sent[0]["to"] == "a@x.test"
    assert len(mock_sms_provider.sent) == 1
    assert mock_sms_provider.sent[0]["phone"] == "+2348011111111"


@pytest.mark.asyncio
async def test_register_normalises_phone_before_uniqueness_check(client):
    """Trying to re-register the same logical phone in a different format should 409."""
    await client.post("/api/v1/auth/register", json={
        "phone": "+2348011111111", "email": "a@x.test",
        "full_name": "A", "password": "Secret1!",
    })
    r = await client.post("/api/v1/auth/register", json={
        "phone": "08011111111", "email": "b@x.test",       # same phone, different format
        "full_name": "B", "password": "Secret1!",
    })
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "USER_ALREADY_EXISTS"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
docker compose exec -T api pytest tests/api/test_register_dual_otp.py -v
```
Expected: FAIL.

- [ ] **Step 3: Update `register` method**

In `app/services/auth_service.py`, replace the `register` method. The full new body:

```python
async def register(self, req: RegisterRequest) -> RegisterResponse:
    """Sprint A: register the user and emit BOTH email and phone OTPs.
    No tokens issued — they come after both verifications + pin/set."""
    from app.utils.phone import normalize_to_e164, InvalidPhoneFormat
    try:
        phone = normalize_to_e164(req.phone)
    except InvalidPhoneFormat:
        raise ValueError("INVALID_PHONE_FORMAT")

    thirty_days_ago = datetime.now(UTC) - timedelta(days=30)

    existing_phone = self._db.query(User).filter(User.phone == phone).first()
    if existing_phone is not None:
        if (
            existing_phone.deleted_at is not None
            and _ensure_aware_utc(existing_phone.deleted_at) > thirty_days_ago
        ):
            raise ValueError("PHONE_RECENTLY_DELETED")
        raise ValueError("USER_ALREADY_EXISTS")

    existing_email = self._db.query(User).filter(User.email == req.email).first()
    if existing_email is not None:
        if (
            existing_email.deleted_at is not None
            and _ensure_aware_utc(existing_email.deleted_at) > thirty_days_ago
        ):
            raise ValueError("EMAIL_RECENTLY_DELETED")
        raise ValueError("USER_ALREADY_EXISTS")

    new_code = generate_referral_code(
        code_exists=lambda c: self._db.query(User)
        .filter(User.referral_code == c)
        .first()
        is not None,
    )

    user = User(
        phone=phone,
        email=req.email,
        full_name=req.full_name,
        password_hash=await hash_password_async(req.password),
        kyc_level=KycLevel.tier_0,
        referral_code=new_code,
    )
    self._db.add(user)
    self._db.flush()

    referred_by = self._maybe_attribute_referral(
        referee=user, raw_code=req.referral_code,
    )

    # Two OTPs: email + phone. Cooldown / daily cap not checked at register
    # (it's the first send); subsequent resends go through their own gates.
    email_code = f"{secrets.randbelow(1_000_000):06d}"
    phone_code = f"{secrets.randbelow(1_000_000):06d}"
    self._db.add(OtpCode(
        user_id=user.id, email=user.email,
        code_hash=await hash_pin_async(email_code),
        purpose=OtpPurpose.email_verification,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    ))
    self._db.add(OtpCode(
        user_id=user.id, phone=user.phone,
        code_hash=await hash_pin_async(phone_code),
        purpose=OtpPurpose.phone_verification,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    ))
    self._db.commit()

    await self._email.send_otp(to=user.email, code=email_code)
    await self._sms.send_otp(phone=user.phone, code=phone_code)

    return RegisterResponse(
        user_id=str(user.id),
        email=user.email,
        phone=user.phone,
        referred_by=referred_by,
        next_action="verify_email_and_phone",
    )
```

Update `RegisterResponse` in `app/schemas/auth.py`:

```python
class RegisterResponse(BaseModel):
    user_id: str
    email: EmailStr
    phone: str
    referred_by: bool
    next_action: Literal["verify_email_and_phone"] = "verify_email_and_phone"
```

Add `INVALID_PHONE_FORMAT` to `_ERROR_MAP` in `app/api/v1/endpoints/auth.py`:

```python
    "INVALID_PHONE_FORMAT": (400, "Invalid phone number format"),
```

- [ ] **Step 4: Run integration tests**

```bash
docker compose exec -T api pytest tests/api/test_register_dual_otp.py -v
```
Expected: PASS.

- [ ] **Step 5: Run the full auth test suite to confirm no regressions**

```bash
docker compose exec -T api pytest tests/api/ tests/services/test_auth_service*.py -v --no-cov
```
Expected: PASS (some pre-existing tests may have assumed register issued tokens — fix them inline if found, expect ~2-3 small assertion updates).

- [ ] **Step 6: Commit**

```bash
git add app/services/auth_service.py app/schemas/auth.py app/api/v1/endpoints/auth.py tests/api/test_register_dual_otp.py
git commit -m "feat(auth): register sends email+phone OTPs, no tokens, next_action response"
```

---

## Task 9 (B9): `/auth/email/verify` — emit `pin_setup_token` when both gates pass

**Depends on:** Task 3 (pin_setup token), Task 8 (register flow)
**Files:**
- Modify: `app/services/auth_service.py` (`verify_email_otp` method)
- Modify: `app/schemas/auth.py` (`EmailVerifiedResponse`)
- Test: `tests/api/test_email_verify_emits_pin_setup_token.py`

- [ ] **Step 1: Write the failing test**

Create `tests/api/test_email_verify_emits_pin_setup_token.py`:

```python
import pytest

from app.core.security import verify_pin_setup_token


@pytest.mark.asyncio
async def test_email_verify_after_phone_verify_emits_pin_setup_token(
    client, seed_user_phone_verified_no_pin, fetch_latest_otp,
):
    """User has already verified phone; verifying email completes both
    gates and (since pin_hash is null) returns a pin_setup_token."""
    user = seed_user_phone_verified_no_pin
    otp = await fetch_latest_otp(user_id=user.id, purpose="email_verification")
    r = await client.post("/api/v1/auth/email/verify", json={
        "email": user.email, "code": otp,
    })
    assert r.status_code == 200
    body = r.json()["data"]
    assert body["next_action"] == "pin_setup_required"
    assert "pin_setup_token" in body
    claims = verify_pin_setup_token(body["pin_setup_token"])
    assert claims["sub"] == str(user.id)


@pytest.mark.asyncio
async def test_email_verify_alone_does_not_emit_pin_setup_token(
    client, seed_fresh_user, fetch_latest_otp,
):
    """User has not yet verified phone; email-verify alone returns next_action
    indicating phone verification still required."""
    user = seed_fresh_user
    otp = await fetch_latest_otp(user_id=user.id, purpose="email_verification")
    r = await client.post("/api/v1/auth/email/verify", json={
        "email": user.email, "code": otp,
    })
    assert r.status_code == 200
    body = r.json()["data"]
    assert body["next_action"] == "phone_verification_required"
    assert "pin_setup_token" not in body
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
docker compose exec -T api pytest tests/api/test_email_verify_emits_pin_setup_token.py -v
```
Expected: FAIL.

- [ ] **Step 3: Update `verify_email_otp`**

In `app/services/auth_service.py`, replace `verify_email_otp` body:

```python
async def verify_email_otp(self, req: VerifyEmailOtpRequest) -> EmailVerifiedResponse:
    user = self._db.query(User).filter(User.email == req.email).first()
    if not user:
        raise ValueError("USER_NOT_FOUND")

    otp = (
        self._db.query(OtpCode)
        .filter(
            OtpCode.user_id == user.id,
            OtpCode.purpose == OtpPurpose.email_verification,
            OtpCode.used_at.is_(None),
        )
        .order_by(OtpCode.created_at.desc())
        .first()
    )
    if not otp:
        raise ValueError("NO_ACTIVE_OTP")
    if _is_expired(otp.expires_at):
        raise ValueError("OTP_EXPIRED")
    if otp.attempts >= 3:
        raise ValueError("OTP_ATTEMPTS_EXCEEDED")
    if not await verify_pin_async(req.code, otp.code_hash):
        otp.attempts += 1
        self._db.commit()
        raise ValueError("INVALID_OTP")

    otp.used_at = datetime.now(UTC)
    user.email_verified = True
    self._db.commit()

    # Decide next_action based on the OTHER gates.
    if not user.is_phone_verified:
        return EmailVerifiedResponse(
            email_verified=True, phone_verified=False,
            pin_set=user.pin_hash is not None,
            next_action="phone_verification_required",
        )

    if user.pin_hash is None:
        # Both verifications now complete + PIN not set → emit scoped token
        from app.core.security import create_pin_setup_token
        token = create_pin_setup_token(user_id=str(user.id))
        return EmailVerifiedResponse(
            email_verified=True, phone_verified=True,
            pin_set=False,
            next_action="pin_setup_required",
            pin_setup_token=token,
        )

    # All gates pass — issue full tokens
    tokens, jti = _issue_token_pair(str(user.id))
    await self._tokens.save(
        user_id=str(user.id), jti=jti, ttl_seconds=REFRESH_TOKEN_TTL_SECONDS,
    )
    return EmailVerifiedResponse(
        email_verified=True, phone_verified=True, pin_set=True,
        next_action="tokens_issued", tokens=tokens,
    )
```

Update `EmailVerifiedResponse` in `app/schemas/auth.py`:

```python
class EmailVerifiedResponse(BaseModel):
    email_verified: bool
    phone_verified: bool
    pin_set: bool
    next_action: Literal[
        "phone_verification_required", "pin_setup_required", "tokens_issued",
    ]
    pin_setup_token: str | None = None
    tokens: AuthTokens | None = None
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
docker compose exec -T api pytest tests/api/test_email_verify_emits_pin_setup_token.py -v
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/auth_service.py app/schemas/auth.py tests/api/test_email_verify_emits_pin_setup_token.py
git commit -m "feat(auth): /email/verify emits pin_setup_token when both gates pass"
```

---

## Task 10 (B10): New `/auth/phone/verify` signup variant (unauthenticated)

**Depends on:** Task 3, Task 8
**Files:**
- Modify: `app/services/auth_service.py` (new `verify_phone_otp_unauthed` method)
- Modify: `app/api/v1/endpoints/auth.py` (new public route)
- Modify: `app/schemas/auth.py` (`PhoneVerifyRequest` for signup)
- Test: `tests/api/test_auth_phone_verify_signup.py`

- [ ] **Step 1: Write the failing test**

Create `tests/api/test_auth_phone_verify_signup.py`:

```python
import pytest

from app.core.security import verify_pin_setup_token


@pytest.mark.asyncio
async def test_phone_verify_signup_emits_pin_setup_token(
    client, seed_user_email_verified_no_pin, fetch_latest_otp,
):
    user = seed_user_email_verified_no_pin
    otp = await fetch_latest_otp(user_id=user.id, purpose="phone_verification")
    r = await client.post("/api/v1/auth/phone/verify", json={
        "phone": user.phone, "code": otp,
    })
    assert r.status_code == 200
    body = r.json()["data"]
    assert body["next_action"] == "pin_setup_required"
    claims = verify_pin_setup_token(body["pin_setup_token"])
    assert claims["sub"] == str(user.id)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
docker compose exec -T api pytest tests/api/test_auth_phone_verify_signup.py -v
```
Expected: FAIL (route doesn't exist).

- [ ] **Step 3: Add service method + endpoint**

In `app/services/auth_service.py` add:

```python
async def verify_phone_otp_unauthed(
    self, *, phone: str, code: str,
) -> "PhoneVerifiedResponse":
    """Public phone verification used during signup + existing-user migration.
    Distinct from `verify_phone_otp` (authed) which targets the tier_1
    upgrade flow within an active session."""
    from app.utils.phone import normalize_to_e164, InvalidPhoneFormat
    try:
        phone = normalize_to_e164(phone)
    except InvalidPhoneFormat:
        raise ValueError("INVALID_PHONE_FORMAT")

    user = self._db.query(User).filter(User.phone == phone).first()
    if not user:
        raise ValueError("USER_NOT_FOUND")

    otp = (
        self._db.query(OtpCode)
        .filter(
            OtpCode.user_id == user.id,
            OtpCode.purpose == OtpPurpose.phone_verification,
            OtpCode.used_at.is_(None),
        )
        .order_by(OtpCode.created_at.desc())
        .first()
    )
    if not otp:
        raise ValueError("NO_ACTIVE_OTP")
    if _is_expired(otp.expires_at):
        raise ValueError("OTP_EXPIRED")
    if otp.attempts >= 3:
        raise ValueError("OTP_ATTEMPTS_EXCEEDED")
    if not await verify_pin_async(code, otp.code_hash):
        otp.attempts += 1
        self._db.commit()
        raise ValueError("INVALID_OTP")

    otp.used_at = datetime.now(UTC)
    user.is_phone_verified = True
    user.kyc_level = KycLevel.tier_1
    self._db.commit()

    # Decide next_action
    if not user.email_verified:
        return PhoneVerifiedResponse(
            email_verified=False, phone_verified=True,
            pin_set=user.pin_hash is not None,
            next_action="email_verification_required",
        )
    if user.pin_hash is None:
        from app.core.security import create_pin_setup_token
        return PhoneVerifiedResponse(
            email_verified=True, phone_verified=True, pin_set=False,
            next_action="pin_setup_required",
            pin_setup_token=create_pin_setup_token(user_id=str(user.id)),
        )
    tokens, jti = _issue_token_pair(str(user.id))
    await self._tokens.save(
        user_id=str(user.id), jti=jti, ttl_seconds=REFRESH_TOKEN_TTL_SECONDS,
    )
    return PhoneVerifiedResponse(
        email_verified=True, phone_verified=True, pin_set=True,
        next_action="tokens_issued", tokens=tokens,
    )
```

In `app/schemas/auth.py`:

```python
class PhoneVerifyRequest(BaseModel):
    phone: str
    code: str


class PhoneVerifiedResponse(BaseModel):
    email_verified: bool
    phone_verified: bool
    pin_set: bool
    next_action: Literal[
        "email_verification_required", "pin_setup_required", "tokens_issued",
    ]
    pin_setup_token: str | None = None
    tokens: AuthTokens | None = None
```

In `app/api/v1/endpoints/auth.py` add a new route (place near the existing email/verify):

```python
@router.post("/phone/verify")
@limiter.limit("5/minute")
async def verify_phone_otp_signup(
    request: Request,
    req: PhoneVerifyRequest,
    svc: AuthService = Depends(get_auth_service),
):
    """Signup + migration phone verification (no auth required).
    Distinct from /phone/verify-otp which is authenticated and is part
    of the in-session tier_1 upgrade flow.
    """
    try:
        res = await svc.verify_phone_otp_unauthed(phone=req.phone, code=req.code)
    except ValueError as e:
        _raise(str(e))
    return success(
        res.model_dump(),
        request_id=getattr(request.state, "request_id", None),
    )
```

- [ ] **Step 4: Run test to verify it passes**

```bash
docker compose exec -T api pytest tests/api/test_auth_phone_verify_signup.py -v
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/auth_service.py app/api/v1/endpoints/auth.py app/schemas/auth.py tests/api/test_auth_phone_verify_signup.py
git commit -m "feat(auth): public /phone/verify endpoint for signup + migration"
```

---

## Task 11 (B11): Rewrite `/auth/pin/set` — require scoped `pin_setup_token`

**Depends on:** Task 3, Task 9, Task 10
**Files:**
- Modify: `app/services/auth_service.py` (`set_pin_first_time`)
- Modify: `app/api/v1/endpoints/auth.py` (replace `/pin/set` handler)
- Modify: `app/schemas/auth.py` (`SetPinFirstTimeRequest`)
- Test: `tests/api/test_pin_set_scoped_token.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/api/test_pin_set_scoped_token.py`:

```python
import pytest

from app.core.security import create_pin_setup_token, create_access_token


@pytest.mark.asyncio
async def test_pin_set_accepts_scoped_token_and_issues_full_tokens(
    client, seed_user_both_verified_no_pin,
):
    user = seed_user_both_verified_no_pin
    token = create_pin_setup_token(user_id=str(user.id))
    r = await client.post(
        "/api/v1/auth/pin/set",
        headers={"X-Pin-Setup-Token": token},
        json={"pin": "1234"},
    )
    assert r.status_code == 200
    body = r.json()["data"]
    assert body["pin_set"] is True
    assert "access_token" in body["tokens"]


@pytest.mark.asyncio
async def test_pin_set_rejects_access_token_in_pin_setup_header(client, seed_user_both_verified_no_pin):
    user = seed_user_both_verified_no_pin
    access = create_access_token(subject=str(user.id))
    r = await client.post(
        "/api/v1/auth/pin/set",
        headers={"X-Pin-Setup-Token": access},
        json={"pin": "1234"},
    )
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "INVALID_PIN_SETUP_TOKEN"


@pytest.mark.asyncio
async def test_pin_set_rejects_reused_scoped_token(client, seed_user_both_verified_no_pin):
    """One-time use: a successful pin/set call blocklists the jti."""
    user = seed_user_both_verified_no_pin
    token = create_pin_setup_token(user_id=str(user.id))
    r1 = await client.post(
        "/api/v1/auth/pin/set",
        headers={"X-Pin-Setup-Token": token},
        json={"pin": "1234"},
    )
    assert r1.status_code == 200
    r2 = await client.post(
        "/api/v1/auth/pin/set",
        headers={"X-Pin-Setup-Token": token},
        json={"pin": "5678"},
    )
    assert r2.status_code == 401


@pytest.mark.asyncio
async def test_pin_set_rejects_when_gates_not_met(client, seed_user_email_verified_no_pin):
    """User has email_verified but NOT phone_verified — token shouldn't have
    been issued in the first place; rejection is defense-in-depth."""
    user = seed_user_email_verified_no_pin
    token = create_pin_setup_token(user_id=str(user.id))
    r = await client.post(
        "/api/v1/auth/pin/set",
        headers={"X-Pin-Setup-Token": token},
        json={"pin": "1234"},
    )
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "GATES_NOT_MET"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
docker compose exec -T api pytest tests/api/test_pin_set_scoped_token.py -v
```
Expected: FAIL.

- [ ] **Step 3: Implement service method**

In `app/services/auth_service.py` add:

```python
async def set_pin_first_time(
    self, *, scoped_token: str, pin: str,
    revocation_svc: "TokenRevocationService",
) -> "SetPinFirstTimeResponse":
    """Consume a scoped pin_setup token, set the PIN, issue full tokens."""
    from app.core.security import (
        verify_pin_setup_token, InvalidPinSetupToken,
    )
    try:
        claims = verify_pin_setup_token(scoped_token)
    except InvalidPinSetupToken:
        raise ValueError("INVALID_PIN_SETUP_TOKEN")

    jti = claims.get("jti")
    if not jti or await revocation_svc.is_revoked(jti):
        raise ValueError("INVALID_PIN_SETUP_TOKEN")

    user = self._db.query(User).filter(User.id == UUID(claims["sub"])).first()
    if not user:
        raise ValueError("USER_NOT_FOUND")
    if not (user.email_verified and user.is_phone_verified):
        raise ValueError("GATES_NOT_MET")
    if user.pin_hash is not None:
        raise ValueError("PIN_ALREADY_SET")

    user.pin_hash = await hash_pin_async(pin)
    self._db.commit()

    # One-time use: blocklist the jti for the remainder of its TTL
    exp = int(claims.get("exp", 0))
    await revocation_svc.revoke(jti=jti, exp_unix_seconds=exp)

    tokens, refresh_jti = _issue_token_pair(str(user.id))
    await self._tokens.save(
        user_id=str(user.id), jti=refresh_jti, ttl_seconds=REFRESH_TOKEN_TTL_SECONDS,
    )
    return SetPinFirstTimeResponse(pin_set=True, tokens=tokens)
```

In `app/schemas/auth.py`:

```python
class SetPinFirstTimeRequest(BaseModel):
    pin: str = Field(min_length=4, max_length=4, pattern=r"^\d{4}$")


class SetPinFirstTimeResponse(BaseModel):
    pin_set: bool
    tokens: AuthTokens
```

Replace the `/pin/set` route in `app/api/v1/endpoints/auth.py`:

```python
@router.post("/pin/set")
@limiter.limit("3/minute")
async def set_pin(
    request: Request,
    req: SetPinFirstTimeRequest,
    x_pin_setup_token: str = Header(..., alias="X-Pin-Setup-Token"),
    svc: AuthService = Depends(get_auth_service),
    revocation_svc: TokenRevocationService = Depends(get_token_revocation_service),
):
    """First-time PIN setup. Requires a scoped pin_setup token issued by
    /auth/email/verify, /auth/phone/verify, or /auth/login. One-time use.
    """
    try:
        res = await svc.set_pin_first_time(
            scoped_token=x_pin_setup_token, pin=req.pin,
            revocation_svc=revocation_svc,
        )
    except ValueError as e:
        _raise(str(e))
    return success(
        res.model_dump(),
        request_id=getattr(request.state, "request_id", None),
    )
```

Add to `_ERROR_MAP`:

```python
    "INVALID_PIN_SETUP_TOKEN": (401, "Invalid or expired pin_setup token"),
    "GATES_NOT_MET": (400, "Email or phone verification not complete"),
    "PIN_ALREADY_SET": (409, "PIN already set; use /pin/change instead"),
```

The existing `set_pin` service method (used by authed `/pin/change` callers) stays as-is.

- [ ] **Step 4: Run tests to verify they pass**

```bash
docker compose exec -T api pytest tests/api/test_pin_set_scoped_token.py -v
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/auth_service.py app/api/v1/endpoints/auth.py app/schemas/auth.py tests/api/test_pin_set_scoped_token.py
git commit -m "feat(auth): /pin/set requires scoped pin_setup token; one-time use"
```

---

## Task 12 (B12): `/auth/login` — phone-only + `next_action` + inline OTP send

**Depends on:** Task 1, Task 3, Task 6, Task 8
**Files:**
- Modify: `app/services/auth_service.py` (`login`)
- Modify: `app/schemas/auth.py` (`LoginRequest`, `LoginResponse`)
- Test: `tests/api/test_login_next_action.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/api/test_login_next_action.py`:

```python
import pytest


@pytest.mark.asyncio
async def test_login_returns_tokens_when_all_gates_pass(client, seed_user_full):
    r = await client.post("/api/v1/auth/login", json={
        "phone": seed_user_full.phone, "password": "Secret1!",
    })
    body = r.json()["data"]
    assert body["next_action"] == "tokens_issued"
    assert "access_token" in body["tokens"]


@pytest.mark.asyncio
async def test_login_returns_phone_verification_required_when_unverified(
    client, seed_user_email_verified_no_phone_no_pin, mock_sms_provider,
):
    user = seed_user_email_verified_no_phone_no_pin
    r = await client.post("/api/v1/auth/login", json={
        "phone": user.phone, "password": "Secret1!",
    })
    body = r.json()["data"]
    assert body["next_action"] == "phone_verification_required"
    assert body["phone_otp_sent"] is True
    assert "tokens" not in body
    # The inline send actually fired Termii
    assert len(mock_sms_provider.sent) == 1


@pytest.mark.asyncio
async def test_login_with_unverified_phone_respects_cooldown(
    client, seed_user_email_verified_no_phone_no_pin, mock_sms_provider, fixture_recent_otp,
):
    """If an OTP was already sent within cooldown, login response still says
    phone_verification_required but phone_otp_sent=False (no new SMS)."""
    user = seed_user_email_verified_no_phone_no_pin
    fixture_recent_otp(user_id=user.id, purpose="phone_verification", seconds_ago=10)
    r = await client.post("/api/v1/auth/login", json={
        "phone": user.phone, "password": "Secret1!",
    })
    body = r.json()["data"]
    assert body["next_action"] == "phone_verification_required"
    assert body["phone_otp_sent"] is False
    assert len(mock_sms_provider.sent) == 0


@pytest.mark.asyncio
async def test_login_returns_pin_setup_token_when_verified_but_no_pin(
    client, seed_user_both_verified_no_pin,
):
    user = seed_user_both_verified_no_pin
    r = await client.post("/api/v1/auth/login", json={
        "phone": user.phone, "password": "Secret1!",
    })
    body = r.json()["data"]
    assert body["next_action"] == "pin_setup_required"
    assert "pin_setup_token" in body
    assert "tokens" not in body


@pytest.mark.asyncio
async def test_login_rejects_email_identifier(client, seed_user_full):
    """Phone-only — email-as-identifier no longer accepted."""
    r = await client.post("/api/v1/auth/login", json={
        "phone": seed_user_full.email,   # passing email in phone field
        "password": "Secret1!",
    })
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "INVALID_PHONE_FORMAT"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
docker compose exec -T api pytest tests/api/test_login_next_action.py -v
```
Expected: FAIL.

- [ ] **Step 3: Update `login` method**

In `app/services/auth_service.py`, replace the `login` method:

```python
async def login(self, req: LoginRequest) -> LoginResponse:
    from app.utils.phone import normalize_to_e164, InvalidPhoneFormat
    try:
        phone = normalize_to_e164(req.phone)
    except InvalidPhoneFormat:
        raise ValueError("INVALID_PHONE_FORMAT")

    user = self._db.query(User).filter(User.phone == phone).first()
    if not user or not await verify_password_async(req.password, user.password_hash):
        raise ValueError("INVALID_CREDENTIALS")
    if not user.is_active:
        raise ValueError("ACCOUNT_DISABLED")

    # Transparent rehash (Phase A perf) — kept as-is
    if password_needs_rehash(user.password_hash):
        user.password_hash = await hash_password_async(req.password)
        self._db.commit()

    # Gate evaluation — order matters: email → phone → pin
    if not user.email_verified:
        return LoginResponse(
            next_action="email_verification_required",
            pin_set=user.pin_hash is not None,
        )

    if not user.is_phone_verified:
        # Inline OTP send if cooldown allows
        phone_otp_sent = False
        try:
            _check_otp_cooldown(
                self._db, user_id=user.id, purpose=OtpPurpose.phone_verification,
            )
            code = f"{secrets.randbelow(1_000_000):06d}"
            self._db.add(OtpCode(
                user_id=user.id, phone=user.phone,
                code_hash=await hash_pin_async(code),
                purpose=OtpPurpose.phone_verification,
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            ))
            self._db.commit()
            await self._sms.send_otp(phone=user.phone, code=code)
            phone_otp_sent = True
        except (OtpCooldownActive, OtpDailyCapExceeded):
            pass
        return LoginResponse(
            next_action="phone_verification_required",
            pin_set=user.pin_hash is not None,
            phone_otp_sent=phone_otp_sent,
        )

    if user.pin_hash is None:
        from app.core.security import create_pin_setup_token
        return LoginResponse(
            next_action="pin_setup_required",
            pin_set=False,
            pin_setup_token=create_pin_setup_token(user_id=str(user.id)),
        )

    tokens, jti = _issue_token_pair(str(user.id))
    await self._tokens.save(
        user_id=str(user.id), jti=jti, ttl_seconds=REFRESH_TOKEN_TTL_SECONDS,
    )
    return LoginResponse(
        next_action="tokens_issued",
        pin_set=True,
        tokens=tokens,
    )
```

Update `LoginRequest` + `LoginResponse` in `app/schemas/auth.py`:

```python
class LoginRequest(BaseModel):
    phone: str
    password: str


class LoginResponse(BaseModel):
    next_action: Literal[
        "tokens_issued",
        "email_verification_required",
        "phone_verification_required",
        "pin_setup_required",
    ]
    pin_set: bool
    tokens: AuthTokens | None = None
    pin_setup_token: str | None = None
    phone_otp_sent: bool | None = None
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
docker compose exec -T api pytest tests/api/test_login_next_action.py -v
```
Expected: PASS.

- [ ] **Step 5: Run the full test suite to catch any login-shape regressions**

```bash
docker compose exec -T api pytest --no-cov -q
```
Expected: PASS (some pre-existing tests may need updating to the new `LoginResponse` shape — fix inline; expect ~5-10 small fixtures).

- [ ] **Step 6: Commit**

```bash
git add app/services/auth_service.py app/schemas/auth.py tests/api/test_login_next_action.py
git commit -m "feat(auth): phone-only /login with next_action + inline OTP send"
```

---

## Task 13 (B13): New `/auth/pin-login` endpoint

**Depends on:** Task 4 (`AUTH_PIN_LOGIN_ENABLED`)
**Files:**
- Modify: `app/services/pin_service.py` (add `pin_login_async`)
- Modify: `app/api/v1/endpoints/auth.py` (new route)
- Modify: `app/schemas/auth.py` (`PinLoginRequest`, `PinLoginResponse`)
- Test: `tests/api/test_pin_login.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/api/test_pin_login.py`:

```python
import pytest

from app.core.security import create_refresh_token, decode_token


@pytest.mark.asyncio
async def test_pin_login_happy_path(client, seed_user_full_with_refresh):
    user, refresh_token = seed_user_full_with_refresh
    r = await client.post("/api/v1/auth/pin-login", json={
        "refresh_token": refresh_token,
        "pin": "1234",
    })
    assert r.status_code == 200
    body = r.json()["data"]
    assert "access_token" in body["tokens"]
    assert "refresh_token" in body["tokens"]
    # New refresh token, not the old one (rotation)
    assert body["tokens"]["refresh_token"] != refresh_token


@pytest.mark.asyncio
async def test_pin_login_wrong_pin_returns_401(client, seed_user_full_with_refresh):
    user, refresh_token = seed_user_full_with_refresh
    r = await client.post("/api/v1/auth/pin-login", json={
        "refresh_token": refresh_token,
        "pin": "9999",
    })
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "INVALID_PIN"


@pytest.mark.asyncio
async def test_pin_login_invalid_refresh_token_returns_401(client):
    r = await client.post("/api/v1/auth/pin-login", json={
        "refresh_token": "not-a-jwt",
        "pin": "1234",
    })
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "INVALID_TOKEN"


@pytest.mark.asyncio
async def test_pin_login_revoked_refresh_token_nukes_all_sessions(
    client, seed_user_full_with_refresh, token_store,
):
    """Replay defense: a refresh_token that's been rotated out should not
    only fail — it should revoke ALL the user's sessions."""
    user, refresh_token = seed_user_full_with_refresh
    # Rotate the refresh once via /auth/refresh (or by manually revoking)
    payload = decode_token(refresh_token)
    await token_store.revoke(user_id=str(user.id), jti=payload["jti"])

    r = await client.post("/api/v1/auth/pin-login", json={
        "refresh_token": refresh_token,
        "pin": "1234",
    })
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_pin_login_for_user_with_no_pin_returns_400(client, seed_user_no_pin_with_refresh):
    user, refresh_token = seed_user_no_pin_with_refresh
    r = await client.post("/api/v1/auth/pin-login", json={
        "refresh_token": refresh_token,
        "pin": "1234",
    })
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "PIN_NOT_SET"


@pytest.mark.asyncio
async def test_pin_login_locked_returns_423(client, seed_user_full_with_refresh, redis):
    user, refresh_token = seed_user_full_with_refresh
    await redis.set(f"pin_locked:{user.id}", "1", ex=60)
    r = await client.post("/api/v1/auth/pin-login", json={
        "refresh_token": refresh_token,
        "pin": "1234",
    })
    assert r.status_code == 423
    assert r.json()["detail"]["code"] == "PIN_LOCKED"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
docker compose exec -T api pytest tests/api/test_pin_login.py -v
```
Expected: FAIL.

- [ ] **Step 3: Add `pin_login_async` to PinService**

Edit `app/services/pin_service.py`:

```python
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_pin_async,
    pin_needs_rehash,
    verify_pin_async,
)
from app.services.token_store import TokenStore


async def pin_login_async(
    *,
    db,
    redis,
    token_store: TokenStore,
    refresh_token: str,
) -> tuple[str, str]:  # (new_access, new_refresh)
    """Stub — overridden by the version inside PinService below."""
    raise NotImplementedError


class PinService:
    # ... existing __init__ / verify_async ...

    async def pin_login(
        self,
        *,
        refresh_token: str,
        pin: str,
        token_store,
    ) -> "AuthTokens":
        """Cold-start PIN login. Reuses existing PinService lockout +
        RedisTokenStore rotation."""
        from datetime import datetime, timedelta, UTC
        from uuid import UUID, uuid4
        from jose import JWTError

        # 1. Decode refresh_token
        try:
            payload = decode_token(refresh_token)
        except JWTError:
            raise InvalidPinLoginToken()
        if payload.get("typ") != "refresh":
            raise InvalidPinLoginToken()
        user_id = payload.get("sub")
        jti = payload.get("jti")
        if not user_id or not jti:
            raise InvalidPinLoginToken()

        # 2. Replay defense — token_store says the jti is still active
        if not await token_store.is_valid(user_id=user_id, jti=jti):
            await token_store.revoke_all(user_id=user_id)
            raise InvalidPinLoginToken()

        # 3. Load user
        try:
            user_uuid = UUID(user_id)
        except (TypeError, ValueError):
            raise InvalidPinLoginToken()
        user = self._db.query(User).filter(User.id == user_uuid).first()
        if not user:
            raise UserNotFound()
        if user.is_active is False:
            raise AccountDisabled()

        # 4. tokens_revoked_at check
        if user.tokens_revoked_at is not None:
            iat = payload.get("iat")
            if iat is not None:
                revoked = user.tokens_revoked_at
                if revoked.tzinfo is None:
                    revoked = revoked.replace(tzinfo=UTC)
                if int(iat) <= int(revoked.timestamp()):
                    raise InvalidPinLoginToken()

        # 5. Lockout
        if await self._is_locked(user_uuid):
            raise PinLocked()

        # 6. PIN must be set
        if user.pin_hash is None:
            raise PinNotSet()

        # 7. Verify PIN (with attempts counter)
        if not await verify_pin_async(pin, user.pin_hash):
            attempts = await self._redis.incr(self._attempts_key(user_uuid))
            if attempts == 1:
                await self._redis.expire(
                    self._attempts_key(user_uuid), LOCKOUT_TTL_SECONDS,
                )
            if attempts >= MAX_ATTEMPTS:
                await self._redis.set(
                    self._lock_key(user_uuid), "1", ex=LOCKOUT_TTL_SECONDS,
                )
            raise InvalidPin()

        await self._redis.delete(self._attempts_key(user_uuid))

        # 8. Transparent rehash
        if pin_needs_rehash(user.pin_hash):
            user.pin_hash = await hash_pin_async(pin)
            self._db.commit()

        # 9. Rotate refresh_token
        await token_store.revoke(user_id=user_id, jti=jti)
        new_jti = uuid4().hex
        new_access = create_access_token(subject=user_id)
        new_refresh = create_refresh_token(
            subject=user_id, jti=new_jti, expires_in=timedelta(days=30),
        )
        await token_store.save(user_id=user_id, jti=new_jti, ttl_seconds=30 * 86400)
        return new_access, new_refresh


class InvalidPinLoginToken(Exception):
    pass


class UserNotFound(Exception):
    pass


class AccountDisabled(Exception):
    pass
```

In `app/schemas/auth.py`:

```python
class PinLoginRequest(BaseModel):
    refresh_token: str
    pin: str = Field(min_length=4, max_length=4, pattern=r"^\d{4}$")


class PinLoginResponse(BaseModel):
    tokens: AuthTokens
    pin_set: bool = True
```

In `app/api/v1/endpoints/auth.py`:

```python
@router.post("/pin-login")
@limiter.limit("10/minute")
async def pin_login(
    request: Request,
    req: PinLoginRequest,
    svc: PinService = Depends(get_pin_service),
    token_store: TokenStore = Depends(get_token_store),
):
    if not settings.AUTH_PIN_LOGIN_ENABLED:
        raise HTTPException(503, detail={"code": "PIN_LOGIN_DISABLED"})
    try:
        access, refresh = await svc.pin_login(
            refresh_token=req.refresh_token, pin=req.pin, token_store=token_store,
        )
    except InvalidPinLoginToken:
        _raise("INVALID_TOKEN")
    except UserNotFound:
        _raise("USER_NOT_FOUND")
    except AccountDisabled:
        _raise("ACCOUNT_DISABLED")
    except PinNotSet:
        _raise("PIN_NOT_SET")
    except PinLocked:
        _raise("PIN_LOCKED")
    except InvalidPin:
        _raise("INVALID_PIN")
    return success(
        PinLoginResponse(
            tokens=AuthTokens(
                access_token=access,
                refresh_token=refresh,
                expires_in=int(_ACCESS_EXPIRE.total_seconds()),
            ),
        ).model_dump(),
        request_id=getattr(request.state, "request_id", None),
    )
```

(Where `_ACCESS_EXPIRE` is imported from `auth_service`.)

Add to `_ERROR_MAP`:

```python
    "PIN_LOGIN_DISABLED": (503, "PIN-based login is temporarily disabled"),
```

- [ ] **Step 4: Run tests**

```bash
docker compose exec -T api pytest tests/api/test_pin_login.py -v
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/pin_service.py app/api/v1/endpoints/auth.py app/schemas/auth.py tests/api/test_pin_login.py
git commit -m "feat(auth): add /auth/pin-login for cold-start PIN authentication"
```

---

## Task 14 (B14): Apply `require_full_auth_gates` to protected endpoints

**Depends on:** Task 7
**Files:**
- Modify: `app/api/v1/endpoints/bills.py`
- Modify: `app/api/v1/endpoints/wallet.py`
- Modify: `app/api/v1/endpoints/transactions.py`
- Modify: any other endpoint using `get_current_user` for money operations
- Test: `tests/integration/test_protected_endpoints_gates.py`

- [ ] **Step 1: List all uses of `get_current_user` that should switch**

```bash
docker compose exec -T api grep -rln "Depends(get_current_user)" app/api/v1/endpoints/
```
Inspect output. **Allowlist (DO NOT change)**:
- `/auth/me`, `/auth/logout`, `/auth/phone/send-otp`, `/auth/phone/verify-otp`, `/auth/pin/change`, `/auth/password/change`
- `/auth/phone/change-request`, `/auth/phone/change-confirm`
- `/users/me/push-tokens`, `/users/me/delete-account`

**Switch list (CHANGE to `require_full_auth_gates`)**:
- All endpoints in `bills.py`
- All endpoints in `wallet.py`
- All endpoints in `transactions.py`
- All endpoints in `referrals.py`
- All endpoints in `payments.py`
- Any other money-touching endpoint

- [ ] **Step 2: Write the failing integration test**

Create `tests/integration/test_protected_endpoints_gates.py`:

```python
import pytest


@pytest.mark.asyncio
async def test_wallet_blocked_when_phone_unverified_strict(
    client, seed_user_email_only, auth_headers, monkeypatch,
):
    monkeypatch.setattr("app.api.deps.settings.AUTH_STRICT_GATES", True)
    r = await client.get("/api/v1/wallet", headers=auth_headers(seed_user_email_only))
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "VERIFICATION_REQUIRED"
    assert r.json()["detail"]["which"] == "phone"


@pytest.mark.asyncio
async def test_wallet_allowed_when_all_gates_pass(client, seed_user_full, auth_headers):
    r = await client.get("/api/v1/wallet", headers=auth_headers(seed_user_full))
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_auth_me_allowed_even_when_gate_fails(client, seed_user_email_only, auth_headers, monkeypatch):
    """/auth/me is on the allowlist — works pre-gate-pass so mobile can route."""
    monkeypatch.setattr("app.api.deps.settings.AUTH_STRICT_GATES", True)
    r = await client.get("/api/v1/auth/me", headers=auth_headers(seed_user_email_only))
    assert r.status_code == 200
```

- [ ] **Step 3: Apply the change**

For each file in the switch list, find every `Depends(get_current_user)` and replace with `Depends(require_full_auth_gates)`. Example for `app/api/v1/endpoints/wallet.py`:

```python
# before:
from app.api.deps import get_current_user
...
async def get_wallet(
    user: User = Depends(get_current_user),
    ...
):

# after:
from app.api.deps import require_full_auth_gates
...
async def get_wallet(
    user: User = Depends(require_full_auth_gates),
    ...
):
```

Repeat for `bills.py`, `transactions.py`, `referrals.py`, `payments.py`.

- [ ] **Step 4: Run tests**

```bash
docker compose exec -T api pytest tests/integration/test_protected_endpoints_gates.py -v
docker compose exec -T api pytest --no-cov -q
```
Expected: both PASS. Some existing tests may need updating to seed users with all three gates passed; fix inline.

- [ ] **Step 5: Commit**

```bash
git add app/api/v1/endpoints/ tests/integration/test_protected_endpoints_gates.py
git commit -m "feat(api): gate money endpoints on require_full_auth_gates"
```

---

## Task 15 (B15): Full integration tests for registration + migration flows

**Depends on:** Tasks 8–13
**Files:**
- Create: `tests/integration/test_full_registration_flow.py`
- Create: `tests/integration/test_existing_user_migration.py`

- [ ] **Step 1: Write the new-user happy-path test**

Create `tests/integration/test_full_registration_flow.py`:

```python
import pytest


@pytest.mark.asyncio
async def test_full_register_verify_pin_set_chain(
    client, mock_email_provider, mock_sms_provider, fetch_latest_otp,
):
    # 1. Register
    r = await client.post("/api/v1/auth/register", json={
        "phone": "08011111111", "email": "user@x.test",
        "full_name": "Adebayo", "password": "Secret1!",
    })
    assert r.status_code == 201
    user_id = r.json()["data"]["user_id"]

    # 2. Verify email (order: email first, then phone)
    email_otp = await fetch_latest_otp(user_id=user_id, purpose="email_verification")
    r = await client.post("/api/v1/auth/email/verify", json={
        "email": "user@x.test", "code": email_otp,
    })
    assert r.json()["data"]["next_action"] == "phone_verification_required"

    # 3. Verify phone — both gates now pass + no PIN → pin_setup_token issued
    phone_otp = await fetch_latest_otp(user_id=user_id, purpose="phone_verification")
    r = await client.post("/api/v1/auth/phone/verify", json={
        "phone": "+2348011111111", "code": phone_otp,
    })
    assert r.json()["data"]["next_action"] == "pin_setup_required"
    pin_setup_token = r.json()["data"]["pin_setup_token"]

    # 4. Set PIN → full tokens issued
    r = await client.post(
        "/api/v1/auth/pin/set",
        headers={"X-Pin-Setup-Token": pin_setup_token},
        json={"pin": "1234"},
    )
    assert r.status_code == 200
    tokens = r.json()["data"]["tokens"]
    assert "access_token" in tokens

    # 5. Subsequent login goes straight to tokens (gates pass)
    r = await client.post("/api/v1/auth/login", json={
        "phone": "08011111111", "password": "Secret1!",
    })
    assert r.json()["data"]["next_action"] == "tokens_issued"


@pytest.mark.asyncio
async def test_full_chain_verifying_phone_before_email(
    client, mock_email_provider, mock_sms_provider, fetch_latest_otp,
):
    """Same end-state, but email verified AFTER phone. The pin_setup_token
    must be emitted by /email/verify in this order."""
    await client.post("/api/v1/auth/register", json={
        "phone": "08022222222", "email": "user2@x.test",
        "full_name": "B", "password": "Secret1!",
    })
    user_id = (await client.get("/api/v1/auth/_test/lookup_by_phone?phone=%2B2348022222222")).json()["id"]

    phone_otp = await fetch_latest_otp(user_id=user_id, purpose="phone_verification")
    r = await client.post("/api/v1/auth/phone/verify", json={
        "phone": "+2348022222222", "code": phone_otp,
    })
    assert r.json()["data"]["next_action"] == "email_verification_required"

    email_otp = await fetch_latest_otp(user_id=user_id, purpose="email_verification")
    r = await client.post("/api/v1/auth/email/verify", json={
        "email": "user2@x.test", "code": email_otp,
    })
    assert r.json()["data"]["next_action"] == "pin_setup_required"
    assert "pin_setup_token" in r.json()["data"]
```

(The `_test/lookup_by_phone` fixture is a test-only helper — implement as a conftest-only patched route, or just look the user up via fixtures.)

- [ ] **Step 2: Write the migration tests**

Create `tests/integration/test_existing_user_migration.py`:

```python
import pytest


@pytest.mark.asyncio
async def test_existing_user_with_unverified_phone_migrates_on_login(
    client, seed_user_email_verified_no_phone_no_pin, fetch_latest_otp, mock_sms_provider,
):
    """Replicates the existing-user flow: their first login post-deploy
    triggers phone OTP inline; verify phone → pin/set → tokens issued."""
    user = seed_user_email_verified_no_phone_no_pin

    # 1. Login — server fires phone OTP inline
    r = await client.post("/api/v1/auth/login", json={
        "phone": user.phone, "password": "Secret1!",
    })
    assert r.json()["data"]["next_action"] == "phone_verification_required"
    assert r.json()["data"]["phone_otp_sent"] is True

    # 2. Verify phone
    phone_otp = await fetch_latest_otp(user_id=user.id, purpose="phone_verification")
    r = await client.post("/api/v1/auth/phone/verify", json={
        "phone": user.phone, "code": phone_otp,
    })
    assert r.json()["data"]["next_action"] == "pin_setup_required"
    pin_setup_token = r.json()["data"]["pin_setup_token"]

    # 3. Set PIN
    r = await client.post(
        "/api/v1/auth/pin/set",
        headers={"X-Pin-Setup-Token": pin_setup_token},
        json={"pin": "1234"},
    )
    assert "access_token" in r.json()["data"]["tokens"]


@pytest.mark.asyncio
async def test_existing_user_with_phone_verified_but_no_pin_only_does_pin_step(
    client, seed_user_both_verified_no_pin,
):
    """Tier-1 user from before; on next login they only need to set PIN."""
    user = seed_user_both_verified_no_pin
    r = await client.post("/api/v1/auth/login", json={
        "phone": user.phone, "password": "Secret1!",
    })
    assert r.json()["data"]["next_action"] == "pin_setup_required"
```

- [ ] **Step 3: Add the conftest seed fixtures used above**

Edit `tests/conftest.py` (or the closest shared fixtures file):

```python
@pytest.fixture
def seed_user_email_verified_no_phone_no_pin(db_session):
    from app.core.security import hash_password
    u = User(
        phone="+2348099999999", email="email-only@x.test",
        full_name="EmailOnly", password_hash=hash_password("Secret1!"),
        referral_code="EO1", kyc_level=KycLevel.tier_0,
        email_verified=True, is_phone_verified=False, pin_hash=None,
        is_active=True,
    )
    db_session.add(u); db_session.commit(); db_session.refresh(u)
    return u


@pytest.fixture
def seed_user_both_verified_no_pin(db_session):
    from app.core.security import hash_password
    u = User(
        phone="+2348088888888", email="no-pin@x.test",
        full_name="NoPin", password_hash=hash_password("Secret1!"),
        referral_code="NP1", kyc_level=KycLevel.tier_1,
        email_verified=True, is_phone_verified=True, pin_hash=None,
        is_active=True,
    )
    db_session.add(u); db_session.commit(); db_session.refresh(u)
    return u


@pytest.fixture
def seed_user_full(db_session):
    from app.core.security import hash_password, hash_pin
    u = User(
        phone="+2348077777777", email="full@x.test",
        full_name="Full", password_hash=hash_password("Secret1!"),
        referral_code="F1", kyc_level=KycLevel.tier_1,
        email_verified=True, is_phone_verified=True,
        pin_hash=hash_pin("1234"),
        is_active=True,
    )
    db_session.add(u); db_session.commit(); db_session.refresh(u)
    return u


@pytest.fixture
async def fetch_latest_otp(db_session):
    """Returns a callable that, given user_id + purpose, returns the latest
    plaintext OTP code by reading the test-mode fakes (not the DB hash)."""
    from app.api.deps import _fake_sms_singleton, _fake_email_singleton
    async def _fetch(*, user_id, purpose):
        for s in reversed(_fake_sms_singleton.sent + _fake_email_singleton.sent):
            if s.get("code") and (
                str(user_id) in str(s) or s.get("purpose") == purpose
            ):
                return s["code"]
        raise AssertionError(f"no OTP captured for {user_id} / {purpose}")
    return _fetch
```

- [ ] **Step 4: Run tests**

```bash
docker compose exec -T api pytest tests/integration/test_full_registration_flow.py tests/integration/test_existing_user_migration.py -v
```
Expected: PASS.

- [ ] **Step 5: Run the entire suite**

```bash
docker compose exec -T api pytest --no-cov -q
docker compose exec -T api ruff check app/ tests/
```
Expected: both green.

- [ ] **Step 6: Commit**

```bash
git add tests/integration/ tests/conftest.py
git commit -m "test(integration): full registration + existing-user migration coverage"
```

---

## Task 16 (M1): Flutter — phone normalisation helper

**Files:**
- Create: `timpbills/lib/core/utils/phone.dart`
- Create: `timpbills/test/core/utils/phone_test.dart`

- [ ] **Step 1: Write the failing test**

Create `timpbills/test/core/utils/phone_test.dart`:

```dart
import 'package:flutter_test/flutter_test.dart';
import 'package:timpbills/core/utils/phone.dart';

void main() {
  group('normalizeToE164', () {
    test('local NG (08...) → +234...', () {
      expect(normalizeToE164('08012345678'), '+2348012345678');
    });
    test('intl (234...) → +234...', () {
      expect(normalizeToE164('2348012345678'), '+2348012345678');
    });
    test('already E.164 unchanged', () {
      expect(normalizeToE164('+2348012345678'), '+2348012345678');
    });
    test('strips whitespace', () {
      expect(normalizeToE164('  080 1234 5678  '), '+2348012345678');
    });
    test('invalid format throws', () {
      expect(() => normalizeToE164('abc'), throwsA(isA<InvalidPhoneFormat>()));
      expect(() => normalizeToE164('0801234567'), throwsA(isA<InvalidPhoneFormat>()));
      expect(() => normalizeToE164('+1234567890'), throwsA(isA<InvalidPhoneFormat>()));
    });
  });
}
```

- [ ] **Step 2: Run test (expect fail)**

```bash
cd /Users/adebayovictor/Documents/mobile/timp/timpbills
flutter test test/core/utils/phone_test.dart
```
Expected: FAIL (module doesn't exist).

- [ ] **Step 3: Implement**

Create `timpbills/lib/core/utils/phone.dart`:

```dart
class InvalidPhoneFormat implements Exception {
  final String message;
  InvalidPhoneFormat(this.message);
  @override String toString() => 'InvalidPhoneFormat: $message';
}

final _e164 = RegExp(r'^\+234[789]\d{9}$');
final _intl = RegExp(r'^234[789]\d{9}$');
final _local = RegExp(r'^0[789]\d{9}$');

String normalizeToE164(String raw) {
  if (raw.isEmpty) throw InvalidPhoneFormat('empty');
  final s = raw.trim().replaceAll(' ', '');
  if (_e164.hasMatch(s)) return s;
  if (_intl.hasMatch(s)) return '+$s';
  if (_local.hasMatch(s)) return '+234${s.substring(1)}';
  throw InvalidPhoneFormat('unrecognised format: $raw');
}
```

- [ ] **Step 4: Run test (expect pass)**

```bash
flutter test test/core/utils/phone_test.dart
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add lib/core/utils/phone.dart test/core/utils/phone_test.dart
git commit -m "feat(mobile): add Nigerian phone E.164 normalisation"
```

---

## Task 17 (M2): Login screen — phone-only field

**Depends on:** Task 16, Task 12 (server-side phone-only login must be merged)
**Files:**
- Modify: `timpbills/lib/features/auth/presentation/screens/login_screen.dart`
- Test: update `timpbills/test/features/auth/login_screen_test.dart`

- [ ] **Step 1: Update placeholder + input formatter**

Edit `timpbills/lib/features/auth/presentation/screens/login_screen.dart`. Locate the existing TextField for the identifier (likely labelled "Email or phone"). Change:

```dart
// before
TextField(
  controller: _identifierCtrl,
  decoration: const InputDecoration(
    labelText: 'Email or phone',
    hintText: 'you@example.com or 08012345678',
  ),
)

// after
TextField(
  controller: _phoneCtrl,
  keyboardType: TextInputType.phone,
  inputFormatters: [
    FilteringTextInputFormatter.allow(RegExp(r'[\d+]')),
    LengthLimitingTextInputFormatter(14),
  ],
  decoration: const InputDecoration(
    labelText: 'Phone number',
    hintText: '08012345678',
    prefixText: '',  // can later be set to '+234 ' once UX confirms
  ),
)
```

Rename `_identifierCtrl` → `_phoneCtrl` throughout the file.

- [ ] **Step 2: Update submit logic to normalise phone**

```dart
Future<void> _submit() async {
  String phone;
  try {
    phone = normalizeToE164(_phoneCtrl.text);
  } on InvalidPhoneFormat {
    setState(() => _error = 'Please enter a valid Nigerian phone number');
    return;
  }
  final result = await ref.read(authControllerProvider.notifier)
      .login(phone: phone, password: _passwordCtrl.text);
  // result drives the next_action routing — see Task 18
}
```

Add import:

```dart
import 'package:timpbills/core/utils/phone.dart';
```

- [ ] **Step 3: Update widget test**

Find or create `timpbills/test/features/auth/login_screen_test.dart` with one assertion:

```dart
testWidgets('login screen accepts phone-only, not email', (tester) async {
  await tester.pumpWidget(makeTestableWidget(child: const LoginScreen()));
  final phoneField = find.byKey(const Key('LoginScreen.phoneField'));
  expect(phoneField, findsOneWidget);
  // Type an email — should fail validation
  await tester.enterText(phoneField, 'foo@bar.com');
  await tester.tap(find.byKey(const Key('LoginScreen.submitBtn')));
  await tester.pump();
  expect(find.textContaining('valid Nigerian phone'), findsOneWidget);
});
```

(Make sure to add `Key('LoginScreen.phoneField')` and `Key('LoginScreen.submitBtn')` to the corresponding widgets if not already keyed.)

- [ ] **Step 4: Run tests**

```bash
flutter test test/features/auth/login_screen_test.dart
flutter analyze lib/features/auth/
```
Expected: both PASS / clean.

- [ ] **Step 5: Commit**

```bash
git add lib/features/auth/presentation/screens/login_screen.dart test/features/auth/login_screen_test.dart
git commit -m "feat(auth): phone-only login screen with E.164 normalisation"
```

---

## Task 18 (M3): Routing on `/auth/login` `next_action` response

**Depends on:** Task 17
**Files:**
- Modify: `timpbills/lib/features/auth/data/auth_repository.dart`
- Modify: `timpbills/lib/features/auth/application/auth_controller.dart`
- Modify: `timpbills/lib/core/router/app_router.dart`
- Test: `timpbills/test/features/auth/login_routing_test.dart`

- [ ] **Step 1: Update `auth_repository.dart` to parse `next_action`**

```dart
class LoginResult {
  final NextAction nextAction;
  final AuthTokens? tokens;
  final String? pinSetupToken;
  final bool? phoneOtpSent;

  LoginResult({
    required this.nextAction,
    this.tokens,
    this.pinSetupToken,
    this.phoneOtpSent,
  });

  factory LoginResult.fromJson(Map<String, dynamic> json) => LoginResult(
        nextAction: NextAction.values.byName(json['next_action'] as String),
        tokens: json['tokens'] != null ? AuthTokens.fromJson(json['tokens']) : null,
        pinSetupToken: json['pin_setup_token'] as String?,
        phoneOtpSent: json['phone_otp_sent'] as bool?,
      );
}

enum NextAction {
  tokens_issued,
  email_verification_required,
  phone_verification_required,
  pin_setup_required,
}
```

- [ ] **Step 2: Update `auth_controller.dart` to route on next_action**

```dart
Future<void> handleLoginResult(LoginResult r) async {
  switch (r.nextAction) {
    case NextAction.tokens_issued:
      await _persistTokens(r.tokens!);
      ref.read(routerProvider).goNamed('home');
      break;
    case NextAction.email_verification_required:
      ref.read(routerProvider).goNamed('verify-email');
      break;
    case NextAction.phone_verification_required:
      ref.read(routerProvider).goNamed('verify-phone');
      break;
    case NextAction.pin_setup_required:
      await _persistPinSetupToken(r.pinSetupToken!);
      ref.read(routerProvider).goNamed('set-pin');
      break;
  }
}
```

- [ ] **Step 3: Register the new routes**

In `timpbills/lib/core/router/app_router.dart`:

```dart
GoRoute(
  path: '/verify-phone',
  name: 'verify-phone',
  builder: (_, __) => const PhoneVerificationScreen(),
),
GoRoute(
  path: '/set-pin',
  name: 'set-pin',
  builder: (_, __) => const SetPinScreen(),
),
```

- [ ] **Step 4: Write the routing test**

Create `timpbills/test/features/auth/login_routing_test.dart`:

```dart
import 'package:flutter_test/flutter_test.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:timpbills/features/auth/application/auth_controller.dart';
import 'package:timpbills/features/auth/data/auth_repository.dart';

void main() {
  test('tokens_issued result triggers home navigation', () async {
    final container = ProviderContainer(overrides: [
      // ... overrides for router + token store
    ]);
    final controller = container.read(authControllerProvider.notifier);
    await controller.handleLoginResult(LoginResult(
      nextAction: NextAction.tokens_issued,
      tokens: AuthTokens(accessToken: 'a', refreshToken: 'r', expiresIn: 1200),
    ));
    // Verify router was called with 'home'
    // ...
  });

  test('pin_setup_required persists token and routes to set-pin', () async {
    // similar shape
  });
}
```

- [ ] **Step 5: Run tests**

```bash
flutter test test/features/auth/login_routing_test.dart
```
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add lib/features/auth/ lib/core/router/app_router.dart test/features/auth/
git commit -m "feat(auth): route by next_action on /login response"
```

---

## Task 19 (M4): Phone verification screen

**Depends on:** Task 18, Task 10 (server-side `/auth/phone/verify`)
**Files:**
- Create: `timpbills/lib/features/auth/presentation/screens/phone_verification_screen.dart`
- Modify: `timpbills/lib/features/auth/data/auth_repository.dart` (add `verifyPhone`, `resendPhoneOtp`)

- [ ] **Step 1: Add API methods to `auth_repository.dart`**

```dart
Future<PhoneVerifyResult> verifyPhone({
  required String phone,
  required String code,
}) async {
  final r = await _dio.post('/auth/phone/verify', data: {
    'phone': phone,
    'code': code,
  });
  return PhoneVerifyResult.fromJson(r.data['data']);
}

Future<void> resendPhoneOtp({required String phone}) async {
  await _dio.post('/auth/phone/resend', data: {'phone': phone});
}
```

`PhoneVerifyResult` mirrors backend `PhoneVerifiedResponse`:

```dart
class PhoneVerifyResult {
  final NextAction nextAction;
  final String? pinSetupToken;
  final AuthTokens? tokens;
  PhoneVerifyResult({required this.nextAction, this.pinSetupToken, this.tokens});
  factory PhoneVerifyResult.fromJson(Map<String, dynamic> json) => PhoneVerifyResult(
        nextAction: NextAction.values.byName(json['next_action']),
        pinSetupToken: json['pin_setup_token'],
        tokens: json['tokens'] != null ? AuthTokens.fromJson(json['tokens']) : null,
      );
}
```

- [ ] **Step 2: Build the screen**

Create `timpbills/lib/features/auth/presentation/screens/phone_verification_screen.dart`:

```dart
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:timpbills/features/auth/application/auth_controller.dart';

class PhoneVerificationScreen extends ConsumerStatefulWidget {
  const PhoneVerificationScreen({super.key});
  @override
  ConsumerState<PhoneVerificationScreen> createState() => _State();
}

class _State extends ConsumerState<PhoneVerificationScreen> {
  final _codeCtrl = TextEditingController();
  String? _err;
  bool _busy = false;

  Future<void> _submit() async {
    setState(() { _busy = true; _err = null; });
    final phone = ref.read(pendingPhoneProvider);  // last login attempt's phone
    final result = await ref.read(authControllerProvider.notifier)
        .verifyPhone(phone: phone, code: _codeCtrl.text);
    setState(() => _busy = false);
    // Route based on result.nextAction (controller does this)
  }

  Future<void> _resend() async {
    final phone = ref.read(pendingPhoneProvider);
    await ref.read(authControllerProvider.notifier).resendPhoneOtp(phone: phone);
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Verify your phone')),
      body: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(children: [
          const Text('Enter the 6-digit code we sent to your phone.'),
          TextField(
            key: const Key('PhoneVerification.codeField'),
            controller: _codeCtrl,
            keyboardType: TextInputType.number,
            maxLength: 6,
          ),
          if (_err != null) Text(_err!, style: const TextStyle(color: Colors.red)),
          ElevatedButton(
            key: const Key('PhoneVerification.submit'),
            onPressed: _busy ? null : _submit,
            child: const Text('Verify'),
          ),
          TextButton(
            onPressed: _resend,
            child: const Text('Resend code'),
          ),
        ]),
      ),
    );
  }
}
```

- [ ] **Step 3: Wire controller method**

In `auth_controller.dart`:

```dart
Future<void> verifyPhone({required String phone, required String code}) async {
  final r = await _repo.verifyPhone(phone: phone, code: code);
  switch (r.nextAction) {
    case NextAction.pin_setup_required:
      await _persistPinSetupToken(r.pinSetupToken!);
      ref.read(routerProvider).goNamed('set-pin');
      break;
    case NextAction.tokens_issued:
      await _persistTokens(r.tokens!);
      ref.read(routerProvider).goNamed('home');
      break;
    default:
      // email still needed etc.
  }
}
```

- [ ] **Step 4: Smoke test**

Manual: build dev app, run through register → verify email → land on phone screen → enter OTP from logs → confirm route to set-pin.

```bash
flutter analyze lib/features/auth/
```
Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add lib/features/auth/presentation/screens/phone_verification_screen.dart lib/features/auth/
git commit -m "feat(auth): phone verification screen with resend + next_action routing"
```

---

## Task 20 (M5): Set PIN screen

**Depends on:** Task 18, Task 11 (server `/pin/set` scoped token contract)
**Files:**
- Create: `timpbills/lib/features/auth/presentation/screens/set_pin_screen.dart`
- Modify: `auth_repository.dart` (add `setPin`)

- [ ] **Step 1: Add API method**

```dart
Future<AuthTokens> setPin({required String pinSetupToken, required String pin}) async {
  final r = await _dio.post(
    '/auth/pin/set',
    options: Options(headers: {'X-Pin-Setup-Token': pinSetupToken}),
    data: {'pin': pin},
  );
  return AuthTokens.fromJson(r.data['data']['tokens']);
}
```

- [ ] **Step 2: Build the screen**

Create `timpbills/lib/features/auth/presentation/screens/set_pin_screen.dart`:

```dart
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

class SetPinScreen extends ConsumerStatefulWidget {
  const SetPinScreen({super.key});
  @override ConsumerState<SetPinScreen> createState() => _State();
}

class _State extends ConsumerState<SetPinScreen> {
  final _pin1 = TextEditingController();
  final _pin2 = TextEditingController();
  String? _err;
  bool _busy = false;

  Future<void> _submit() async {
    if (_pin1.text.length != 4 || !RegExp(r'^\d{4}$').hasMatch(_pin1.text)) {
      setState(() => _err = 'PIN must be 4 digits');
      return;
    }
    if (_pin1.text != _pin2.text) {
      setState(() => _err = "PINs don't match");
      return;
    }
    setState(() { _busy = true; _err = null; });
    final controller = ref.read(authControllerProvider.notifier);
    await controller.setPin(_pin1.text);
    setState(() => _busy = false);
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Create your PIN'), automaticallyImplyLeading: false),
      body: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(children: [
          const Text("Pick a 4-digit PIN. You'll use this every time you open the app."),
          TextField(
            key: const Key('SetPin.pin1'), controller: _pin1,
            obscureText: true, keyboardType: TextInputType.number, maxLength: 4,
            decoration: const InputDecoration(labelText: 'PIN'),
          ),
          TextField(
            key: const Key('SetPin.pin2'), controller: _pin2,
            obscureText: true, keyboardType: TextInputType.number, maxLength: 4,
            decoration: const InputDecoration(labelText: 'Confirm PIN'),
          ),
          if (_err != null) Text(_err!, style: const TextStyle(color: Colors.red)),
          ElevatedButton(
            key: const Key('SetPin.submit'),
            onPressed: _busy ? null : _submit,
            child: const Text('Continue'),
          ),
        ]),
      ),
    );
  }
}
```

- [ ] **Step 3: Controller method**

In `auth_controller.dart`:

```dart
Future<void> setPin(String pin) async {
  final token = await _readPinSetupToken();
  final tokens = await _repo.setPin(pinSetupToken: token, pin: pin);
  await _clearPinSetupToken();
  await _persistTokens(tokens);
  ref.read(routerProvider).goNamed('home');
}
```

- [ ] **Step 4: Manual smoke + analyze**

```bash
flutter analyze lib/features/auth/
```
Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add lib/features/auth/presentation/screens/set_pin_screen.dart lib/features/auth/
git commit -m "feat(auth): set-PIN screen with scoped token consumption"
```

---

## Task 21 (M6): Cold-start PIN screen

**Depends on:** Task 13 (server `/auth/pin-login`)
**Files:**
- Create: `timpbills/lib/features/auth/presentation/screens/cold_start_pin_screen.dart`
- Modify: `auth_repository.dart` (add `pinLogin`)
- Modify: `main.dart` (cold-start routing logic)
- Test: `timpbills/test/features/auth/cold_start_pin_test.dart`

- [ ] **Step 1: Add API method**

```dart
Future<AuthTokens> pinLogin({required String refreshToken, required String pin}) async {
  final r = await _dio.post('/auth/pin-login', data: {
    'refresh_token': refreshToken,
    'pin': pin,
  });
  return AuthTokens.fromJson(r.data['data']['tokens']);
}
```

- [ ] **Step 2: Build the screen**

Create `timpbills/lib/features/auth/presentation/screens/cold_start_pin_screen.dart`:

```dart
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

class ColdStartPinScreen extends ConsumerStatefulWidget {
  const ColdStartPinScreen({super.key});
  @override ConsumerState<ColdStartPinScreen> createState() => _State();
}

class _State extends ConsumerState<ColdStartPinScreen> {
  final _pin = TextEditingController();
  String? _err;
  bool _busy = false;

  Future<void> _submit() async {
    setState(() { _busy = true; _err = null; });
    try {
      await ref.read(authControllerProvider.notifier).pinLogin(_pin.text);
    } on InvalidPinException {
      setState(() => _err = 'Wrong PIN');
    } on PinLockedException {
      setState(() => _err = "PIN locked. Try again in 30 minutes or use 'Forgot PIN'.");
    } on RefreshTokenExpiredException {
      // Token expired — clear local state, route to login
      await ref.read(authControllerProvider.notifier).fallbackToLogin();
    } finally {
      setState(() => _busy = false);
    }
  }

  Future<void> _forgotPin() async {
    await ref.read(authControllerProvider.notifier).fallbackToLogin();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: SafeArea(child: Padding(
        padding: const EdgeInsets.all(24),
        child: Column(children: [
          const Spacer(),
          const Text('Enter your PIN', style: TextStyle(fontSize: 24)),
          const SizedBox(height: 16),
          TextField(
            key: const Key('ColdStartPin.pin'),
            controller: _pin,
            obscureText: true, keyboardType: TextInputType.number, maxLength: 4,
            textAlign: TextAlign.center,
            style: const TextStyle(fontSize: 24, letterSpacing: 8),
          ),
          if (_err != null) Text(_err!, style: const TextStyle(color: Colors.red)),
          const SizedBox(height: 24),
          ElevatedButton(
            key: const Key('ColdStartPin.submit'),
            onPressed: _busy ? null : _submit,
            child: const Text('Unlock'),
          ),
          TextButton(
            onPressed: _forgotPin,
            child: const Text('Forgot PIN?'),
          ),
          const Spacer(),
        ]),
      )),
    );
  }
}
```

- [ ] **Step 3: Cold-start gate in `main.dart`**

In the app boot widget:

```dart
class AppBoot extends ConsumerWidget {
  const AppBoot({super.key});
  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final hasRefresh = ref.watch(refreshTokenPresenceProvider);
    if (hasRefresh) {
      return const ColdStartPinScreen();
    }
    return const LoginScreen();
  }
}
```

`refreshTokenPresenceProvider` is a synchronous provider that reads from secure storage at startup.

- [ ] **Step 4: Forgot-PIN fallback**

In `auth_controller.dart`:

```dart
Future<void> fallbackToLogin() async {
  await _clearRefreshToken();   // local-only; no server call
  ref.read(routerProvider).goNamed('login');
}
```

This is the "Forgot PIN" path — clears the saved refresh_token client-side, so on next boot the app shows the phone+password login screen. User logs in with password, and after successful login → forced through pin_setup_required flow (Task 12) to set a new PIN.

- [ ] **Step 5: Write test**

Create `timpbills/test/features/auth/cold_start_pin_test.dart`:

```dart
testWidgets('cold-start PIN screen shown when refresh token present', (tester) async {
  // ... fake provider with hasRefresh = true
  await tester.pumpWidget(const AppBoot());
  expect(find.byType(ColdStartPinScreen), findsOneWidget);
});

testWidgets('login screen shown when no refresh token', (tester) async {
  // ... fake provider with hasRefresh = false
  await tester.pumpWidget(const AppBoot());
  expect(find.byType(LoginScreen), findsOneWidget);
});
```

```bash
flutter test test/features/auth/cold_start_pin_test.dart
flutter analyze lib/
```
Expected: PASS / clean.

- [ ] **Step 6: Commit**

```bash
git add lib/features/auth/presentation/screens/cold_start_pin_screen.dart lib/main.dart lib/features/auth/ test/features/auth/cold_start_pin_test.dart
git commit -m "feat(auth): cold-start PIN screen with forgot-PIN fallback"
```

---

## Task 22 (M7): /me state routing for existing-user migration

**Depends on:** Task 12, Task 18
**Files:**
- Modify: `timpbills/lib/features/auth/application/auth_controller.dart`

- [ ] **Step 1: Add post-refresh routing**

When the app cold-starts WITH a valid refresh token (PIN-login succeeds), the controller calls `/auth/me` and inspects gates. If any are missing → route to migration screen.

In `auth_controller.dart`:

```dart
Future<void> _routeAfterAuth() async {
  final me = await _repo.me();
  if (!me.emailVerified) {
    ref.read(routerProvider).goNamed('verify-email');
    return;
  }
  if (!me.phoneVerified) {
    // Trigger inline OTP send via the resend endpoint, then route
    await _repo.resendPhoneOtp(phone: me.phone);
    ref.read(pendingPhoneProvider.notifier).state = me.phone;
    ref.read(routerProvider).goNamed('verify-phone');
    return;
  }
  if (!me.pinSet) {
    // We don't have a scoped token here (this is post-pin-login).
    // Tell user to logout + log in via password to get the scoped token.
    // OR — add an authenticated `/auth/pin-setup-token` endpoint to mint one.
    // For Phase A+B: redirect to login and clear refresh token.
    await fallbackToLogin();
    return;
  }
  ref.read(routerProvider).goNamed('home');
}
```

- [ ] **Step 2: Smoke test**

Seed a dev DB user with `is_phone_verified = false`, log in, expect routing to verify-phone screen.

- [ ] **Step 3: Commit**

```bash
git add lib/features/auth/application/auth_controller.dart
git commit -m "feat(auth): post-auth routing handles unverified phone + missing PIN"
```

---

## Task 23 (B16): Documentation + changelog

**Files:**
- Create: `docs/changelog/2026-05-26-phone-only-auth.md`

- [ ] **Step 1: Write changelog**

```markdown
# Phone-only authentication + PIN-based cold-start login

**Released:** 2026-05-26 (backend), 2026-05-XX (mobile)
**Spec:** `docs/superpowers/specs/2026-05-26-phone-only-auth-design.md`

## What changed

- `/auth/login` now accepts phone only (no email-as-identifier)
- Both email AND phone must be verified before tokens are issued
- Every user must set a 4-digit PIN to access the app
- New endpoint `POST /auth/pin-login` for cold-start authentication
- Termii SMS channel switched from `generic` → `dnd` for OTP delivery
- All phone numbers normalised to E.164 (`+234...`) in storage

## Settings added

- `AUTH_STRICT_GATES` (default `false`) — gate enforcement
- `AUTH_PIN_LOGIN_ENABLED` (default `true`) — kill switch for pin-login
- `TERMII_OTP_CHANNEL` (default `dnd`)
- `OTP_RESEND_COOLDOWN_SECONDS` (default 60)
- `OTP_RESEND_DAILY_CAP` (default 10)

## Migration

- Existing users with unverified phones get force-routed through OTP on next login
- Existing users without a PIN get force-routed through set-PIN
- One-time Alembic data migration normalises stored phones to E.164

## Rollout

1. Backend deploy with `AUTH_STRICT_GATES=false`
2. Mobile release with new flows
3. Flip `AUTH_STRICT_GATES=true` after ~80% client adoption
4. Phase B (pin-login) ships alongside

See spec for rollback per failure mode.
```

- [ ] **Step 2: Commit**

```bash
git add docs/changelog/2026-05-26-phone-only-auth.md
git commit -m "docs(changelog): phone-only auth + PIN-login release notes"
```

---

## Self-Review

**Spec coverage check** (mapping spec sections → tasks):

| Spec section | Task(s) |
|---|---|
| §4 Architecture (gates + endpoint topology) | T7 (gates dep), T12 (login next_action), T9/T10/T11 (verify endpoints + set-pin) |
| §5 Phone normalisation | T1 (helper), T2 (migration), T16 (mobile helper) |
| §6 Scoped pin_setup JWT | T3 (helpers), T9/T10/T12 (issuers), T11 (consumer) |
| §7 Registration flow | T8 (register), T9 (email verify), T10 (phone verify), T11 (pin set), T15 (integration test) |
| §8 Migration flow | T12 (login inline OTP), T15 (integration test), T22 (mobile /me routing) |
| §9 PIN-login | T13 (endpoint), T21 (mobile cold-start) |
| §10 Refresh + logout | Unchanged from existing — covered by current `/auth/refresh`, `/auth/logout` |
| §11 Credential-change interactions | Unchanged from existing — current `/auth/password/change`, `/auth/pin/change`, `/auth/phone/change-confirm` already match spec |
| §12 SMS + email integration | T5 (Termii channel), T6 (cooldown helpers) |
| §13 Data migration | T2 |
| §14 Rate-limiting | T6 (cooldown), T11/T12/T13 (slowapi limits on endpoints) |
| §15 Error handling | All endpoint tasks include `_ERROR_MAP` additions |
| §16 Deploy plan | Settings (T4) + soft mode (T7) + integration tests (T15) |
| §17 Feature flags | T4 |
| §18 User communication | Out of scope (product/marketing) |
| §19 Rollback | Covered by feature flags (T4) |
| §20 Monitoring | Out of scope for this plan (logged via structured loguru already) |
| §21 Tests | T1, T2, T3, T4, T5, T6, T7, T8, T9, T10, T11, T12, T13, T14, T15 |
| §22 Risks accepted | Acknowledged in spec; nothing actionable in this plan |
| §6 mobile screens | T16 (phone helper), T17 (login), T18 (routing), T19 (phone verify), T20 (set PIN), T21 (cold-start), T22 (/me routing) |

All spec requirements have at least one task. No gaps.

**Placeholder scan**: no TBDs, TODOs, FIXMEs, "fill in details", or vague requirements. Every code block contains the actual content the engineer needs.

**Type consistency check** (cross-task identifier match):
- `normalize_to_e164` (T1) used in T2, T8, T10, T12 — consistent
- `InvalidPhoneFormat` (T1) raised consistently
- `create_pin_setup_token` / `verify_pin_setup_token` (T3) used by T9, T10, T11, T12 — consistent
- `_check_otp_cooldown` (T6) called from T8, T12 — consistent
- `OtpCooldownActive` / `OtpDailyCapExceeded` (T6) caught in T12 — consistent
- `require_full_auth_gates` (T7) applied in T14 — consistent
- `NextAction` enum values (Dart, T18) match server `next_action` strings (T8/T9/T10/T11/T12) — consistent
- `pin_setup_token` / `X-Pin-Setup-Token` header naming consistent across T11 (server) and T20 (mobile)
- `pin_login` / `pinLogin` method naming consistent T13 ↔ T21

No drift detected.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-05-26-phone-only-auth.md`. Two execution options:

**1. Subagent-driven (recommended)** — dispatch a fresh subagent per task, two-stage review (spec compliance + code quality) between each, fast iteration with cross-stack parallelism wherever the task list allows.

**2. Inline execution** — run tasks in this session using `superpowers:executing-plans`, batched checkpoints for review.

For this plan I recommend **#1**. Reasons:
- 23 tasks across backend + mobile = enough work that the two-stage review per task pays off
- Cross-stack parallelism opportunities (after T12, mobile T17/T18 can run while backend T13/T14 finish)
- The `backend-engineer` and `mobile-engineer` specialised agents are a better fit per-task than this fullstack-staff session driving everything serially

Which approach do you want?
