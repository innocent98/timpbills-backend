"""
Full-flow E2E backend test suite (B5).

Each test simulates a complete user journey, asserting DB state and
fake SMS/email state at each step.  All tests share the `client` fixture
defined in tests/api/test_auth_flow.py via the conftest-provided fixtures
(db_session).  This file defines its own `client` fixture that mirrors the
one in test_auth_flow.py so this module is fully self-contained.
"""
import pytest
import pytest_asyncio
from datetime import datetime, timedelta
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.api.deps import (
    get_db,
    get_redis,
    get_token_store,
    get_email_provider,
    get_sms_provider,
    reset_fake_sms,
    reset_fake_email,
)
from app.integrations.email.fake import FakeEmailClient
from app.integrations.termii.fake import FakeTermiiClient
from app.core.limiter import limiter
from app.db.models.otp import OtpCode, OtpPurpose
from app.db.models.user import User, KycLevel
from fakeredis.aioredis import FakeRedis
from app.services.token_store import RedisTokenStore

# Module-level fake providers reused by all tests in this file
_e2e_email_client = FakeEmailClient()
_e2e_sms_client = FakeTermiiClient()


@pytest_asyncio.fixture
async def client(db_session):
    """Async HTTP client with all external dependencies overridden."""

    def _get_db():
        try:
            yield db_session
        finally:
            pass

    fake_redis = FakeRedis(decode_responses=True)

    def _get_token_store():
        return RedisTokenStore(redis=fake_redis)

    def _get_redis():
        return fake_redis

    def _get_email():
        return _e2e_email_client

    def _get_sms():
        return _e2e_sms_client

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_token_store] = _get_token_store
    app.dependency_overrides[get_redis] = _get_redis
    app.dependency_overrides[get_email_provider] = _get_email
    app.dependency_overrides[get_sms_provider] = _get_sms

    # Reset fake providers so each test starts clean
    _e2e_email_client.sent.clear()
    _e2e_sms_client.sent.clear()
    reset_fake_sms()
    reset_fake_email()

    # Disable rate-limiting for all non-rate-limit tests
    limiter.enabled = False

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

    limiter.enabled = True
    await fake_redis.aclose()
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def rate_limited_client(db_session):
    """Client with rate limiting enabled for rate-limit enforcement tests."""

    def _get_db():
        try:
            yield db_session
        finally:
            pass

    fake_redis = FakeRedis(decode_responses=True)

    def _get_token_store():
        return RedisTokenStore(redis=fake_redis)

    def _get_redis():
        return fake_redis

    def _get_email():
        return _e2e_email_client

    def _get_sms():
        return _e2e_sms_client

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_token_store] = _get_token_store
    app.dependency_overrides[get_redis] = _get_redis
    app.dependency_overrides[get_email_provider] = _get_email
    app.dependency_overrides[get_sms_provider] = _get_sms

    _e2e_email_client.sent.clear()
    _e2e_sms_client.sent.clear()
    reset_fake_sms()
    reset_fake_email()

    limiter.enabled = True
    limiter.reset()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

    limiter.enabled = False
    await fake_redis.aclose()
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Helper: seed a user all the way to email-verified + pin-set
# ---------------------------------------------------------------------------

def _resolve_test_db():
    """Resolve the test DB session that the active ``client`` fixture
    bound via ``app.dependency_overrides[get_db]``. Helpers run *inside*
    a test, so an override is guaranteed to be installed."""
    override = app.dependency_overrides.get(get_db)
    if override is None:  # pragma: no cover — guards against fixture drift
        raise RuntimeError("_seed_logged_in_user requires the client fixture")
    gen = override()
    try:
        return next(gen)
    except StopIteration as exc:  # pragma: no cover
        raise RuntimeError("get_db override yielded nothing") from exc


async def _seed_logged_in_user(
    client: AsyncClient,
    *,
    email: str = "e@e.co",
    phone: str = "+2348000010001",
    password: str = "Secret1!",
    pin: str = "8527",
) -> tuple[dict, dict]:
    """
    Register → flip phone-verified + PIN in the DB (existing-user
    migration path) → verify email → receive full tokens.

    B9 changed ``/auth/email/verify``: a fresh registration no longer
    yields tokens — only the *migration* branch does (email last gate,
    phone already verified, PIN already set). Pre-stamping the row
    keeps the helper's contract (returns ``(tokens, auth_headers)``)
    without depending on B10 (/phone/verify unauth) or B11 (rewritten
    /pin/set) which are still pending.

    Returns (tokens_dict, auth_headers).
    """
    from app.core.security import hash_pin

    r = await client.post(
        "/api/v1/auth/register",
        json={
            "full_name": "Test User",
            "phone": phone,
            "email": email,
            "password": password,
        },
    )
    assert r.status_code == 201, f"register failed: {r.text}"

    # Migration-path pre-stamp: phone verified + PIN set so that the
    # /email/verify call below takes the ``tokens_issued`` branch.
    # We intentionally leave ``kyc_level`` at tier_0 — many downstream
    # tests assume the ₦50,000 cap and the new helper must not silently
    # promote them. (Real flow promotes tier_1 inside verify_phone_otp.)
    db = _resolve_test_db()
    user = db.query(User).filter(User.email == email).first()
    assert user is not None, "register did not persist user"
    user.is_phone_verified = True
    user.pin_hash = hash_pin(pin)
    db.commit()

    code = _e2e_email_client.sent[-1].code_or_body
    r2 = await client.post(
        "/api/v1/auth/email/verify", json={"email": email, "code": code}
    )
    assert r2.status_code == 200, f"email verify failed: {r2.text}"
    data = r2.json()["data"]
    assert data["next_action"] == "tokens_issued", data
    tokens = data["tokens"]

    auth = {"Authorization": f"Bearer {tokens['access_token']}"}
    return tokens, auth


# ===========================================================================
# 1. Happy path — full new-user journey
# ===========================================================================

@pytest.mark.asyncio
async def test_full_new_user_journey(client, db_session):
    """
    register → email verify → set pin → send phone OTP → verify phone OTP
    Assert DB state at each step.
    """
    email = "happy@path.co"
    phone = "+2348011111101"

    # Step 1: Register
    r = await client.post(
        "/api/v1/auth/register",
        json={
            "full_name": "Happy Path",
            "phone": phone,
            "email": email,
            "password": "Secret1!",
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["success"] is True
    assert r.json()["data"]["email"] == email
    assert r.json()["data"]["phone"] == phone

    # B8: register now sends BOTH email and phone OTPs.
    assert len(_e2e_email_client.sent) == 1
    email_code = _e2e_email_client.sent[-1].code_or_body
    assert len(email_code) == 6 and email_code.isdigit()
    assert len(_e2e_sms_client.sent) == 1
    register_sms_count = 1

    # DB: user exists, not verified yet
    user = db_session.query(User).filter(User.email == email).first()
    assert user is not None
    assert user.email_verified is False
    assert user.is_phone_verified is False
    assert user.pin_hash is None
    assert user.kyc_level == KycLevel.tier_0

    # Step 2: Verify email — B9 contract.  Fresh registration, phone
    # still unverified → response signals ``phone_verification_required``
    # and emits NO tokens / pin_setup_token. Mobile bounces to the
    # phone-OTP screen.
    r2 = await client.post(
        "/api/v1/auth/email/verify", json={"email": email, "code": email_code}
    )
    assert r2.status_code == 200, r2.text
    data2 = r2.json()["data"]
    assert data2["email_verified"] is True
    assert data2["phone_verified"] is False
    assert data2["pin_set"] is False
    assert data2["next_action"] == "phone_verification_required"
    assert data2.get("tokens") is None
    assert data2.get("pin_setup_token") is None

    # DB: email now verified
    db_session.refresh(user)
    assert user.email_verified is True

    # NOTE: B10 (/phone/verify unauthenticated) + B11 (rewritten /pin/set
    # scoped-token) are still pending. The rest of the journey — phone
    # OTP → PIN setup → full tokens — exercises those flows once they
    # land. For now we stop after asserting the phone_verification_required
    # branch, which is the new contract B9 introduced.
    assert user.is_phone_verified is False
    assert user.pin_hash is None
    assert user.kyc_level == KycLevel.tier_0
    # Quiet linter — register_sms_count is part of the original step-5
    # block that will be reintroduced after B10/B11.
    _ = register_sms_count


# ===========================================================================
# 2. Login → refresh → replay revokes all
# ===========================================================================

@pytest.mark.asyncio
async def test_login_then_refresh_rotation(client):
    """
    Login → refresh with new token → replay old token → 401 + all sessions revoked.
    """
    tokens_seed, _ = await _seed_logged_in_user(
        client, email="refresh@test.co", phone="+2348011111102"
    )

    # Login to get a fresh token pair (device 1) — B12: phone field.
    r_login = await client.post(
        "/api/v1/auth/login",
        json={"phone": "+2348011111102", "password": "Secret1!"},
    )
    assert r_login.status_code == 200, r_login.text
    tokens1 = r_login.json()["data"]["tokens"]

    # Also login from a second "device"
    r_login2 = await client.post(
        "/api/v1/auth/login",
        json={"phone": "+2348011111102", "password": "Secret1!"},
    )
    assert r_login2.status_code == 200, r_login2.text
    tokens2 = r_login2.json()["data"]["tokens"]

    # Refresh device 1 — should succeed and return new tokens
    r_refresh = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens1["refresh_token"]}
    )
    assert r_refresh.status_code == 200, r_refresh.text
    new_tokens = r_refresh.json()["data"]
    # New refresh token must differ from the old one (rotation)
    assert new_tokens["refresh_token"] != tokens1["refresh_token"]

    # Replay old refresh token → 401 and revoke_all triggers
    r_replay = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens1["refresh_token"]}
    )
    assert r_replay.status_code == 401, r_replay.text
    assert r_replay.json()["error"]["code"] == "INVALID_TOKEN"

    # After revoke_all: device 2's token should also be dead
    r_device2 = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens2["refresh_token"]}
    )
    assert r_device2.status_code == 401, r_device2.text

    # The rotated token (new_tokens) was also in-store when revoke_all fired → also dead
    r_new_after = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": new_tokens["refresh_token"]}
    )
    assert r_new_after.status_code == 401, r_new_after.text


# ===========================================================================
# 3. Password reset revokes all sessions
# ===========================================================================

@pytest.mark.asyncio
async def test_password_reset_revokes_sessions(client):
    """
    Two logins → forgot password → reset → old refresh tokens 401 → new creds work.
    """
    old_password = "Secret1!"
    new_password = "NewSecret2!"
    phone = "+2348011111103"
    email = "reset@sessions.co"

    await _seed_logged_in_user(client, email=email, phone=phone, password=old_password)

    # Login device 1 — B12: phone-only field.
    r1 = await client.post(
        "/api/v1/auth/login", json={"phone": phone, "password": old_password}
    )
    assert r1.status_code == 200
    tokens_d1 = r1.json()["data"]["tokens"]

    # Login device 2 (same phone — a single account; two refresh tokens
    # simulate two devices on the same account).
    r2 = await client.post(
        "/api/v1/auth/login", json={"phone": phone, "password": old_password}
    )
    assert r2.status_code == 200
    tokens_d2 = r2.json()["data"]["tokens"]

    # Forgot password — uses SMS (phone identifier)
    _e2e_sms_client.sent.clear()
    r_forgot = await client.post(
        "/api/v1/auth/password/forgot", json={"identifier": phone}
    )
    assert r_forgot.status_code == 200, r_forgot.text

    # The reset code arrives via SMS
    assert len(_e2e_sms_client.sent) == 1
    reset_code = _e2e_sms_client.sent[-1].code_or_message

    # Reset password
    r_reset = await client.post(
        "/api/v1/auth/password/reset",
        json={"identifier": phone, "code": reset_code, "new_password": new_password},
    )
    assert r_reset.status_code == 200, r_reset.text

    # Device 1 refresh → 401 (sessions revoked)
    r_d1 = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens_d1["refresh_token"]}
    )
    assert r_d1.status_code == 401, r_d1.text

    # Device 2 refresh → 401
    r_d2 = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens_d2["refresh_token"]}
    )
    assert r_d2.status_code == 401, r_d2.text

    # Login with NEW password → 200 (B12: phone-only field).
    r_new_login = await client.post(
        "/api/v1/auth/login", json={"phone": phone, "password": new_password}
    )
    assert r_new_login.status_code == 200, r_new_login.text

    # Login with OLD password → 401
    r_old_login = await client.post(
        "/api/v1/auth/login", json={"phone": phone, "password": old_password}
    )
    assert r_old_login.status_code == 401, r_old_login.text
    assert r_old_login.json()["error"]["code"] == "INVALID_CREDENTIALS"


# ===========================================================================
# 4. Email OTP failure paths
# ===========================================================================

@pytest.mark.asyncio
async def test_email_otp_invalid_code(client):
    """Wrong code → 400 INVALID_OTP; 4th attempt → 429 OTP_ATTEMPTS_EXCEEDED."""
    email = "badotp@test.co"
    await client.post(
        "/api/v1/auth/register",
        json={
            "full_name": "Bad OTP",
            "phone": "+2348011111104",
            "email": email,
            "password": "Secret1!",
        },
    )

    # 3 wrong attempts should all return INVALID_OTP (400)
    for i in range(3):
        r = await client.post(
            "/api/v1/auth/email/verify", json={"email": email, "code": "000000"}
        )
        assert r.status_code == 400, f"attempt {i+1}: {r.text}"
        assert r.json()["error"]["code"] == "INVALID_OTP"

    # 4th attempt → OTP_ATTEMPTS_EXCEEDED (429)
    r4 = await client.post(
        "/api/v1/auth/email/verify", json={"email": email, "code": "000000"}
    )
    assert r4.status_code == 429, r4.text
    assert r4.json()["error"]["code"] == "OTP_ATTEMPTS_EXCEEDED"


@pytest.mark.asyncio
async def test_email_otp_expired(client, db_session):
    """Manually expire OTP in DB → verify returns 410 OTP_EXPIRED."""
    email = "expired@test.co"
    await client.post(
        "/api/v1/auth/register",
        json={
            "full_name": "Expired OTP",
            "phone": "+2348011111105",
            "email": email,
            "password": "Secret1!",
        },
    )

    # Capture the real code before expiring
    real_code = _e2e_email_client.sent[-1].code_or_body

    # Fast-forward expires_at to the past
    user = db_session.query(User).filter(User.email == email).first()
    otp = (
        db_session.query(OtpCode)
        .filter(
            OtpCode.user_id == user.id,
            OtpCode.purpose == OtpPurpose.email_verification,
            OtpCode.used_at.is_(None),
        )
        .first()
    )
    otp.expires_at = datetime.utcnow() - timedelta(minutes=10)
    db_session.commit()

    r = await client.post(
        "/api/v1/auth/email/verify", json={"email": email, "code": real_code}
    )
    assert r.status_code == 410, r.text
    assert r.json()["error"]["code"] == "OTP_EXPIRED"


# ===========================================================================
# 5. Email OTP resend flow
# ===========================================================================

@pytest.mark.asyncio
async def test_email_otp_resend(client):
    """
    Register → resend → verify with NEW code succeeds.

    Note on behaviour: verify_email_otp queries OtpCodes ordered by
    created_at DESC and picks the most recent unused one (latest-wins).
    After a resend, the old code is still unused in the DB, but the
    latest-wins query returns only the newest OTP.  Therefore:
      - verifying with the OLD code → INVALID_OTP (it is not the latest)
      - verifying with the NEW code → 200
    """
    email = "resend@flow.co"
    await client.post(
        "/api/v1/auth/register",
        json={
            "full_name": "Resend User",
            "phone": "+2348011111106",
            "email": email,
            "password": "Secret1!",
        },
    )
    assert len(_e2e_email_client.sent) == 1
    old_code = _e2e_email_client.sent[-1].code_or_body

    # Resend
    r_resend = await client.post("/api/v1/auth/email/resend", json={"email": email})
    assert r_resend.status_code == 200, r_resend.text
    assert r_resend.json()["data"]["ok"] is True
    assert len(_e2e_email_client.sent) == 2
    new_code = _e2e_email_client.sent[-1].code_or_body

    # Old code should now be rejected because the latest-wins query returns the
    # new OTP, not the old one.
    r_old = await client.post(
        "/api/v1/auth/email/verify", json={"email": email, "code": old_code}
    )
    assert r_old.status_code == 400, r_old.text
    assert r_old.json()["error"]["code"] == "INVALID_OTP"

    # New code succeeds — B9: fresh user verifying email returns
    # ``phone_verification_required`` (no tokens yet) since phone is
    # still unverified. The body still confirms email_verified=True.
    r_new = await client.post(
        "/api/v1/auth/email/verify", json={"email": email, "code": new_code}
    )
    assert r_new.status_code == 200, r_new.text
    data = r_new.json()["data"]
    assert data["email_verified"] is True
    assert data["next_action"] == "phone_verification_required"
    assert data.get("tokens") is None


# ===========================================================================
# 6. Phone verification guards
# ===========================================================================

@pytest.mark.asyncio
async def test_phone_verify_without_auth_returns_401(client):
    """Both phone OTP endpoints require a valid Bearer token."""
    r_send = await client.post("/api/v1/auth/phone/send-otp")
    assert r_send.status_code == 401, r_send.text

    r_verify = await client.post(
        "/api/v1/auth/phone/verify-otp", json={"code": "123456"}
    )
    assert r_verify.status_code == 401, r_verify.text


@pytest.mark.asyncio
async def test_phone_verify_already_verified(client):
    """Calling send-otp when phone is already verified returns 409 PHONE_ALREADY_VERIFIED."""
    email = "alreadyver@test.co"
    phone = "+2348011111107"

    # Seed: register → email verify → set pin → verify phone
    tokens, auth = await _seed_logged_in_user(client, email=email, phone=phone)

    # Send and verify phone OTP
    await client.post("/api/v1/auth/phone/send-otp", headers=auth)
    sms_code = _e2e_sms_client.sent[-1].code_or_message
    r_pv = await client.post(
        "/api/v1/auth/phone/verify-otp", json={"code": sms_code}, headers=auth
    )
    assert r_pv.status_code == 200, r_pv.text

    # Update auth to use the new access token returned from phone verify
    new_access = r_pv.json()["data"]["tokens"]["access_token"]
    auth2 = {"Authorization": f"Bearer {new_access}"}

    # Try to send phone OTP again → PHONE_ALREADY_VERIFIED
    r_again = await client.post("/api/v1/auth/phone/send-otp", headers=auth2)
    assert r_again.status_code == 409, r_again.text
    assert r_again.json()["error"]["code"] == "PHONE_ALREADY_VERIFIED"


# ===========================================================================
# 7. Registration duplicate detection
# ===========================================================================

@pytest.mark.asyncio
async def test_register_duplicate_phone_or_email(client):
    """Duplicate phone OR email both return 409 USER_ALREADY_EXISTS."""
    base = {
        "full_name": "Dup User",
        "phone": "+2348011111108",
        "email": "dupuser@test.co",
        "password": "Secret1!",
    }
    r1 = await client.post("/api/v1/auth/register", json=base)
    assert r1.status_code == 201

    # Duplicate phone, different email
    r2 = await client.post(
        "/api/v1/auth/register",
        json={**base, "email": "other@test.co"},
    )
    assert r2.status_code == 409, r2.text
    assert r2.json()["error"]["code"] == "USER_ALREADY_EXISTS"

    # Different phone, duplicate email
    r3 = await client.post(
        "/api/v1/auth/register",
        json={**base, "phone": "+2348011111109"},
    )
    assert r3.status_code == 409, r3.text
    assert r3.json()["error"]["code"] == "USER_ALREADY_EXISTS"


# ===========================================================================
# 8. Rate-limit enforcement
# ===========================================================================

@pytest.mark.asyncio
async def test_register_rate_limit_exceeded(rate_limited_client):
    """4th /register call in a row (limit 3/min per IP) should return 429."""
    last = None
    for i in range(4):
        last = await rate_limited_client.post(
            "/api/v1/auth/register",
            json={
                "full_name": f"Rate User {i}",
                "phone": f"+234801111{1110 + i:04d}",
                "email": f"rateuser{i}@test.co",
                "password": "Secret1!",
            },
        )
    assert last.status_code == 429, last.text


@pytest.mark.asyncio
async def test_login_rate_limit_exceeded(rate_limited_client):
    """6th /login call with bad creds should return 429 (limit 5/min).
    B12: phone field — uses a never-registered NG phone so password fails."""
    payload = {"phone": "+2348099999988", "password": "WrongPass1!"}
    last = None
    for _ in range(6):
        last = await rate_limited_client.post("/api/v1/auth/login", json=payload)
    assert last.status_code == 429, last.text


# ===========================================================================
# 9. Envelope shape assertions
# ===========================================================================

@pytest.mark.asyncio
async def test_error_envelope_shape(client):
    """Invalid password → 422; response must match the error envelope."""
    r = await client.post(
        "/api/v1/auth/register",
        json={
            "full_name": "Envelope Test",
            "phone": "+2348011112001",
            "email": "envelope@test.co",
            "password": "short",  # fails validation
        },
    )
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["success"] is False
    assert body["data"] is None
    assert body["error"] is not None
    assert "code" in body["error"]
    assert "message" in body["error"]
    assert "details" in body["error"]
    assert body["request_id"] is not None


@pytest.mark.asyncio
async def test_success_envelope_shape(client):
    """Valid register → 201; response must match the success envelope."""
    r = await client.post(
        "/api/v1/auth/register",
        json={
            "full_name": "Envelope OK",
            "phone": "+2348011112002",
            "email": "envelope_ok@test.co",
            "password": "Secret1!",
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["success"] is True
    assert body["data"] is not None
    assert body["error"] is None
    assert body["request_id"] is not None


# ===========================================================================
# 10. Idempotency on register
# ===========================================================================

@pytest.mark.asyncio
async def test_register_is_idempotent_on_id(client):
    """
    Idempotency-Key header is not implemented in the current codebase.
    The second identical call returns 409 USER_ALREADY_EXISTS, not a cached
    201.  This test documents that behaviour.

    If idempotent register is added later, this test should be updated to
    assert that both calls return 201 with the same response body.
    """
    payload = {
        "full_name": "Idempotent User",
        "phone": "+2348011112003",
        "email": "idempotent@test.co",
        "password": "Secret1!",
    }
    r1 = await client.post(
        "/api/v1/auth/register",
        json=payload,
        headers={"Idempotency-Key": "idem-key-001"},
    )
    assert r1.status_code == 201, r1.text

    r2 = await client.post(
        "/api/v1/auth/register",
        json=payload,
        headers={"Idempotency-Key": "idem-key-001"},
    )
    # Current behaviour: duplicate → 409 (no idempotency implemented)
    assert r2.status_code == 409, r2.text
    assert r2.json()["error"]["code"] == "USER_ALREADY_EXISTS"


# ===========================================================================
# 11. /me endpoint tests
# ===========================================================================

@pytest.mark.asyncio
async def test_me_requires_auth(client):
    """/me without a Bearer token → 401."""
    r = await client.get("/api/v1/auth/me")
    assert r.status_code == 401, r.text


@pytest.mark.asyncio
async def test_me_returns_current_user_state(client, db_session):
    """/me returns correct fields reflecting live DB state."""
    email = "me@endpoint.co"
    phone = "+2348011112004"
    password = "Secret1!"
    pin = "4321"

    tokens, auth = await _seed_logged_in_user(
        client, email=email, phone=phone, password=password, pin=pin
    )

    r = await client.get("/api/v1/auth/me", headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True
    assert body["error"] is None

    data = body["data"]
    assert data["email"] == email
    assert data["phone"] == phone
    assert data["email_verified"] is True
    # B9: _seed_logged_in_user takes the migration path — phone is
    # pre-stamped verified and PIN pre-stamped set so /email/verify
    # returns tokens. kyc_level deliberately stays tier_0 (no automatic
    # promotion in the helper); real promotion happens in verify_phone_otp.
    assert data["phone_verified"] is True
    assert data["pin_set"] is True
    assert data["kyc_level"] == 0
    assert data["full_name"] == "Test User"
    assert data["user_id"]  # non-empty UUID string
