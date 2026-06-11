"""API tests for /api/v1/auth/refresh (Sprint 5c · Task 4.1).

Covers the spec's required behaviour:
  1. Successful refresh returns a NEW access + refresh pair.
  2. Refresh tokens rotate — using the same refresh twice fails the
     second time (replay detection nukes all sessions).
  3. Refresh consults the JWT-jti blocklist — if the refresh token's
     jti has been blocklisted via TokenRevocationService, /refresh
     returns 401 even though the RedisTokenStore entry is still live.
     This is the Task 4.1 defensive fix.
  4. Expired refresh tokens are rejected (401).
  5. Sending an access token in place of a refresh token is rejected
     (401) because the ``typ`` claim does not match.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from fakeredis.aioredis import FakeRedis
from httpx import ASGITransport, AsyncClient
from jose import jwt

from app.api.deps import (
    get_db,
    get_email_provider,
    get_redis,
    get_token_store,
    reset_fake_email,
    reset_fake_sms,
)
from app.core.config import settings
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
        # Expose the shared fake_redis so tests can poke the blocklist.
        c._fake_redis = fake_redis  # type: ignore[attr-defined]
        yield c
    limiter.enabled = True
    await fake_redis.aclose()
    app.dependency_overrides.clear()


async def _seed_user(
    client: AsyncClient, *, email: str, phone: str
) -> dict:
    """Register + verify email; return the tokens dict."""
    r = await client.post(
        "/api/v1/auth/register",
        json={
            "full_name": "Refresh User",
            "phone": phone,
            "email": email,
            "password": "Secret1!",
        },
    )
    assert r.status_code == 201, r.text

    from tests._b9_seed import stamp_for_email_verify_tokens
    stamp_for_email_verify_tokens(email=email)

    code = _test_email_client.sent[-1].code_or_body
    r2 = await client.post(
        "/api/v1/auth/email/verify", json={"email": email, "code": code}
    )
    assert r2.status_code == 200, r2.text
    return r2.json()["data"]["tokens"]


# ---------------------------------------------------------------------------
# 1. Successful refresh issues a NEW access token
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_refresh_returns_new_access_token(client):
    tokens = await _seed_user(
        client, email="rf1@test.co", phone="+2348077777701"
    )
    # Sleep 1s so jti uniqueness aside, even the iat differs and the
    # encoded tokens cannot accidentally collide (jose includes iat).
    time.sleep(1)
    r = await client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": tokens["refresh_token"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert body["access_token"]
    assert body["access_token"] != tokens["access_token"]


# ---------------------------------------------------------------------------
# 2. Refresh tokens rotate — same refresh used twice → 401
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_refresh_rotates_refresh_token(client):
    tokens = await _seed_user(
        client, email="rf2@test.co", phone="+2348077777702"
    )
    time.sleep(1)
    r = await client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": tokens["refresh_token"]},
    )
    assert r.status_code == 200
    new_refresh = r.json()["data"]["refresh_token"]
    assert new_refresh != tokens["refresh_token"]

    # Second use of the original refresh token must fail (rotated out).
    bad = await client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": tokens["refresh_token"]},
    )
    assert bad.status_code == 401, bad.text


# ---------------------------------------------------------------------------
# 3. Refresh respects the JWT-jti blocklist
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_refresh_with_jwt_blocklisted_token_fails(client):
    """If the refresh token's jti is added to the JWT-jti blocklist
    (revoked:jwt:{jti}) — independent of the rotation keyspace — the
    refresh endpoint must reject it. This protects against any future
    code path that revokes refresh tokens via the blocklist."""
    tokens = await _seed_user(
        client, email="rf3@test.co", phone="+2348077777703"
    )
    payload = jwt.decode(
        tokens["refresh_token"], settings.SECRET_KEY, algorithms=["HS256"]
    )
    jti = payload["jti"]
    fake_redis = client._fake_redis  # type: ignore[attr-defined]
    # Manually blocklist the refresh jti (mirrors what a future
    # password-change-revokes-refresh flow would do).
    await fake_redis.set(f"revoked:jwt:{jti}", "1", ex=600)

    r = await client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": tokens["refresh_token"]},
    )
    assert r.status_code == 401, r.text


# ---------------------------------------------------------------------------
# 4. Expired refresh tokens are rejected
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_refresh_with_expired_token_fails(client):
    """Hand-craft an expired refresh token and verify /refresh rejects it.

    We don't go through register/verify because real tokens have a 30d
    TTL — too long to wait. Instead we mint a payload with sub matching
    a real user but exp in the past.
    """
    tokens = await _seed_user(
        client, email="rf4@test.co", phone="+2348077777704"
    )
    real = jwt.decode(
        tokens["refresh_token"], settings.SECRET_KEY, algorithms=["HS256"]
    )
    expired = jwt.encode(
        {
            "sub": real["sub"],
            "jti": "expired-jti",
            "typ": "refresh",
            "iat": datetime.now(UTC) - timedelta(minutes=5),
            "exp": datetime.now(UTC) - timedelta(minutes=1),
        },
        settings.SECRET_KEY,
        algorithm="HS256",
    )

    r = await client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": expired},
    )
    assert r.status_code == 401, r.text


# ---------------------------------------------------------------------------
# 5. Access token cannot be used as a refresh token
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_refresh_with_access_token_in_body_is_rejected(client):
    tokens = await _seed_user(
        client, email="rf5@test.co", phone="+2348077777705"
    )
    r = await client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": tokens["access_token"]},
    )
    assert r.status_code == 401, r.text


# ---------------------------------------------------------------------------
# 6. Garbage in → 401
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_refresh_with_garbage_token_fails(client):
    r = await client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": "not.a.jwt"},
    )
    assert r.status_code == 401, r.text
