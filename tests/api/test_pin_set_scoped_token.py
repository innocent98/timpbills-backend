"""B11: /auth/pin/set scoped token contract."""
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
from app.core.security import create_access_token, create_pin_setup_token
from app.integrations.email.fake import FakeEmailClient
from app.main import app
from app.services.token_store import RedisTokenStore

_test_email_client = FakeEmailClient()


@pytest_asyncio.fixture
async def client(db_session):
    """Async client with DB, Redis, email all faked out (same pattern as
    sibling tests in tests/api/)."""

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


def _seed_user_both_verified_no_pin(db_session, *, email="b11both@example.com"):
    from app.core.security import hash_password
    from app.db.models.user import KycLevel, User
    user = User(
        phone="+2348011111111", email=email, full_name="B11 Both",
        password_hash=hash_password("Secret1!"), referral_code="B11BO",
        kyc_level=KycLevel.tier_1,
        email_verified=True, is_phone_verified=True, pin_hash=None,
        is_active=True,
    )
    db_session.add(user); db_session.commit(); db_session.refresh(user)
    return user


def _seed_user_email_verified_no_pin(db_session):
    from app.core.security import hash_password
    from app.db.models.user import KycLevel, User
    user = User(
        phone="+2348022222222", email="b11email@example.com", full_name="B11 Email",
        password_hash=hash_password("Secret1!"), referral_code="B11EM",
        kyc_level=KycLevel.tier_0,
        email_verified=True, is_phone_verified=False, pin_hash=None,
        is_active=True,
    )
    db_session.add(user); db_session.commit(); db_session.refresh(user)
    return user


@pytest.mark.asyncio
async def test_pin_set_with_valid_scoped_token_issues_full_tokens(
    client, db_session,
):
    user = _seed_user_both_verified_no_pin(db_session)
    token = create_pin_setup_token(user_id=str(user.id))
    r = await client.post(
        "/api/v1/auth/pin/set",
        headers={"X-Pin-Setup-Token": token},
        json={"pin": "1234"},
    )
    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert body["pin_set"] is True
    assert "access_token" in body["tokens"]
    assert "refresh_token" in body["tokens"]


@pytest.mark.asyncio
async def test_pin_set_rejects_access_token_in_pin_setup_header(client, db_session):
    user = _seed_user_both_verified_no_pin(
        db_session, email="b11rej-access@example.com",
    )
    access = create_access_token(subject=str(user.id))
    r = await client.post(
        "/api/v1/auth/pin/set",
        headers={"X-Pin-Setup-Token": access},
        json={"pin": "1234"},
    )
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "INVALID_PIN_SETUP_TOKEN"


@pytest.mark.asyncio
async def test_pin_set_rejects_reused_scoped_token(client, db_session):
    """First call succeeds, second call with the same token must be 401."""
    user = _seed_user_both_verified_no_pin(
        db_session, email="b11reuse@example.com",
    )
    token = create_pin_setup_token(user_id=str(user.id))

    r1 = await client.post(
        "/api/v1/auth/pin/set",
        headers={"X-Pin-Setup-Token": token},
        json={"pin": "1234"},
    )
    assert r1.status_code == 200, r1.text

    # Even though pin is now set (which would 409), the jti blocklist
    # should fire first → 401.
    r2 = await client.post(
        "/api/v1/auth/pin/set",
        headers={"X-Pin-Setup-Token": token},
        json={"pin": "5678"},
    )
    assert r2.status_code == 401
    assert r2.json()["error"]["code"] == "INVALID_PIN_SETUP_TOKEN"


@pytest.mark.asyncio
async def test_pin_set_rejects_when_gates_not_met(client, db_session):
    """Phone unverified — even with a valid pin_setup_token (defense-in-depth),
    /pin/set refuses with GATES_NOT_MET."""
    user = _seed_user_email_verified_no_pin(db_session)
    token = create_pin_setup_token(user_id=str(user.id))
    r = await client.post(
        "/api/v1/auth/pin/set",
        headers={"X-Pin-Setup-Token": token},
        json={"pin": "1234"},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "GATES_NOT_MET"


@pytest.mark.asyncio
async def test_pin_set_missing_header_returns_422(client, db_session):
    """No X-Pin-Setup-Token header — FastAPI raises 422 (FastAPI default
    for missing required header)."""
    _seed_user_both_verified_no_pin(
        db_session, email="b11noheader@example.com",
    )
    r = await client.post(
        "/api/v1/auth/pin/set",
        json={"pin": "1234"},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_pin_set_garbage_token_returns_401(client, db_session):
    r = await client.post(
        "/api/v1/auth/pin/set",
        headers={"X-Pin-Setup-Token": "not-a-jwt"},
        json={"pin": "1234"},
    )
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "INVALID_PIN_SETUP_TOKEN"


@pytest.mark.asyncio
async def test_pin_set_invalid_pin_format_returns_422(client, db_session):
    """3-digit or non-numeric pin must be rejected by Pydantic validation."""
    user = _seed_user_both_verified_no_pin(
        db_session, email="b11bad-pin@example.com",
    )
    token = create_pin_setup_token(user_id=str(user.id))
    r = await client.post(
        "/api/v1/auth/pin/set",
        headers={"X-Pin-Setup-Token": token},
        json={"pin": "abc"},
    )
    assert r.status_code == 422
