"""API-level tests for /users/me/push-tokens — Sprint 4 B16.

POST upserts an FCM device token (new row / same-user bump / cross-user
reassign). DELETE is owner-scoped — a 404 response deliberately covers
both "not found" and "belongs to another user" so ownership does not
leak across the side channel."""
from datetime import datetime
from uuid import UUID

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
    reset_fake_paystack,
    reset_fake_sms,
    reset_fake_vtpass,
)
from app.core.limiter import limiter
from app.db.models.push_token import PushToken
from app.db.models.user import User
from app.integrations.email.fake import FakeEmailClient
from app.main import app
from app.services.token_store import RedisTokenStore

import tests.e2e.test_auth_full_flows as _e2e_mod
from tests.e2e.test_auth_full_flows import _seed_logged_in_user


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
    reset_fake_paystack()
    reset_fake_vtpass()
    _test_email_client.sent.clear()

    _orig = _e2e_mod._e2e_email_client
    _e2e_mod._e2e_email_client = _test_email_client

    limiter.enabled = False
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    limiter.enabled = True

    _e2e_mod._e2e_email_client = _orig
    await fake_redis.aclose()
    app.dependency_overrides.clear()


def _parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


# ── 1. New registration ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_creates_new_push_token(client):
    _, headers = await _seed_logged_in_user(
        client, email="pt1@test.co", phone="+2348020000001"
    )

    r = await client.post(
        "/api/v1/users/me/push-tokens",
        json={"fcm_token": "TOKEN_A", "platform": "ios"},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["platform"] == "ios"
    assert data["fcm_token"] == "TOKEN_A"
    UUID(data["id"])  # raises if not a valid UUID


# ── 2. Upsert on (user, token) is idempotent + bumps last_seen ──────────


@pytest.mark.asyncio
async def test_post_same_user_same_token_is_idempotent_bumps_last_seen(client):
    _, headers = await _seed_logged_in_user(
        client, email="pt2@test.co", phone="+2348020000002"
    )

    r1 = await client.post(
        "/api/v1/users/me/push-tokens",
        json={"fcm_token": "TOKEN_IDEM", "platform": "android"},
        headers=headers,
    )
    assert r1.status_code == 200, r1.text
    d1 = r1.json()["data"]

    r2 = await client.post(
        "/api/v1/users/me/push-tokens",
        json={"fcm_token": "TOKEN_IDEM", "platform": "android"},
        headers=headers,
    )
    assert r2.status_code == 200, r2.text
    d2 = r2.json()["data"]

    # Same row id: no duplicates.
    assert d1["id"] == d2["id"]
    # last_seen_at bumped (or at worst equal, if the two calls landed in
    # the same timestamp tick).
    assert _parse_iso(d2["last_seen_at"]) >= _parse_iso(d1["last_seen_at"])


# ── 3. Cross-user reassignment ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_cross_user_reassigns_ownership(client, db_session):
    _, headers_a = await _seed_logged_in_user(
        client, email="pt3a@test.co", phone="+2348020000003"
    )
    _, headers_b = await _seed_logged_in_user(
        client, email="pt3b@test.co", phone="+2348020000004"
    )

    r_a = await client.post(
        "/api/v1/users/me/push-tokens",
        json={"fcm_token": "TOKEN_CROSS", "platform": "ios"},
        headers=headers_a,
    )
    assert r_a.status_code == 200, r_a.text
    id_a = r_a.json()["data"]["id"]

    r_b = await client.post(
        "/api/v1/users/me/push-tokens",
        json={"fcm_token": "TOKEN_CROSS", "platform": "android"},
        headers=headers_b,
    )
    assert r_b.status_code == 200, r_b.text
    # Same DB row id — ownership moved, no duplicate row.
    assert r_b.json()["data"]["id"] == id_a

    db_session.expire_all()
    user_a = db_session.query(User).filter(User.email == "pt3a@test.co").one()
    user_b = db_session.query(User).filter(User.email == "pt3b@test.co").one()
    row = (
        db_session.query(PushToken)
        .filter(PushToken.fcm_token == "TOKEN_CROSS")
        .one()
    )
    assert row.user_id == user_b.id
    # User A's list should no longer include the token.
    a_tokens = (
        db_session.query(PushToken)
        .filter(PushToken.user_id == user_a.id)
        .all()
    )
    assert all(t.fcm_token != "TOKEN_CROSS" for t in a_tokens)


# ── 4. DELETE own token, second DELETE → 404 ────────────────────────────


@pytest.mark.asyncio
async def test_delete_removes_own_token(client):
    _, headers = await _seed_logged_in_user(
        client, email="pt4@test.co", phone="+2348020000005"
    )

    r_post = await client.post(
        "/api/v1/users/me/push-tokens",
        json={"fcm_token": "TOKEN_DEL", "platform": "ios"},
        headers=headers,
    )
    assert r_post.status_code == 200, r_post.text
    token_id = r_post.json()["data"]["id"]

    r_del = await client.delete(
        f"/api/v1/users/me/push-tokens/{token_id}",
        headers=headers,
    )
    assert r_del.status_code == 204, r_del.text

    r_del2 = await client.delete(
        f"/api/v1/users/me/push-tokens/{token_id}",
        headers=headers,
    )
    assert r_del2.status_code == 404
    err = r_del2.json()["error"]
    assert err["code"] == "PUSH_TOKEN_NOT_FOUND"


# ── 5. DELETE someone else's token → 404 without ownership leak ─────────


@pytest.mark.asyncio
async def test_delete_another_users_token_returns_404(client, db_session):
    _, headers_a = await _seed_logged_in_user(
        client, email="pt5a@test.co", phone="+2348020000006"
    )
    _, headers_b = await _seed_logged_in_user(
        client, email="pt5b@test.co", phone="+2348020000007"
    )

    r_a = await client.post(
        "/api/v1/users/me/push-tokens",
        json={"fcm_token": "TOKEN_OTHER", "platform": "android"},
        headers=headers_a,
    )
    assert r_a.status_code == 200, r_a.text
    id_a = r_a.json()["data"]["id"]

    r_del = await client.delete(
        f"/api/v1/users/me/push-tokens/{id_a}",
        headers=headers_b,
    )
    assert r_del.status_code == 404
    err = r_del.json()["error"]
    assert err["code"] == "PUSH_TOKEN_NOT_FOUND"
    # Ownership must not leak through the error message.
    assert "another" not in err["message"].lower()
    assert "owner" not in err["message"].lower()

    db_session.expire_all()
    row = (
        db_session.query(PushToken)
        .filter(PushToken.id == UUID(id_a))
        .one_or_none()
    )
    assert row is not None  # Row survives the failed delete attempt.


# ── 6. Invalid platform is rejected at the schema layer ─────────────────


@pytest.mark.asyncio
async def test_post_rejects_invalid_platform(client):
    _, headers = await _seed_logged_in_user(
        client, email="pt6@test.co", phone="+2348020000008"
    )

    r = await client.post(
        "/api/v1/users/me/push-tokens",
        json={"fcm_token": "TOKEN_BAD", "platform": "windows"},
        headers=headers,
    )
    assert r.status_code == 422, r.text
