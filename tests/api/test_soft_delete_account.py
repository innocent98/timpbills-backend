"""API tests for DELETE /api/v1/users/me — soft-delete account
(Sprint 5c · Task 6.1).

Covers:
  * happy path: 204 + ``is_active`` flips to False and ``deleted_at`` is set
  * the same bearer no longer authenticates afterwards (ACCOUNT_DISABLED)
  * login with the same credentials fails (ACCOUNT_DISABLED)
  * re-registering with the same phone within 30 days → 409 PHONE_RECENTLY_DELETED
  * re-registering with the same email within 30 days → 409 EMAIL_RECENTLY_DELETED
  * the refresh token is revoked along with the access token
  * the endpoint requires auth (401 when no bearer)

Mirrors the fixture shape of ``test_password_change`` / ``test_phone_change_flow``
so async client + fakeredis + dependency-override plumbing stays consistent.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

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
) -> tuple[dict, dict, str]:
    """Register + verify; return (headers, tokens, user_id)."""
    r = await client.post(
        "/api/v1/auth/register",
        json={
            "full_name": "Soft Delete",
            "phone": phone,
            "email": email,
            "password": "Secret1!",
        },
    )
    assert r.status_code == 201, r.text
    user_id = r.json()["data"]["user_id"]

    from tests._b9_seed import stamp_for_email_verify_tokens
    stamp_for_email_verify_tokens(email=email)

    code = _test_email_client.sent[-1].code_or_body
    r2 = await client.post(
        "/api/v1/auth/email/verify", json={"email": email, "code": code}
    )
    assert r2.status_code == 200, r2.text
    tokens = r2.json()["data"]["tokens"]
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    return headers, tokens, user_id


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_delete_me_returns_204(client):
    headers, _, _ = await _seed_user(
        client, email="sd1@test.co", phone="+2348077777701"
    )
    r = await client.delete("/api/v1/users/me", headers=headers)
    assert r.status_code == 204, r.text


@pytest.mark.asyncio
async def test_delete_me_sets_is_active_false_and_deleted_at(client, db_session):
    from app.db.models.user import User

    headers, _, _ = await _seed_user(
        client, email="sd2@test.co", phone="+2348077777702"
    )
    r = await client.delete("/api/v1/users/me", headers=headers)
    assert r.status_code == 204

    db_session.expire_all()
    user = db_session.query(User).filter(User.email == "sd2@test.co").first()
    assert user is not None
    assert user.is_active is False
    assert user.deleted_at is not None


# ---------------------------------------------------------------------------
# Side-effects on the current session
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_authenticated_endpoints_fail_after_soft_delete(client):
    """The same access token used for the delete call must not work
    afterwards — auth gate rejects when the user is inactive."""
    headers, _, _ = await _seed_user(
        client, email="sd3@test.co", phone="+2348077777703"
    )
    # Sanity: token works pre-delete
    pre = await client.get("/api/v1/auth/me", headers=headers)
    assert pre.status_code == 200

    r = await client.delete("/api/v1/users/me", headers=headers)
    assert r.status_code == 204

    # Same bearer is now rejected
    post = await client.get("/api/v1/auth/me", headers=headers)
    assert post.status_code == 401, post.text
    # tokens_revoked_at stamp fires first (predates the iat clock-second),
    # then ACCOUNT_DISABLED — either is acceptable. The spec just requires
    # 401 with a code that signals the account-disabled / token-revoked state.
    assert post.json()["error"]["code"] in {"ACCOUNT_DISABLED", "TOKEN_REVOKED"}


@pytest.mark.asyncio
async def test_login_fails_after_soft_delete(client):
    """The user can't log back in with the same credentials — the
    login service surfaces ACCOUNT_DISABLED (which is already the
    code AuthService.login emits for ``is_active is False``)."""
    headers, _, _ = await _seed_user(
        client, email="sd4@test.co", phone="+2348077777704"
    )
    r = await client.delete("/api/v1/users/me", headers=headers)
    assert r.status_code == 204

    bad = await client.post(
        "/api/v1/auth/login",
        json={"phone": "+2348077777704", "password": "Secret1!"},
    )
    assert bad.status_code == 403, bad.text
    assert bad.json()["error"]["code"] == "ACCOUNT_DISABLED"


@pytest.mark.asyncio
async def test_soft_delete_revokes_refresh_token(client):
    """The refresh token from before the delete can no longer be
    exchanged — rotation keyspace is nuked alongside the soft-delete."""
    headers, tokens, _ = await _seed_user(
        client, email="sd5@test.co", phone="+2348077777705"
    )
    r = await client.delete("/api/v1/users/me", headers=headers)
    assert r.status_code == 204

    rf = await client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": tokens["refresh_token"]},
    )
    assert rf.status_code == 401, rf.text


# ---------------------------------------------------------------------------
# 30-day re-register block
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_re_register_blocked_within_30_days_same_phone(client):
    """Re-registering with the *phone* of a recently-deleted account
    must be refused with a distinct error code so mobile can surface
    a clear message ('Try again after 30 days')."""
    headers, _, _ = await _seed_user(
        client, email="sd6@test.co", phone="+2348077777706"
    )
    r = await client.delete("/api/v1/users/me", headers=headers)
    assert r.status_code == 204

    # Different email, same phone — must be blocked.
    rr = await client.post(
        "/api/v1/auth/register",
        json={
            "full_name": "Reregister",
            "phone": "+2348077777706",
            "email": "different@test.co",
            "password": "Secret1!",
        },
    )
    assert rr.status_code == 409, rr.text
    assert rr.json()["error"]["code"] == "PHONE_RECENTLY_DELETED"


@pytest.mark.asyncio
async def test_re_register_blocked_within_30_days_same_email(client):
    headers, _, _ = await _seed_user(
        client, email="sd7@test.co", phone="+2348077777707"
    )
    r = await client.delete("/api/v1/users/me", headers=headers)
    assert r.status_code == 204

    # Different phone, same email — must be blocked.
    rr = await client.post(
        "/api/v1/auth/register",
        json={
            "full_name": "Reregister",
            "phone": "+2348077777799",
            "email": "sd7@test.co",
            "password": "Secret1!",
        },
    )
    assert rr.status_code == 409, rr.text
    assert rr.json()["error"]["code"] == "EMAIL_RECENTLY_DELETED"


@pytest.mark.asyncio
async def test_re_register_allowed_after_30_days(client, db_session):
    """After 30 days the recently-deleted block expires. We simulate
    the passage of time by back-dating ``deleted_at`` directly in the
    DB (the production code never rewrites this column).

    The block is the *only* protection — once it expires, the
    duplicate phone/email guard catches it as a plain
    USER_ALREADY_EXISTS. We assert that error code rather than 201
    because re-using the *same* identifier on a still-existing row is
    a different problem (handled by Sprint 8's hard-delete pass)."""
    from app.db.models.user import User

    headers, _, _ = await _seed_user(
        client, email="sd8@test.co", phone="+2348077777708"
    )
    r = await client.delete("/api/v1/users/me", headers=headers)
    assert r.status_code == 204

    # Back-date deleted_at to 31 days ago.
    db_session.expire_all()
    user = db_session.query(User).filter(User.email == "sd8@test.co").first()
    assert user is not None
    user.deleted_at = datetime.now(UTC) - timedelta(days=31)
    db_session.add(user)
    db_session.commit()

    rr = await client.post(
        "/api/v1/auth/register",
        json={
            "full_name": "Reregister",
            "phone": "+2348077777708",
            "email": "sd8@test.co",
            "password": "Secret1!",
        },
    )
    # > 30 days: the recently-deleted block lifts, but the row still
    # exists with the same email/phone → USER_ALREADY_EXISTS. Sprint 8
    # hard-delete will purge the row entirely.
    assert rr.status_code == 409, rr.text
    assert rr.json()["error"]["code"] == "USER_ALREADY_EXISTS"


# ---------------------------------------------------------------------------
# Auth required
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_soft_delete_requires_auth(client):
    r = await client.delete("/api/v1/users/me")
    assert r.status_code == 401, r.text
