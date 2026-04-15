import pytest
from httpx import AsyncClient, ASGITransport
from app.main import app
from app.api.deps import (
    get_db,
    get_token_store,
    get_email_provider,
    _fake_sms_singleton,
    reset_fake_sms,
    reset_fake_email,
)
from app.integrations.email.fake import FakeEmailClient
from app.core.limiter import limiter
from fakeredis.aioredis import FakeRedis
from app.services.token_store import RedisTokenStore

# Module-level fake email client so tests can inspect .sent
_test_email_client = FakeEmailClient()


@pytest.fixture
async def client(db_session):
    # Override get_db to use our test session
    def _get_db():
        try:
            yield db_session
        finally:
            pass

    # Override get_token_store to use fakeredis
    fake_redis = FakeRedis(decode_responses=True)

    def _get_token_store():
        return RedisTokenStore(redis=fake_redis)

    def _get_email():
        return _test_email_client

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_token_store] = _get_token_store
    app.dependency_overrides[get_email_provider] = _get_email
    reset_fake_sms()
    reset_fake_email()
    _test_email_client.sent.clear()

    # Disable rate limiting during tests to avoid cross-test interference
    limiter.enabled = False
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    limiter.enabled = True
    await fake_redis.aclose()
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_register_then_verify_email_happy_path(client):
    r1 = await client.post(
        "/api/v1/auth/register",
        json={
            "full_name": "E2E User",
            "phone": "+2348022222222",
            "email": "e@e.co",
            "password": "Secret1!",
        },
    )
    assert r1.status_code == 201, r1.text
    body = r1.json()
    assert body["success"] is True
    assert body["data"]["phone"] == "+2348022222222"
    assert body["data"]["email"] == "e@e.co"

    # Read code from fake email client
    code = _test_email_client.sent[-1].code_or_body

    r2 = await client.post(
        "/api/v1/auth/email/verify", json={"email": "e@e.co", "code": code}
    )
    assert r2.status_code == 200, r2.text
    body2 = r2.json()
    assert body2["data"]["pin_set"] is False
    assert body2["data"]["phone_verified"] is False
    assert body2["data"]["tokens"]["access_token"]


@pytest.mark.asyncio
async def test_register_duplicate_returns_409(client):
    payload = {
        "full_name": "Dup User",
        "phone": "+2348022222223",
        "email": "d@d.co",
        "password": "Secret1!",
    }
    r = await client.post("/api/v1/auth/register", json=payload)
    assert r.status_code == 201
    r2 = await client.post("/api/v1/auth/register", json={**payload, "email": "d2@d.co"})
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "USER_ALREADY_EXISTS"


@pytest.mark.asyncio
async def test_invalid_email_otp_returns_400(client):
    await client.post(
        "/api/v1/auth/register",
        json={
            "full_name": "Bad OTP",
            "phone": "+2348022222224",
            "email": "b@b.co",
            "password": "Secret1!",
        },
    )
    r = await client.post(
        "/api/v1/auth/email/verify", json={"email": "b@b.co", "code": "000000"}
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_OTP"


@pytest.mark.asyncio
async def test_email_resend_endpoint(client):
    await client.post(
        "/api/v1/auth/register",
        json={
            "full_name": "Resend User",
            "phone": "+2348022222225",
            "email": "resend@test.co",
            "password": "Secret1!",
        },
    )
    assert len(_test_email_client.sent) == 1

    r = await client.post("/api/v1/auth/email/resend", json={"email": "resend@test.co"})
    assert r.status_code == 200
    assert r.json()["data"]["ok"] is True
    assert len(_test_email_client.sent) == 2


@pytest.mark.asyncio
async def test_phone_upgrade_flow(client):
    """Full phone upgrade: register → email verify → send phone OTP → verify phone OTP."""
    # Register
    await client.post(
        "/api/v1/auth/register",
        json={
            "full_name": "Upgrade User",
            "phone": "+2348022222226",
            "email": "upgrade@test.co",
            "password": "Secret1!",
        },
    )
    email_code = _test_email_client.sent[-1].code_or_body

    # Verify email → get tokens
    r_ev = await client.post(
        "/api/v1/auth/email/verify", json={"email": "upgrade@test.co", "code": email_code}
    )
    assert r_ev.status_code == 200, r_ev.text
    access_token = r_ev.json()["data"]["tokens"]["access_token"]

    headers = {"Authorization": f"Bearer {access_token}"}

    # Send phone OTP (authenticated)
    r_send = await client.post("/api/v1/auth/phone/send-otp", headers=headers)
    assert r_send.status_code == 200, r_send.text

    # Read code from fake SMS
    phone_code = _fake_sms_singleton.sent[-1].code_or_message

    # Verify phone OTP (authenticated)
    r_pv = await client.post(
        "/api/v1/auth/phone/verify-otp",
        json={"code": phone_code},
        headers=headers,
    )
    assert r_pv.status_code == 200, r_pv.text
    body_pv = r_pv.json()
    assert body_pv["data"]["tokens"]["access_token"]


@pytest.fixture
async def rate_limited_client(db_session):
    """Client with rate limiting enabled for rate limit tests."""
    def _get_db():
        try:
            yield db_session
        finally:
            pass

    fake_redis_rl = FakeRedis(decode_responses=True)

    def _get_token_store():
        return RedisTokenStore(redis=fake_redis_rl)

    def _get_email():
        return _test_email_client

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_token_store] = _get_token_store
    app.dependency_overrides[get_email_provider] = _get_email
    reset_fake_sms()
    # Ensure rate limiting is enabled and reset any stored state
    limiter.enabled = True
    limiter.reset()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    limiter.enabled = False
    await fake_redis_rl.aclose()
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_login_rate_limited(rate_limited_client):
    """6th login attempt with bad creds should return 429."""
    payload = {"identifier": "nobody@example.com", "password": "WrongPass1!"}
    last_response = None
    for _ in range(6):
        last_response = await rate_limited_client.post("/api/v1/auth/login", json=payload)
    assert last_response.status_code == 429
