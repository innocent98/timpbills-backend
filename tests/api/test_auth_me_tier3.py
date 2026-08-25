"""Task A8: /auth/me tier_3 regression guard (Track A · Backend).

`/auth/me` projects ``User.kyc_level`` (a ``KycLevel`` enum) through
``KycLevel.numeric`` to the numeric tier mobile expects. This test locks
in tier_3 -> 3 specifically so a future change to the enum or the
serializer can't silently regress the mobile tier gate.

Same fixture shape as tests/api/test_login_next_action.py (B12): a
directly-seeded User + fakeredis + fake email, login for tokens, then
call the endpoint under test.
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
from app.core.security import hash_password, hash_pin
from app.db.models.user import KycLevel, User
from app.integrations.email.fake import FakeEmailClient
from app.main import app
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


def _seed_tier3_user(db_session) -> User:
    """A fully-verified, tier_3 user — all login gates already pass."""
    user = User(
        phone="+2348055555501",
        email="a8-tier3@example.com",
        full_name="A8 Tier3 User",
        password_hash=hash_password("Secret1!"),
        referral_code="A8TIER3",
        kyc_level=KycLevel.tier_3,
        email_verified=True,
        is_phone_verified=True,
        pin_hash=hash_pin("1234"),
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.mark.asyncio
async def test_auth_me_reports_numeric_tier_3_for_tier3_user(client, db_session):
    user = _seed_tier3_user(db_session)

    login_r = await client.post(
        "/api/v1/auth/login",
        json={"phone": user.phone, "password": "Secret1!"},
    )
    assert login_r.status_code == 200, login_r.text
    login_body = login_r.json()["data"]
    assert login_body["next_action"] == "tokens_issued"
    access_token = login_body["tokens"]["access_token"]

    me_r = await client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert me_r.status_code == 200, me_r.text
    assert me_r.json()["data"]["kyc_level"] == 3
