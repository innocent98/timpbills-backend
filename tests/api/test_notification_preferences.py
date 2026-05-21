"""API tests for /users/me/notification-preferences (Sprint 5c · Task 2.3).

Covers:
* GET creates a default-valued row on first read (lazy provisioning).
* GET on a second read returns the in-DB row, not a fresh default —
  proves a PATCH between the two reads is observable.
* PATCH applies a partial mutation and returns the merged state.
* PATCH rejects unknown fields (422) — extra="forbid".
* Auth required (401 without a Bearer token).
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from fakeredis.aioredis import FakeRedis

from app.main import app
from app.api.deps import (
    get_db,
    get_redis,
    get_token_store,
    get_email_provider,
    reset_fake_sms,
    reset_fake_email,
)
from app.core.limiter import limiter
from app.integrations.email.fake import FakeEmailClient
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


async def _seed_user(client: AsyncClient, *, email: str, phone: str) -> dict:
    r = await client.post(
        "/api/v1/auth/register",
        json={
            "full_name": "Notif Prefs User",
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


# ---------------------------------------------------------------------------
# GET defaults
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_creates_default_row_on_first_read(client):
    headers = await _seed_user(client, email="np1@test.co", phone="+2348055555501")

    r = await client.get(
        "/api/v1/users/me/notification-preferences", headers=headers
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    # Spec §3.2 defaults: 3 opt-ins true, promotions opt-out false.
    assert data == {
        "transaction_alerts": True,
        "referral_updates": True,
        "promotions": False,
        "email_notifications": True,
    }


@pytest.mark.asyncio
async def test_get_after_patch_reflects_mutation(client):
    headers = await _seed_user(client, email="np2@test.co", phone="+2348055555502")

    # First GET lazily creates the row
    r0 = await client.get(
        "/api/v1/users/me/notification-preferences", headers=headers
    )
    assert r0.status_code == 200

    # PATCH turn off promotions and email_notifications
    rp = await client.patch(
        "/api/v1/users/me/notification-preferences",
        headers=headers,
        json={"promotions": True, "email_notifications": False},
    )
    assert rp.status_code == 200, rp.text

    # Second GET shows the mutation, not a fresh default
    r1 = await client.get(
        "/api/v1/users/me/notification-preferences", headers=headers
    )
    assert r1.status_code == 200
    data = r1.json()["data"]
    assert data["promotions"] is True
    assert data["email_notifications"] is False
    # untouched fields retain defaults
    assert data["transaction_alerts"] is True
    assert data["referral_updates"] is True


# ---------------------------------------------------------------------------
# PATCH partial
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_patch_partial_update(client):
    headers = await _seed_user(client, email="np3@test.co", phone="+2348055555503")

    r = await client.patch(
        "/api/v1/users/me/notification-preferences",
        headers=headers,
        json={"transaction_alerts": False},
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["transaction_alerts"] is False
    # Untouched defaults preserved
    assert data["referral_updates"] is True
    assert data["promotions"] is False
    assert data["email_notifications"] is True


# ---------------------------------------------------------------------------
# PATCH extra field rejected
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_patch_rejects_unknown_field(client):
    headers = await _seed_user(client, email="np4@test.co", phone="+2348055555504")

    r = await client.patch(
        "/api/v1/users/me/notification-preferences",
        headers=headers,
        json={"sms_alerts": True},
    )
    assert r.status_code == 422, r.text


# ---------------------------------------------------------------------------
# Auth required
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_requires_auth(client):
    r = await client.get("/api/v1/users/me/notification-preferences")
    assert r.status_code == 401, r.text


@pytest.mark.asyncio
async def test_patch_requires_auth(client):
    r = await client.patch(
        "/api/v1/users/me/notification-preferences",
        json={"transaction_alerts": False},
    )
    assert r.status_code == 401, r.text
