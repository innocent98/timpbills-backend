import pytest
from httpx import AsyncClient, ASGITransport
from app.main import app
from app.api.deps import get_db, get_token_store, _fake_sms_singleton, reset_fake_sms
from app.core.limiter import limiter
from fakeredis.aioredis import FakeRedis
from app.services.token_store import RedisTokenStore


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

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_token_store] = _get_token_store
    reset_fake_sms()

    # Disable rate limiting during tests to avoid cross-test interference
    limiter.enabled = False
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    limiter.enabled = True
    await fake_redis.aclose()
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_register_then_verify_happy_path(client):
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

    # Read code from fake SMS singleton
    code = _fake_sms_singleton.sent[-1].code_or_message

    r2 = await client.post(
        "/api/v1/auth/verify-otp", json={"phone": "+2348022222222", "code": code}
    )
    assert r2.status_code == 200, r2.text
    body2 = r2.json()
    assert body2["data"]["pin_set"] is False
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
async def test_invalid_otp_returns_400(client):
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
        "/api/v1/auth/verify-otp", json={"phone": "+2348022222224", "code": "000000"}
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_OTP"


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

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_token_store] = _get_token_store
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
