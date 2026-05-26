"""B10: public /auth/phone/verify — used during signup + existing-user
migration. Mirrors /auth/email/verify shape and gate logic."""
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
from app.core.security import verify_pin_setup_token
from app.db.models.otp import OtpCode, OtpPurpose
from app.db.models.user import KycLevel, User
from app.integrations.email.fake import FakeEmailClient
from app.main import app
from app.services.token_store import RedisTokenStore

_test_email_client = FakeEmailClient()


@pytest_asyncio.fixture
async def client(db_session):
    """Async client with DB, Redis, email all faked out (same pattern as
    sibling B9 test)."""

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


def _seed_otp(db_session, user, code="123456"):
    from datetime import UTC, datetime, timedelta

    from app.core.security import hash_pin
    db_session.add(OtpCode(
        user_id=user.id, phone=user.phone,
        code_hash=hash_pin(code),
        purpose=OtpPurpose.phone_verification,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    ))
    db_session.commit()


@pytest.fixture
def seed_fresh_user(db_session):
    """No verifications yet. Phone OTP seeded."""
    user = User(
        phone="+2348011111111", email="b10fresh@example.com", full_name="B10 Fresh",
        password_hash="h", referral_code="B10F1",
        kyc_level=KycLevel.tier_0,
        email_verified=False, is_phone_verified=False, pin_hash=None,
        is_active=True,
    )
    db_session.add(user); db_session.commit(); db_session.refresh(user)
    _seed_otp(db_session, user)
    return user


@pytest.fixture
def seed_email_verified_no_pin(db_session):
    user = User(
        phone="+2348022222222", email="b10email@example.com", full_name="B10 Email",
        password_hash="h", referral_code="B10E1",
        kyc_level=KycLevel.tier_0,
        email_verified=True, is_phone_verified=False, pin_hash=None,
        is_active=True,
    )
    db_session.add(user); db_session.commit(); db_session.refresh(user)
    _seed_otp(db_session, user)
    return user


@pytest.fixture
def seed_email_verified_with_pin(db_session):
    from app.core.security import hash_pin, hash_password
    user = User(
        phone="+2348033333333", email="b10full@example.com", full_name="B10 Full",
        password_hash=hash_password("Secret1!"), referral_code="B10F",
        kyc_level=KycLevel.tier_0,
        email_verified=True, is_phone_verified=False,
        pin_hash=hash_pin("1234"),
        is_active=True,
    )
    db_session.add(user); db_session.commit(); db_session.refresh(user)
    _seed_otp(db_session, user)
    return user


@pytest.mark.asyncio
async def test_phone_verify_alone_requires_email_verification(client, seed_fresh_user):
    user = seed_fresh_user
    r = await client.post("/api/v1/auth/phone/verify", json={
        "phone": user.phone, "code": "123456",
    })
    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert body["next_action"] == "email_verification_required"
    assert body["phone_verified"] is True
    assert body["email_verified"] is False
    assert body.get("pin_setup_token") is None
    assert body.get("tokens") is None


@pytest.mark.asyncio
async def test_phone_verify_with_email_verified_emits_pin_setup_token(
    client, seed_email_verified_no_pin,
):
    user = seed_email_verified_no_pin
    r = await client.post("/api/v1/auth/phone/verify", json={
        "phone": user.phone, "code": "123456",
    })
    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert body["next_action"] == "pin_setup_required"
    claims = verify_pin_setup_token(body["pin_setup_token"])
    assert claims["sub"] == str(user.id)
    assert claims["scope"] == "pin_setup"


@pytest.mark.asyncio
async def test_phone_verify_with_email_and_pin_issues_full_tokens(
    client, seed_email_verified_with_pin,
):
    """Existing-user migration: email already verified + PIN already set;
    verifying phone is the final gate → full tokens."""
    user = seed_email_verified_with_pin
    r = await client.post("/api/v1/auth/phone/verify", json={
        "phone": user.phone, "code": "123456",
    })
    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert body["next_action"] == "tokens_issued"
    assert "access_token" in body["tokens"]


@pytest.mark.asyncio
async def test_phone_verify_normalises_local_format(client, seed_fresh_user):
    """Calling with local format (080...) instead of E.164 must still find
    the user via normalisation."""
    # User stored as +2348011111111 via the fixture; only the side effect
    # (insert) is what we need — the local-format call below must still
    # resolve the same row via normalize_to_e164.
    assert seed_fresh_user.phone == "+2348011111111"
    r = await client.post("/api/v1/auth/phone/verify", json={
        "phone": "08011111111",   # local format
        "code": "123456",
    })
    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert body["phone_verified"] is True


@pytest.mark.asyncio
async def test_phone_verify_bad_format_returns_400(client):
    r = await client.post("/api/v1/auth/phone/verify", json={
        "phone": "not-a-phone", "code": "123456",
    })
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_PHONE_FORMAT"
