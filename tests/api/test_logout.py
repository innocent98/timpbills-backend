"""API tests for /api/v1/auth/logout + JWT-jti blocklist (Sprint 5c · Task 3.3).

The blocklist is asserted both ways:
  * after logout, a protected route called with the same bearer
    returns 401 TOKEN_REVOKED;
  * a second logout call with the same bearer also returns 401
    because the first logout already revoked the token — that is
    the contract: logout is idempotent in *effect* (the token is
    dead either way) but only the first call traverses to 204.
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


async def _seed_user(
    client: AsyncClient, *, email: str, phone: str
) -> tuple[dict, dict]:
    """Register + verify email; return (auth_headers, tokens_dict)."""
    r = await client.post(
        "/api/v1/auth/register",
        json={
            "full_name": "Logout User",
            "phone": phone,
            "email": email,
            "password": "Secret1!",
        },
    )
    assert r.status_code == 201, r.text

    code = _test_email_client.sent[-1].code_or_body
    r2 = await client.post(
        "/api/v1/auth/email/verify", json={"email": email, "code": code}
    )
    assert r2.status_code == 200, r2.text
    tokens = r2.json()["data"]["tokens"]
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    return headers, tokens


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_logout_returns_204(client):
    headers, _ = await _seed_user(
        client, email="lo1@test.co", phone="+2348066666601"
    )
    r = await client.post("/api/v1/auth/logout", headers=headers)
    assert r.status_code == 204, r.text


@pytest.mark.asyncio
async def test_logout_blocks_subsequent_requests(client):
    """After logout, the same access token returns 401 TOKEN_REVOKED."""
    headers, _ = await _seed_user(
        client, email="lo2@test.co", phone="+2348066666602"
    )

    # Sanity: the token works before logout
    pre = await client.get("/api/v1/auth/me", headers=headers)
    assert pre.status_code == 200

    # Logout
    r = await client.post("/api/v1/auth/logout", headers=headers)
    assert r.status_code == 204

    # Same bearer no longer authenticates
    post = await client.get("/api/v1/auth/me", headers=headers)
    assert post.status_code == 401, post.text
    assert post.json()["error"]["code"] == "TOKEN_REVOKED"


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_logout_requires_auth(client):
    r = await client.post("/api/v1/auth/logout")
    assert r.status_code == 401, r.text


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_logout_idempotent_second_call_is_revoked(client):
    """Second logout with the same bearer returns 401 (already revoked),
    confirming the blocklist persists. The first-call side-effect is
    idempotent — the same Redis key just rewrites with the same TTL."""
    headers, _ = await _seed_user(
        client, email="lo3@test.co", phone="+2348066666603"
    )
    r1 = await client.post("/api/v1/auth/logout", headers=headers)
    assert r1.status_code == 204

    r2 = await client.post("/api/v1/auth/logout", headers=headers)
    assert r2.status_code == 401
    assert r2.json()["error"]["code"] == "TOKEN_REVOKED"


# ---------------------------------------------------------------------------
# Refresh-token revocation via body
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_logout_with_refresh_token_kills_refresh_session(client):
    """Logout with refresh_token in body must invalidate that refresh
    token — a subsequent /auth/refresh with it returns 401."""
    headers, tokens = await _seed_user(
        client, email="lo4@test.co", phone="+2348066666604"
    )

    r = await client.post(
        "/api/v1/auth/logout",
        headers=headers,
        json={"refresh_token": tokens["refresh_token"]},
    )
    assert r.status_code == 204

    # The refresh token is gone from the rotation keyspace; /refresh
    # treats it as replay and 401s.
    rf = await client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": tokens["refresh_token"]},
    )
    assert rf.status_code == 401, rf.text


@pytest.mark.asyncio
async def test_logout_with_malformed_refresh_token_still_204(client):
    """A junk refresh_token must not 500 the logout — bad bodies are
    silently ignored so logout remains a best-effort cleanup."""
    headers, _ = await _seed_user(
        client, email="lo5@test.co", phone="+2348066666605"
    )
    r = await client.post(
        "/api/v1/auth/logout",
        headers=headers,
        json={"refresh_token": "this.is.notajwt"},
    )
    assert r.status_code == 204
