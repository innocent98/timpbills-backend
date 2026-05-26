"""B9: /auth/email/verify next_action branches."""
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


@pytest.fixture
def seed_fresh_user(db_session):
    """Register-state user — both gates unverified, no PIN. The matching
    email-verification OTP code is ``123456``."""
    from datetime import UTC, datetime, timedelta

    from app.core.security import hash_pin

    user = User(
        phone="+2348011111111",
        email="b9fresh@example.com",
        full_name="B9 Fresh",
        password_hash="h",
        referral_code="B9F1",
        kyc_level=KycLevel.tier_0,
        email_verified=False,
        is_phone_verified=False,
        pin_hash=None,
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    db_session.add(
        OtpCode(
            user_id=user.id,
            email=user.email,
            code_hash=hash_pin("123456"),
            purpose=OtpPurpose.email_verification,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
    )
    db_session.commit()
    return user


@pytest.fixture
def seed_user_phone_verified_no_pin(db_session):
    """Phone already verified, email still pending, no PIN."""
    from datetime import UTC, datetime, timedelta

    from app.core.security import hash_pin

    user = User(
        phone="+2348022222222",
        email="b9phoneok@example.com",
        full_name="B9 Phone",
        password_hash="h",
        referral_code="B9P1",
        kyc_level=KycLevel.tier_1,
        email_verified=False,
        is_phone_verified=True,
        pin_hash=None,
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    db_session.add(
        OtpCode(
            user_id=user.id,
            email=user.email,
            code_hash=hash_pin("123456"),
            purpose=OtpPurpose.email_verification,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
    )
    db_session.commit()
    return user


@pytest.fixture
def seed_user_phone_verified_with_pin(db_session):
    """Existing-user migration path: phone verified + PIN set; only email pending."""
    from datetime import UTC, datetime, timedelta

    from app.core.security import hash_password, hash_pin

    user = User(
        phone="+2348033333333",
        email="b9full@example.com",
        full_name="B9 Full",
        password_hash=hash_password("Secret1!"),
        referral_code="B9FULL",
        kyc_level=KycLevel.tier_1,
        email_verified=False,
        is_phone_verified=True,
        pin_hash=hash_pin("1234"),
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    db_session.add(
        OtpCode(
            user_id=user.id,
            email=user.email,
            code_hash=hash_pin("123456"),
            purpose=OtpPurpose.email_verification,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
    )
    db_session.commit()
    return user


@pytest.mark.asyncio
async def test_email_verify_alone_returns_phone_verification_required(
    client, seed_fresh_user,
):
    user = seed_fresh_user
    r = await client.post(
        "/api/v1/auth/email/verify",
        json={"email": user.email, "code": "123456"},
    )
    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert body["email_verified"] is True
    assert body["phone_verified"] is False
    assert body["next_action"] == "phone_verification_required"
    assert body.get("pin_setup_token") is None
    assert body.get("tokens") is None


@pytest.mark.asyncio
async def test_email_verify_after_phone_emits_pin_setup_token(
    client, seed_user_phone_verified_no_pin,
):
    user = seed_user_phone_verified_no_pin
    r = await client.post(
        "/api/v1/auth/email/verify",
        json={"email": user.email, "code": "123456"},
    )
    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert body["next_action"] == "pin_setup_required"
    assert body["pin_setup_token"] is not None
    claims = verify_pin_setup_token(body["pin_setup_token"])
    assert claims["sub"] == str(user.id)
    assert claims["scope"] == "pin_setup"
    assert body.get("tokens") is None


@pytest.mark.asyncio
async def test_email_verify_with_phone_and_pin_issues_full_tokens(
    client, seed_user_phone_verified_with_pin,
):
    """Existing-user migration: phone already verified + PIN set; verifying
    email completes the third gate → full tokens issued."""
    user = seed_user_phone_verified_with_pin
    r = await client.post(
        "/api/v1/auth/email/verify",
        json={"email": user.email, "code": "123456"},
    )
    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert body["next_action"] == "tokens_issued"
    assert body["tokens"] is not None
    assert "access_token" in body["tokens"]
    assert "refresh_token" in body["tokens"]
    assert body.get("pin_setup_token") is None
