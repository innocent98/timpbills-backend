"""API tests for POST /auth/pin/verify."""
import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.api.deps import (
    get_db,
    get_redis,
    get_token_store,
    get_email_provider,
    reset_fake_sms,
    reset_fake_email,
)
from app.integrations.email.fake import FakeEmailClient
from app.core.limiter import limiter
from fakeredis.aioredis import FakeRedis
from app.services.token_store import RedisTokenStore

_test_email_client = FakeEmailClient()


@pytest_asyncio.fixture
async def client(db_session):
    """Async client with DB, Redis, email all faked out."""

    def _get_db():
        try:
            yield db_session
        finally:
            pass

    fake_redis = FakeRedis(decode_responses=True)

    def _get_token_store():
        return RedisTokenStore(redis=fake_redis)

    def _get_email():
        return _test_email_client

    async def _get_redis():
        return fake_redis

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_token_store] = _get_token_store
    app.dependency_overrides[get_email_provider] = _get_email
    app.dependency_overrides[get_redis] = _get_redis
    reset_fake_sms()
    reset_fake_email()
    _test_email_client.sent.clear()

    limiter.enabled = False
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    limiter.enabled = True
    await fake_redis.aclose()
    app.dependency_overrides.clear()


async def _seed_user(client: AsyncClient) -> dict:
    """Register → verify email → set PIN → return auth headers."""
    r = await client.post(
        "/api/v1/auth/register",
        json={
            "full_name": "Pin Verify User",
            "phone": "+2348033333333",
            "email": "pinverify@test.co",
            "password": "Secret1!",
        },
    )
    assert r.status_code == 201, r.text

    code = _test_email_client.sent[-1].code_or_body
    r2 = await client.post(
        "/api/v1/auth/email/verify",
        json={"email": "pinverify@test.co", "code": code},
    )
    assert r2.status_code == 200, r2.text
    tokens = r2.json()["data"]["tokens"]

    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    r3 = await client.post(
        "/api/v1/auth/pin/set", json={"pin": "8527"}, headers=headers
    )
    assert r3.status_code == 200, r3.text

    return headers


@pytest.mark.asyncio
async def test_pin_verify_returns_token(client):
    """Correct PIN → 200 with pin_token and expires_in=300."""
    headers = await _seed_user(client)
    r = await client.post(
        "/api/v1/auth/pin/verify", json={"pin": "8527"}, headers=headers
    )
    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert "pin_token" in body
    assert body["expires_in"] == 300


@pytest.mark.asyncio
async def test_wrong_pin_returns_401(client):
    """Wrong PIN → 401 with INVALID_PIN error code."""
    headers = await _seed_user(client)
    r = await client.post(
        "/api/v1/auth/pin/verify", json={"pin": "0000"}, headers=headers
    )
    assert r.status_code == 401, r.text
    assert r.json()["error"]["code"] == "INVALID_PIN"
