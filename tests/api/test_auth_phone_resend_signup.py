"""Public /auth/phone/resend — unauthenticated signup OTP resend.

Distinct from the authenticated /auth/phone/send-otp (in-session Tier 1
upgrade). No Authorization header is ever sent here: the user has no tokens
yet during signup, so a 200 / business-logic-4xx (never 401) confirms the
public path. Mirrors the sibling test_auth_phone_verify_signup fixtures.
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
from app.db.models.otp import OtpCode, OtpPurpose
from app.db.models.user import KycLevel, User
from app.integrations.email.fake import FakeEmailClient
from app.main import app
from app.services.token_store import RedisTokenStore

_test_email_client = FakeEmailClient()


@pytest_asyncio.fixture
async def client(db_session):
    """Async client with DB, Redis, email all faked out (same pattern as
    sibling phone-verify signup test)."""

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
def seed_unverified_user(db_session):
    """Unverified phone, no prior OTP — cooldown window open."""
    user = User(
        phone="+2348011111111", email="resendfresh@example.com",
        full_name="Resend Fresh",
        password_hash="h", referral_code="RSNDA1",
        kyc_level=KycLevel.tier_0,
        email_verified=False, is_phone_verified=False, pin_hash=None,
        is_active=True,
    )
    db_session.add(user); db_session.commit(); db_session.refresh(user)
    return user


@pytest.fixture
def seed_phone_verified_user(db_session):
    user = User(
        phone="+2348022222222", email="resendverified@example.com",
        full_name="Resend Verified",
        password_hash="h", referral_code="RSNDA2",
        kyc_level=KycLevel.tier_1,
        email_verified=False, is_phone_verified=True, pin_hash=None,
        is_active=True,
    )
    db_session.add(user); db_session.commit(); db_session.refresh(user)
    return user


@pytest.mark.asyncio
async def test_resend_unverified_returns_200_and_creates_otp(
    client, seed_unverified_user, db_session,
):
    user = seed_unverified_user
    r = await client.post("/api/v1/auth/phone/resend", json={"phone": user.phone})
    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert body["phone_otp_sent"] is True
    otp = (
        db_session.query(OtpCode)
        .filter_by(user_id=user.id, purpose=OtpPurpose.phone_verification)
        .order_by(OtpCode.created_at.desc())
        .first()
    )
    assert otp is not None


@pytest.mark.asyncio
async def test_resend_unknown_phone_returns_404(client):
    r = await client.post(
        "/api/v1/auth/phone/resend", json={"phone": "+2348070000000"},
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "USER_NOT_FOUND"


@pytest.mark.asyncio
async def test_resend_already_verified_returns_409(client, seed_phone_verified_user):
    user = seed_phone_verified_user
    r = await client.post("/api/v1/auth/phone/resend", json={"phone": user.phone})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "PHONE_ALREADY_VERIFIED"


@pytest.mark.asyncio
async def test_resend_bad_format_returns_400(client):
    r = await client.post(
        "/api/v1/auth/phone/resend", json={"phone": "not-a-phone"},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_PHONE_FORMAT"


@pytest.mark.asyncio
async def test_resend_normalises_local_format(client, seed_unverified_user):
    """Local format (080...) must still resolve the same user via
    normalize_to_e164 → 200."""
    assert seed_unverified_user.phone == "+2348011111111"
    r = await client.post(
        "/api/v1/auth/phone/resend", json={"phone": "08011111111"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["phone_otp_sent"] is True
