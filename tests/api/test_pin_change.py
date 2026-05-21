"""API tests for POST /auth/pin/change (Sprint 5c · Task 4.3).

Covers:
  * happy path: 200 + new PIN verifies, old PIN no longer works
  * old_pin wrong: 401 INVALID_PIN
  * malformed PIN (wrong length, non-digit): 422
  * PIN not yet set: 400 PIN_NOT_SET
  * auth required: 401
  * sessions NOT revoked (unlike password change — PIN is step-up only)
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from fakeredis.aioredis import FakeRedis
from httpx import ASGITransport, AsyncClient

from app.api.deps import (
    get_db,
    get_email_provider,
    get_redis,
    get_token_store,
    reset_fake_email,
    reset_fake_sms,
)
from app.core.limiter import limiter
from app.integrations.email.fake import FakeEmailClient
from app.main import app
from app.services.token_store import RedisTokenStore

_test_email_client = FakeEmailClient()


@pytest_asyncio.fixture
async def client(db_session):
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
        return _test_email_client

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_token_store] = _get_token_store
    app.dependency_overrides[get_redis] = _get_redis
    app.dependency_overrides[get_email_provider] = _get_email
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


async def _seed_user_with_pin(client: AsyncClient, *, email: str, phone: str, pin: str = "1234") -> dict:
    """Register → verify email → set PIN → return auth headers."""
    r = await client.post(
        "/api/v1/auth/register",
        json={
            "full_name": "Pin Change User",
            "phone": phone,
            "email": email,
            "password": "Secret1!",
        },
    )
    assert r.status_code == 201, r.text

    code = _test_email_client.sent[-1].code_or_body
    r2 = await client.post(
        "/api/v1/auth/email/verify",
        json={"email": email, "code": code},
    )
    assert r2.status_code == 200, r2.text
    tokens = r2.json()["data"]["tokens"]
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    r3 = await client.post("/api/v1/auth/pin/set", json={"pin": pin}, headers=headers)
    assert r3.status_code == 200, r3.text
    return headers


async def _seed_user_no_pin(client: AsyncClient, *, email: str, phone: str) -> dict:
    """Register + email-verify, but DO NOT set a PIN."""
    r = await client.post(
        "/api/v1/auth/register",
        json={
            "full_name": "No Pin User",
            "phone": phone,
            "email": email,
            "password": "Secret1!",
        },
    )
    assert r.status_code == 201, r.text
    code = _test_email_client.sent[-1].code_or_body
    r2 = await client.post(
        "/api/v1/auth/email/verify",
        json={"email": email, "code": code},
    )
    assert r2.status_code == 200, r2.text
    tokens = r2.json()["data"]["tokens"]
    return {"Authorization": f"Bearer {tokens['access_token']}"}


# ---- happy path -----------------------------------------------------------

@pytest.mark.asyncio
async def test_change_pin_happy_path(client):
    headers = await _seed_user_with_pin(client, email="pinchg1@test.co", phone="+2348011110001", pin="1234")
    r = await client.post(
        "/api/v1/auth/pin/change",
        json={"old_pin": "1234", "new_pin": "9876"},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["ok"] is True

    # New PIN verifies
    v_new = await client.post("/api/v1/auth/pin/verify", json={"pin": "9876"}, headers=headers)
    assert v_new.status_code == 200, v_new.text

    # Old PIN no longer works
    v_old = await client.post("/api/v1/auth/pin/verify", json={"pin": "1234"}, headers=headers)
    assert v_old.status_code == 401, v_old.text
    assert v_old.json()["error"]["code"] == "INVALID_PIN"


# ---- failure modes --------------------------------------------------------

@pytest.mark.asyncio
async def test_change_pin_wrong_old(client):
    headers = await _seed_user_with_pin(client, email="pinchg2@test.co", phone="+2348011110002", pin="1234")
    r = await client.post(
        "/api/v1/auth/pin/change",
        json={"old_pin": "0000", "new_pin": "5678"},
        headers=headers,
    )
    assert r.status_code == 401, r.text
    assert r.json()["error"]["code"] == "INVALID_PIN"


@pytest.mark.asyncio
async def test_change_pin_requires_pin_already_set(client):
    headers = await _seed_user_no_pin(client, email="pinchg3@test.co", phone="+2348011110003")
    r = await client.post(
        "/api/v1/auth/pin/change",
        json={"old_pin": "1234", "new_pin": "5678"},
        headers=headers,
    )
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "PIN_NOT_SET"


@pytest.mark.asyncio
async def test_change_pin_rejects_non_digit(client):
    headers = await _seed_user_with_pin(client, email="pinchg4@test.co", phone="+2348011110004", pin="1234")
    r = await client.post(
        "/api/v1/auth/pin/change",
        json={"old_pin": "1234", "new_pin": "abcd"},
        headers=headers,
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_change_pin_rejects_wrong_length(client):
    headers = await _seed_user_with_pin(client, email="pinchg5@test.co", phone="+2348011110005", pin="1234")
    r = await client.post(
        "/api/v1/auth/pin/change",
        json={"old_pin": "1234", "new_pin": "12345"},
        headers=headers,
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_change_pin_requires_auth(client):
    r = await client.post(
        "/api/v1/auth/pin/change",
        json={"old_pin": "1234", "new_pin": "5678"},
    )
    assert r.status_code == 401


# ---- sessions stay valid --------------------------------------------------

@pytest.mark.asyncio
async def test_change_pin_does_not_revoke_session(client):
    """Unlike /password/change, PIN rotation should NOT invalidate the
    access token. PIN is a step-up factor, not the session credential."""
    headers = await _seed_user_with_pin(client, email="pinchg6@test.co", phone="+2348011110006", pin="1234")
    # Rotate PIN
    r = await client.post(
        "/api/v1/auth/pin/change",
        json={"old_pin": "1234", "new_pin": "9876"},
        headers=headers,
    )
    assert r.status_code == 200, r.text

    # The same access token still works on a protected endpoint
    me = await client.get("/api/v1/auth/me", headers=headers)
    assert me.status_code == 200, me.text
