"""API tests for PATCH /api/v1/auth/me (Sprint 5c · Task 2.2).

Covers:
* happy path — full_name patch persists + response shape.
* happy path — DOB + gender + address patch.
* email rejected (422)            — UserUpdateRequest has extra="forbid".
* phone rejected (422)            — same reason; phone has its own OTP flow.
* auth required                   — missing/invalid bearer returns 401.
* age <18 rejected (422)          — schema-level validator.
* partial PATCH preserves untouched fields.
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


async def _seed_user(
    client: AsyncClient,
    *,
    email: str,
    phone: str,
    full_name: str = "Patch Me User",
) -> dict:
    """Register + verify email; return auth headers ready for protected calls."""
    r = await client.post(
        "/api/v1/auth/register",
        json={
            "full_name": full_name,
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
        "/api/v1/auth/email/verify",
        json={"email": email, "code": code},
    )
    assert r2.status_code == 200, r2.text
    tokens = r2.json()["data"]["tokens"]
    return {"Authorization": f"Bearer {tokens['access_token']}"}


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_patch_me_full_name_happy(client):
    headers = await _seed_user(
        client, email="pm1@test.co", phone="+2348044444401", full_name="Ada Lovelace"
    )

    r = await client.patch(
        "/api/v1/auth/me", json={"full_name": "Ada L."}, headers=headers
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["full_name"] == "Ada L."
    # Profile fields default to null until set
    assert data["date_of_birth"] is None
    assert data["gender"] is None
    assert data["address"] is None
    assert data["avatar_url"] is None
    # Verification flags still surface
    assert data["email"] == "pm1@test.co"
    assert data["phone"] == "+2348044444401"
    assert data["email_verified"] is True


@pytest.mark.asyncio
async def test_patch_me_dob_gender_address_happy(client):
    headers = await _seed_user(
        client, email="pm2@test.co", phone="+2348044444402"
    )

    r = await client.patch(
        "/api/v1/auth/me",
        json={
            "date_of_birth": "1990-05-21",
            "gender": "female",
            "address": "42 Marina Road, Lagos",
        },
        headers=headers,
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["date_of_birth"] == "1990-05-21"
    assert data["gender"] == "female"
    assert data["address"] == "42 Marina Road, Lagos"

    # GET /me reflects the persisted mutation
    g = await client.get("/api/v1/auth/me", headers=headers)
    assert g.status_code == 200
    gdata = g.json()["data"]
    assert gdata["date_of_birth"] == "1990-05-21"
    assert gdata["gender"] == "female"
    assert gdata["address"] == "42 Marina Road, Lagos"


# ---------------------------------------------------------------------------
# extra="forbid" — email/phone routed elsewhere
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_patch_me_email_rejected_as_extra(client):
    headers = await _seed_user(
        client, email="pm3@test.co", phone="+2348044444403"
    )
    r = await client.patch(
        "/api/v1/auth/me", json={"email": "new@test.co"}, headers=headers
    )
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_patch_me_phone_rejected_as_extra(client):
    headers = await _seed_user(
        client, email="pm4@test.co", phone="+2348044444404"
    )
    r = await client.patch(
        "/api/v1/auth/me", json={"phone": "+2348099999999"}, headers=headers
    )
    assert r.status_code == 422, r.text


# ---------------------------------------------------------------------------
# Auth & validation gates
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_patch_me_auth_required(client):
    r = await client.patch("/api/v1/auth/me", json={"full_name": "Anon"})
    assert r.status_code == 401, r.text


@pytest.mark.asyncio
async def test_patch_me_age_below_18_rejected(client):
    headers = await _seed_user(
        client, email="pm5@test.co", phone="+2348044444405"
    )
    # Today minus 10 years — clearly under 18
    import datetime as dt

    too_young = (dt.date.today() - dt.timedelta(days=365 * 10)).isoformat()
    r = await client.patch(
        "/api/v1/auth/me", json={"date_of_birth": too_young}, headers=headers
    )
    assert r.status_code == 422, r.text
    assert "18" in r.text


# ---------------------------------------------------------------------------
# Partial updates preserve untouched fields
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_patch_me_partial_preserves_untouched(client):
    headers = await _seed_user(
        client,
        email="pm6@test.co",
        phone="+2348044444406",
        full_name="Original Name",
    )

    # First set DOB + gender
    r1 = await client.patch(
        "/api/v1/auth/me",
        json={"date_of_birth": "1985-03-15", "gender": "male"},
        headers=headers,
    )
    assert r1.status_code == 200, r1.text

    # Now patch only address — DOB & gender must survive
    r2 = await client.patch(
        "/api/v1/auth/me",
        json={"address": "1 Ahmadu Bello Way, Abuja"},
        headers=headers,
    )
    assert r2.status_code == 200, r2.text
    data = r2.json()["data"]
    assert data["address"] == "1 Ahmadu Bello Way, Abuja"
    assert data["date_of_birth"] == "1985-03-15"
    assert data["gender"] == "male"
    assert data["full_name"] == "Original Name"
