"""API tests for /api/v1/auth/password/change (Sprint 5c · Task 4.2).

Covers:
  * happy path: 204 + new password verified via login
  * old_password wrong: 400 INVALID_CREDENTIALS
  * weak new password (no uppercase / no digit / too short): 422
  * auth required: 401
  * existing access token rejected after change (TOKEN_REVOKED via
    tokens_revoked_at backstop)
  * existing refresh token rejected after change (rotation keyspace
    nuked via RedisTokenStore.revoke_all)
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
    """Register + verify; return (headers, tokens)."""
    r = await client.post(
        "/api/v1/auth/register",
        json={
            "full_name": "Password Change",
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
async def test_change_password_returns_204(client):
    headers, _ = await _seed_user(
        client, email="pc1@test.co", phone="+2348088888801"
    )
    r = await client.post(
        "/api/v1/auth/password/change",
        headers=headers,
        json={"old_password": "Secret1!", "new_password": "Newpass2!"},
    )
    assert r.status_code == 204, r.text


@pytest.mark.asyncio
async def test_change_password_actually_updates_password(client):
    """After change, the OLD password is rejected on /auth/login and
    the NEW password succeeds — the only way to prove the hash was
    actually rewritten in the DB."""
    headers, _ = await _seed_user(
        client, email="pc2@test.co", phone="+2348088888802"
    )

    r = await client.post(
        "/api/v1/auth/password/change",
        headers=headers,
        json={"old_password": "Secret1!", "new_password": "Newpass2!"},
    )
    assert r.status_code == 204

    # Old password fails
    bad = await client.post(
        "/api/v1/auth/login",
        json={"identifier": "pc2@test.co", "password": "Secret1!"},
    )
    assert bad.status_code == 401, bad.text

    # New password works
    good = await client.post(
        "/api/v1/auth/login",
        json={"identifier": "pc2@test.co", "password": "Newpass2!"},
    )
    assert good.status_code == 200, good.text


# ---------------------------------------------------------------------------
# 400 — wrong current password
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_change_password_wrong_old_returns_400(client):
    headers, _ = await _seed_user(
        client, email="pc3@test.co", phone="+2348088888803"
    )
    r = await client.post(
        "/api/v1/auth/password/change",
        headers=headers,
        json={"old_password": "WrongPass1!", "new_password": "Newpass2!"},
    )
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "INVALID_CREDENTIALS"


# ---------------------------------------------------------------------------
# 422 — weak new password
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_change_password_weak_new_short_returns_422(client):
    headers, _ = await _seed_user(
        client, email="pc4@test.co", phone="+2348088888804"
    )
    r = await client.post(
        "/api/v1/auth/password/change",
        headers=headers,
        json={"old_password": "Secret1!", "new_password": "short"},
    )
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_change_password_weak_new_no_digit_returns_422(client):
    headers, _ = await _seed_user(
        client, email="pc5@test.co", phone="+2348088888805"
    )
    r = await client.post(
        "/api/v1/auth/password/change",
        headers=headers,
        json={"old_password": "Secret1!", "new_password": "Newpassword!"},
    )
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_change_password_weak_new_no_uppercase_returns_422(client):
    headers, _ = await _seed_user(
        client, email="pc6@test.co", phone="+2348088888806"
    )
    r = await client.post(
        "/api/v1/auth/password/change",
        headers=headers,
        json={"old_password": "Secret1!", "new_password": "newpass1!"},
    )
    assert r.status_code == 422, r.text


# ---------------------------------------------------------------------------
# 401 — auth required
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_change_password_requires_auth(client):
    r = await client.post(
        "/api/v1/auth/password/change",
        json={"old_password": "Secret1!", "new_password": "Newpass2!"},
    )
    assert r.status_code == 401, r.text


# ---------------------------------------------------------------------------
# Side-effects: existing tokens revoked
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_change_password_revokes_existing_access_token(client):
    """After change, the SAME access token used for the change call
    should no longer authenticate — the tokens_revoked_at stamp on the
    user row marks every pre-change token as expired."""
    headers, _ = await _seed_user(
        client, email="pc7@test.co", phone="+2348088888807"
    )
    # Sanity: token works pre-change
    pre = await client.get("/api/v1/auth/me", headers=headers)
    assert pre.status_code == 200

    r = await client.post(
        "/api/v1/auth/password/change",
        headers=headers,
        json={"old_password": "Secret1!", "new_password": "Newpass2!"},
    )
    assert r.status_code == 204

    # Same bearer is now revoked
    post = await client.get("/api/v1/auth/me", headers=headers)
    assert post.status_code == 401, post.text
    assert post.json()["error"]["code"] == "TOKEN_REVOKED"


@pytest.mark.asyncio
async def test_change_password_revokes_existing_refresh_token(client):
    """After change, the refresh token from before the change can no
    longer be exchanged for a new pair."""
    headers, tokens = await _seed_user(
        client, email="pc8@test.co", phone="+2348088888808"
    )

    r = await client.post(
        "/api/v1/auth/password/change",
        headers=headers,
        json={"old_password": "Secret1!", "new_password": "Newpass2!"},
    )
    assert r.status_code == 204

    rf = await client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": tokens["refresh_token"]},
    )
    assert rf.status_code == 401, rf.text
