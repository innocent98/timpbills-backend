"""B12: phone-only /auth/login + next_action routing + inline OTP send.

Replaces the old (email|phone) identifier + always-tokens contract.

Gate evaluation order on login: email → phone → pin. The first
unverified gate sets the ``next_action``; the response shape mirrors
the one /auth/email/verify and /auth/phone/verify already emit so
mobile reuses the same router.

When the phone gate is the first unverified gate, /login also sends a
fresh phone OTP inline (subject to the cooldown helper) so the mobile
client can transition straight to the OTP entry screen without an
extra round-trip.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from fakeredis.aioredis import FakeRedis
from httpx import ASGITransport, AsyncClient

from app.api.deps import (
    _fake_sms_singleton,
    get_db,
    get_email_provider,
    get_redis,
    get_token_store,
    reset_fake_email,
    reset_fake_sms,
)
from app.core.limiter import limiter
from app.core.security import (
    hash_password,
    hash_pin,
    verify_pin_setup_token,
)
from app.db.models.otp import OtpCode, OtpPurpose
from app.db.models.user import KycLevel, User
from app.integrations.email.fake import FakeEmailClient
from app.main import app
from app.services.token_store import RedisTokenStore

_test_email_client = FakeEmailClient()


@pytest_asyncio.fixture
async def client(db_session):
    """Async client with DB, Redis, email all faked out (same pattern as
    sibling B9/B10 tests). The SMS singleton is the module-level
    ``_fake_sms_singleton`` exposed via ``fake_sms_provider`` below."""

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
def fake_sms_provider():
    """Expose the singleton FakeTermiiClient the deps use by default so
    tests can inspect ``.sent`` and clear it between assertions."""
    _fake_sms_singleton.sent.clear()
    return _fake_sms_singleton


def _seed(db, *, email_verified=True, phone_verified=True, has_pin=True,
          phone="+2348011111111", email="b12@example.com"):
    user = User(
        phone=phone, email=email, full_name="B12 User",
        password_hash=hash_password("Secret1!"),
        referral_code=f"B12{phone[-4:]}",
        kyc_level=KycLevel.tier_1 if phone_verified else KycLevel.tier_0,
        email_verified=email_verified,
        is_phone_verified=phone_verified,
        pin_hash=hash_pin("1234") if has_pin else None,
        is_active=True,
    )
    db.add(user); db.commit(); db.refresh(user)
    return user


@pytest.mark.asyncio
async def test_login_all_gates_passed_issues_tokens(client, db_session):
    user = _seed(db_session)
    r = await client.post("/api/v1/auth/login", json={
        "phone": user.phone, "password": "Secret1!",
    })
    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert body["next_action"] == "tokens_issued"
    assert "access_token" in body["tokens"]
    assert body["email"] == "b12@example.com"


@pytest.mark.asyncio
async def test_login_email_unverified_returns_email_action(client, db_session):
    """Email gate: response carries the user's ``email`` and dispatches a
    fresh email OTP inline (no prior code → cooldown passes)."""
    user = _seed(db_session,
                 email_verified=False,
                 email="b12-noemail@example.com")
    _test_email_client.sent.clear()
    r = await client.post("/api/v1/auth/login", json={
        "phone": user.phone, "password": "Secret1!",
    })
    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert body["next_action"] == "email_verification_required"
    assert body.get("tokens") is None
    assert body["email"] == "b12-noemail@example.com"
    assert body["email_otp_sent"] is True
    # A fresh code was queued via the fake email client.
    assert len(_test_email_client.sent) == 1
    assert _test_email_client.sent[0].to == "b12-noemail@example.com"


@pytest.mark.asyncio
async def test_login_email_unverified_respects_cooldown(client, db_session):
    """A recent email-verification OTP inside the cooldown window makes the
    inline login send a no-op: ``email_otp_sent=False`` and no new email."""
    from datetime import UTC, datetime, timedelta
    user = _seed(db_session,
                 email_verified=False,
                 phone="+2348088888888",
                 email="b12-emailcooldown@example.com")
    db_session.add(OtpCode(
        user_id=user.id, email=user.email,
        code_hash=hash_pin("000000"),
        purpose=OtpPurpose.email_verification,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        created_at=datetime.now(UTC) - timedelta(seconds=10),
    ))
    db_session.commit()
    _test_email_client.sent.clear()

    r = await client.post("/api/v1/auth/login", json={
        "phone": user.phone, "password": "Secret1!",
    })
    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert body["next_action"] == "email_verification_required"
    assert body["email"] == "b12-emailcooldown@example.com"
    assert body["email_otp_sent"] is False
    assert len(_test_email_client.sent) == 0


@pytest.mark.asyncio
async def test_login_phone_unverified_sends_inline_otp(
    client, db_session, fake_sms_provider,
):
    user = _seed(db_session,
                 phone_verified=False, has_pin=False,
                 phone="+2348022222222",
                 email="b12-nophone@example.com")
    fake_sms_provider.sent.clear()
    r = await client.post("/api/v1/auth/login", json={
        "phone": user.phone, "password": "Secret1!",
    })
    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert body["next_action"] == "phone_verification_required"
    assert body["phone_otp_sent"] is True
    assert body.get("tokens") is None
    # Termii / Fake provider received a send.
    assert len(fake_sms_provider.sent) == 1
    assert fake_sms_provider.sent[0].phone == "+2348022222222"


@pytest.mark.asyncio
async def test_login_phone_unverified_respects_cooldown(
    client, db_session, fake_sms_provider,
):
    """If an OTP was sent within the cooldown window, the second login
    returns the same next_action but phone_otp_sent=False and no new SMS
    is dispatched."""
    from datetime import UTC, datetime, timedelta
    user = _seed(db_session,
                 phone_verified=False, has_pin=False,
                 phone="+2348033333333",
                 email="b12-cooldown@example.com")
    # Seed a recent phone-verification OTP (within cooldown window).
    db_session.add(OtpCode(
        user_id=user.id, phone=user.phone,
        code_hash=hash_pin("000000"),
        purpose=OtpPurpose.phone_verification,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        created_at=datetime.now(UTC) - timedelta(seconds=10),
    ))
    db_session.commit()
    fake_sms_provider.sent.clear()

    r = await client.post("/api/v1/auth/login", json={
        "phone": user.phone, "password": "Secret1!",
    })
    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert body["next_action"] == "phone_verification_required"
    assert body["phone_otp_sent"] is False
    assert len(fake_sms_provider.sent) == 0


@pytest.mark.asyncio
async def test_login_no_pin_returns_pin_setup_token(client, db_session):
    user = _seed(db_session,
                 has_pin=False,
                 phone="+2348044444444",
                 email="b12-nopin@example.com")
    r = await client.post("/api/v1/auth/login", json={
        "phone": user.phone, "password": "Secret1!",
    })
    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert body["next_action"] == "pin_setup_required"
    assert body["pin_setup_token"] is not None
    claims = verify_pin_setup_token(body["pin_setup_token"])
    assert claims["sub"] == str(user.id)


@pytest.mark.asyncio
async def test_login_rejects_email_as_phone(client, db_session):
    """Phone-only — passing an email in the phone field must 400."""
    user = _seed(db_session,
                 phone="+2348055555555",
                 email="b12-emailrej@example.com")
    r = await client.post("/api/v1/auth/login", json={
        "phone": user.email,        # email in phone field
        "password": "Secret1!",
    })
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_PHONE_FORMAT"


@pytest.mark.asyncio
async def test_login_wrong_password_returns_401(client, db_session):
    _seed(db_session,
          phone="+2348066666666",
          email="b12-wrongpw@example.com")
    r = await client.post("/api/v1/auth/login", json={
        "phone": "+2348066666666", "password": "WrongPassword1!",
    })
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "INVALID_CREDENTIALS"


@pytest.mark.asyncio
async def test_login_local_phone_format_works(client, db_session):
    """Calling with local 080... format must find the user (stored as +234)."""
    _seed(db_session,
          phone="+2348077777777",
          email="b12-local@example.com")
    r = await client.post("/api/v1/auth/login", json={
        "phone": "08077777777",   # local format
        "password": "Secret1!",
    })
    assert r.status_code == 200, r.text
    assert r.json()["data"]["next_action"] == "tokens_issued"
