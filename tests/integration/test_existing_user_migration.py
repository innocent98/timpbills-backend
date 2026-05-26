"""B15: existing-user migration paths under phone-only-auth.

Pre-deploy users always had ``email_verified=True`` (email-verify was the
only verification step) and a mixture of phone-verified / PIN states.
Post-deploy, /auth/login routes them through whichever gates remain open.

Four scenarios:

1. ``email_verified=True``, ``is_phone_verified=False``, no PIN — login
   sends inline phone OTP → phone-verify returns pin_setup_token →
   /pin/set issues full tokens.

2. ``email_verified=True``, ``is_phone_verified=False``, has PIN —
   phone-verify alone completes all gates and issues tokens directly.

3. ``email_verified=True``, ``is_phone_verified=True``, no PIN —
   login skips phone-verify and goes straight to ``pin_setup_required``.

4. After completing migration #3, the resulting refresh_token + new PIN
   round-trips through /auth/pin-login to mint a fresh access+refresh
   pair (the cold-start path the mobile client takes on relaunch).
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
from app.core.security import hash_password, hash_pin
from app.db.models.user import KycLevel, User
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


def _seed_existing_user(
    db_session,
    *,
    phone: str = "+2348011111111",
    email: str = "legacy@example.com",
    is_phone_verified: bool = False,
    has_pin: bool = False,
) -> User:
    """Seed a row in the legacy shape: email_verified=True always
    (pre-deploy that was the only verification), phone_verified +
    pin_hash variable.
    """
    user = User(
        phone=phone,
        email=email,
        full_name="Legacy User",
        password_hash=hash_password("Secret1!"),
        referral_code=f"LEG{phone[-4:]}",
        kyc_level=KycLevel.tier_1 if is_phone_verified else KycLevel.tier_0,
        email_verified=True,
        is_phone_verified=is_phone_verified,
        pin_hash=hash_pin("1234") if has_pin else None,
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.mark.asyncio
async def test_legacy_user_no_phone_no_pin_completes_migration(
    client, db_session, fake_sms_provider,
):
    """Legacy user: phone unverified + no PIN. Login fires inline OTP,
    phone-verify hands back pin_setup_token, /pin/set mints tokens."""
    user = _seed_existing_user(
        db_session,
        phone="+2348033333333",
        email="legacy-noboth@example.com",
    )
    fake_sms_provider.sent.clear()

    # 1. Login — phone gate is first unverified gate → inline OTP send.
    r = await client.post("/api/v1/auth/login", json={
        "phone": user.phone,
        "password": "Secret1!",
    })
    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert body["next_action"] == "phone_verification_required"
    assert body["phone_otp_sent"] is True
    assert body.get("tokens") is None
    assert len(fake_sms_provider.sent) == 1
    phone_otp = fake_sms_provider.sent[-1].code_or_message

    # 2. Verify phone → pin_setup_required + scoped token.
    r = await client.post("/api/v1/auth/phone/verify", json={
        "phone": user.phone,
        "code": phone_otp,
    })
    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert body["next_action"] == "pin_setup_required"
    pin_setup_token = body["pin_setup_token"]
    assert pin_setup_token is not None
    assert body.get("tokens") is None

    # 3. /pin/set → full access+refresh.
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


@pytest.mark.asyncio
async def test_legacy_user_with_pin_skips_pin_step(
    client, db_session, fake_sms_provider,
):
    """Legacy user who set a PIN before the migration: phone verification
    alone clears every gate → tokens_issued directly."""
    user = _seed_existing_user(
        db_session,
        phone="+2348044444444",
        email="legacy-haspin@example.com",
        has_pin=True,
    )
    fake_sms_provider.sent.clear()

    # 1. Login — inline OTP sent.
    r = await client.post("/api/v1/auth/login", json={
        "phone": user.phone,
        "password": "Secret1!",
    })
    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert body["next_action"] == "phone_verification_required"
    assert body["phone_otp_sent"] is True
    phone_otp = fake_sms_provider.sent[-1].code_or_message

    # 2. Verify phone → tokens issued directly (PIN already present, no /pin/set
    #    round-trip needed).
    r = await client.post("/api/v1/auth/phone/verify", json={
        "phone": user.phone,
        "code": phone_otp,
    })
    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert body["next_action"] == "tokens_issued"
    assert body.get("pin_setup_token") is None
    assert "access_token" in body["tokens"]
    assert "refresh_token" in body["tokens"]


@pytest.mark.asyncio
async def test_legacy_user_phone_verified_no_pin_routes_to_pin_setup(
    client, db_session,
):
    """Legacy tier-1 (phone already verified pre-deploy) with no PIN:
    login skips the phone-verify gate entirely and returns
    ``pin_setup_required`` + a scoped token straight away."""
    user = _seed_existing_user(
        db_session,
        phone="+2348055555555",
        email="legacy-tier1@example.com",
        is_phone_verified=True,
        has_pin=False,
    )

    r = await client.post("/api/v1/auth/login", json={
        "phone": user.phone,
        "password": "Secret1!",
    })
    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert body["next_action"] == "pin_setup_required"
    assert body["pin_setup_token"] is not None
    assert body.get("tokens") is None


@pytest.mark.asyncio
async def test_full_pin_login_round_trip(client, db_session):
    """After a legacy user completes the migration, the refresh_token
    minted by /pin/set must round-trip through /auth/pin-login.

    Mirrors the cold-start path mobile takes on relaunch: persisted refresh
    is presented alongside the PIN; backend issues a brand-new access+refresh
    pair and the old refresh is rotated out (the new refresh_token MUST
    differ from the seed refresh_token).
    """
    user = _seed_existing_user(
        db_session,
        phone="+2348066666666",
        email="legacy-roundtrip@example.com",
        is_phone_verified=True,
        has_pin=False,
    )

    # Complete migration in-test to obtain a fresh refresh_token.
    r = await client.post("/api/v1/auth/login", json={
        "phone": user.phone,
        "password": "Secret1!",
    })
    assert r.status_code == 200, r.text
    pin_setup_token = r.json()["data"]["pin_setup_token"]

    r = await client.post(
        "/api/v1/auth/pin/set",
        headers={"X-Pin-Setup-Token": pin_setup_token},
        json={"pin": "5678"},
    )
    assert r.status_code == 200, r.text
    refresh_token = r.json()["data"]["tokens"]["refresh_token"]

    # Exercise /pin-login with the freshly-minted refresh + the same PIN.
    r = await client.post("/api/v1/auth/pin-login", json={
        "refresh_token": refresh_token,
        "pin": "5678",
    })
    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert "access_token" in body["tokens"]
    assert "refresh_token" in body["tokens"]
    # Refresh rotates — the new refresh_token must differ from the seed.
    assert body["tokens"]["refresh_token"] != refresh_token
