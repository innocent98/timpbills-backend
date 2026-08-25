"""B15: Full new-user registration → both OTPs verified → PIN set → tokens.

End-to-end happy path for the phone-only-auth contract, exercising both
orderings of the email/phone verification gates and the subsequent
``pin_setup_token`` → /auth/pin/set → /auth/login round-trip.

Lazy-phone-OTP note: register now sends ONLY the email OTP. The phone OTP
is dispatched once the phone gate becomes active — i.e. on the email/verify
call (``phone_otp_sent=True``). The phone-first ordering test therefore
seeds the phone OTP directly (mirroring tests/api/test_auth_phone_verify_signup.py)
to exercise the phone→email gate routing without depending on register to
auto-send a phone OTP.

The fixture wiring mirrors the B9/B10/B11/B12 single-endpoint tests: a
per-test in-memory DB, fakeredis, the singleton FakeEmailClient + the
deps-module FakeTermiiClient singleton, slowapi disabled. We rely on the
fact that when ``FORCE_FAKE_PROVIDERS=True`` (set by the autouse
conftest fixture), get_sms_provider resolves to ``_fake_sms_singleton``
already; we surface that singleton through ``fake_sms_provider`` so the
test code stays symmetric with the B12 file.
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
from app.integrations.email.fake import FakeEmailClient
from app.main import app
from app.services.token_store import RedisTokenStore

_test_email_client = FakeEmailClient()


@pytest_asyncio.fixture
async def client(db_session):
    """Async client with DB, Redis, email all faked out (B12 pattern)."""

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
    """The singleton FakeTermiiClient deps resolve to under FORCE_FAKE_PROVIDERS."""
    _fake_sms_singleton.sent.clear()
    return _fake_sms_singleton


@pytest.fixture
def fake_email_provider():
    """The module-local FakeEmailClient bound via the get_email_provider override."""
    _test_email_client.sent.clear()
    return _test_email_client


@pytest.mark.asyncio
async def test_full_flow_email_then_phone(
    client, db_session, fake_sms_provider, fake_email_provider,
):
    """Email-first verification ordering; pin_setup_token issued from phone/verify."""
    # 1. Register — both OTPs delivered, no tokens, next_action verify_email_and_phone.
    r = await client.post("/api/v1/auth/register", json={
        "phone": "08011111111",
        "email": "fullflow1@example.com",
        "full_name": "Full Flow One",
        "password": "Secret1!",
    })
    assert r.status_code == 201, r.text
    body = r.json()["data"]
    assert body["next_action"] == "verify_email_and_phone"
    assert "tokens" not in body
    assert body["phone"] == "+2348011111111"  # phone normalised before persistence

    # Register sent only the email OTP — no phone SMS yet.
    assert len(fake_email_provider.sent) == 1
    assert len(fake_sms_provider.sent) == 0
    email_otp = fake_email_provider.sent[-1].code_or_body

    # 2. Verify email first — phone gate becomes active, phone OTP sent now,
    #    no pin_setup_token yet.
    r = await client.post("/api/v1/auth/email/verify", json={
        "email": "fullflow1@example.com",
        "code": email_otp,
    })
    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert body["next_action"] == "phone_verification_required"
    assert body["phone_otp_sent"] is True
    assert body.get("pin_setup_token") is None
    assert body.get("tokens") is None

    # The phone OTP was dispatched at the email/verify step.
    assert len(fake_sms_provider.sent) == 1
    phone_otp = fake_sms_provider.sent[-1].code_or_message

    # 3. Verify phone — both gates pass + no PIN → pin_setup_token issued.
    r = await client.post("/api/v1/auth/phone/verify", json={
        "phone": "+2348011111111",
        "code": phone_otp,
    })
    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert body["next_action"] == "pin_setup_required"
    pin_setup_token = body["pin_setup_token"]
    assert pin_setup_token is not None
    assert body.get("tokens") is None

    # 4. Set PIN with the scoped token → full access+refresh.
    r = await client.post(
        "/api/v1/auth/pin/set",
        headers={"X-Pin-Setup-Token": pin_setup_token},
        json={"pin": "1234"},
    )
    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert body["pin_set"] is True
    tokens = body["tokens"]
    assert "access_token" in tokens
    assert "refresh_token" in tokens

    # 5. Subsequent /login with phone+password goes straight to tokens_issued.
    r = await client.post("/api/v1/auth/login", json={
        "phone": "08011111111",
        "password": "Secret1!",
    })
    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert body["next_action"] == "tokens_issued"
    assert "access_token" in body["tokens"]


@pytest.mark.asyncio
async def test_full_flow_phone_then_email(
    client, db_session, fake_sms_provider, fake_email_provider,
):
    """Phone-first verification ordering; pin_setup_token issued from email/verify.

    Register no longer auto-sends a phone OTP, and the lazy send only fires
    once email is verified — so the phone-first ordering is reached here by
    seeding a phone_verification OTP directly (the same way mobile would
    obtain one via a phone-OTP resend). This still exercises the
    phone→email gate routing in verify_phone_otp_unauthed.
    """
    from datetime import UTC, datetime, timedelta

    from app.core.security import hash_pin
    from app.db.models.otp import OtpCode, OtpPurpose
    from app.db.models.user import User

    # 1. Register.
    r = await client.post("/api/v1/auth/register", json={
        "phone": "08022222222",
        "email": "fullflow2@example.com",
        "full_name": "Full Flow Two",
        "password": "Secret1!",
    })
    assert r.status_code == 201, r.text
    email_otp = fake_email_provider.sent[-1].code_or_body
    assert len(fake_sms_provider.sent) == 0  # no phone OTP at register

    # Seed a phone_verification OTP so the phone gate can be cleared first.
    phone_otp = "654321"
    user = db_session.query(User).filter(User.email == "fullflow2@example.com").one()
    db_session.add(OtpCode(
        user_id=user.id, phone=user.phone,
        code_hash=hash_pin(phone_otp),
        purpose=OtpPurpose.phone_verification,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    ))
    db_session.commit()

    # 2. Verify phone first — email gate still open, no pin_setup_token yet.
    r = await client.post("/api/v1/auth/phone/verify", json={
        "phone": "+2348022222222",
        "code": phone_otp,
    })
    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert body["next_action"] == "email_verification_required"
    assert body.get("pin_setup_token") is None
    assert body.get("tokens") is None

    # 3. Verify email — both gates pass + no PIN → pin_setup_token.
    r = await client.post("/api/v1/auth/email/verify", json={
        "email": "fullflow2@example.com",
        "code": email_otp,
    })
    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert body["next_action"] == "pin_setup_required"
    pin_setup_token = body["pin_setup_token"]
    assert pin_setup_token is not None
    assert body.get("tokens") is None

    # 4. Set PIN → full tokens.
    r = await client.post(
        "/api/v1/auth/pin/set",
        headers={"X-Pin-Setup-Token": pin_setup_token},
        json={"pin": "1234"},
    )
    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert body["pin_set"] is True
    assert "access_token" in body["tokens"]
    assert "refresh_token" in body["tokens"]
