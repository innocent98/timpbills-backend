"""B13: /auth/pin-login — cold-start PIN authentication.

Covers:
  1. Happy path — valid refresh + correct PIN → fresh access+refresh, rotated jti.
  2. Wrong PIN → 401 INVALID_PIN.
  3. Garbage refresh token → 401 INVALID_TOKEN.
  4. Access token in refresh_token field (typ mismatch) → 401 INVALID_TOKEN.
  5. Already-rotated refresh (replay) → 401 + nukes ALL of the user's sessions.
  6. User without pin_hash → 400 PIN_NOT_SET (defense-in-depth).
  7. PIN-locked user → 423 PIN_LOCKED.
  8. Inactive account → 403 ACCOUNT_DISABLED.
"""
from datetime import timedelta
from uuid import uuid4

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
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    hash_pin,
)
from app.db.models.user import KycLevel, User
from app.integrations.email.fake import FakeEmailClient
from app.main import app
from app.services.token_store import RedisTokenStore

_test_email_client = FakeEmailClient()


@pytest_asyncio.fixture
async def client(db_session):
    """Async client wired to in-memory SQLite + fakeredis; shares the same
    fake_redis instance across the token_store + pin_service so the test
    can pre-seed lockout keys directly via the redis dependency."""

    def _get_db():
        try:
            yield db_session
        finally:
            pass

    fake_redis = FakeRedis(decode_responses=True)

    def _get_token_store():
        return RedisTokenStore(redis=fake_redis)

    async def _get_redis():
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
        c._fake_redis = fake_redis  # type: ignore[attr-defined]
        yield c
    limiter.enabled = True
    await fake_redis.aclose()
    app.dependency_overrides.clear()


def _seed_user_with_active_refresh(db_session) -> tuple[User, str, str]:
    """Returns (user, refresh_token, refresh_jti). Caller is responsible for
    persisting the jti to the token store via the redis fixture."""
    user = User(
        phone="+2348011111111",
        email="b13@example.com",
        full_name="B13 User",
        password_hash=hash_password("Secret1!"),
        referral_code=f"B13{uuid4().hex[:4].upper()}",
        kyc_level=KycLevel.tier_1,
        email_verified=True,
        is_phone_verified=True,
        pin_hash=hash_pin("1234"),
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    jti = uuid4().hex
    refresh = create_refresh_token(
        subject=str(user.id), jti=jti, expires_in=timedelta(days=30),
    )
    return user, refresh, jti


# ---------------------------------------------------------------------------
# 1. Happy path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pin_login_happy_path(client, db_session):
    user, refresh_token, jti = _seed_user_with_active_refresh(db_session)
    fake_redis = client._fake_redis  # type: ignore[attr-defined]
    store = RedisTokenStore(redis=fake_redis)
    await store.save(user_id=str(user.id), jti=jti, ttl_seconds=30 * 86400)

    r = await client.post(
        "/api/v1/auth/pin-login",
        json={"refresh_token": refresh_token, "pin": "1234"},
    )
    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert "access_token" in body["tokens"]
    assert "refresh_token" in body["tokens"]
    assert body["pin_set"] is True
    # New refresh is rotated (different jti).
    new_payload = decode_token(body["tokens"]["refresh_token"])
    assert new_payload["jti"] != jti
    # Old jti is rotated out, new jti is saved.
    assert await store.is_valid(user_id=str(user.id), jti=jti) is False
    assert await store.is_valid(
        user_id=str(user.id), jti=new_payload["jti"],
    ) is True


# ---------------------------------------------------------------------------
# 2. Wrong PIN → 401
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pin_login_wrong_pin_returns_401(client, db_session):
    user, refresh_token, jti = _seed_user_with_active_refresh(db_session)
    fake_redis = client._fake_redis  # type: ignore[attr-defined]
    store = RedisTokenStore(redis=fake_redis)
    await store.save(user_id=str(user.id), jti=jti, ttl_seconds=30 * 86400)

    r = await client.post(
        "/api/v1/auth/pin-login",
        json={"refresh_token": refresh_token, "pin": "9999"},
    )
    assert r.status_code == 401, r.text
    assert r.json()["error"]["code"] == "INVALID_PIN"


# ---------------------------------------------------------------------------
# 3. Garbage refresh token → 401
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pin_login_invalid_refresh_token_returns_401(client):
    r = await client.post(
        "/api/v1/auth/pin-login",
        json={"refresh_token": "not-a-jwt", "pin": "1234"},
    )
    assert r.status_code == 401, r.text
    assert r.json()["error"]["code"] == "INVALID_TOKEN"


# ---------------------------------------------------------------------------
# 4. Access token in refresh_token field — typ mismatch → 401
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pin_login_access_token_in_refresh_field_returns_401(
    client, db_session,
):
    """Spec: refresh_token must have typ=refresh."""
    user, _, _ = _seed_user_with_active_refresh(db_session)
    access = create_access_token(subject=str(user.id))
    r = await client.post(
        "/api/v1/auth/pin-login",
        json={"refresh_token": access, "pin": "1234"},
    )
    assert r.status_code == 401, r.text
    assert r.json()["error"]["code"] == "INVALID_TOKEN"


# ---------------------------------------------------------------------------
# 5. Replay defense — rotated refresh → 401 + revoke_all
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pin_login_revoked_refresh_returns_401_and_nukes_sessions(
    client, db_session,
):
    """Replay defense — refresh_token rotated out should fail AND wipe ALL
    sessions for the user (multi-device scenario)."""
    user, refresh_token, jti = _seed_user_with_active_refresh(db_session)
    fake_redis = client._fake_redis  # type: ignore[attr-defined]
    store = RedisTokenStore(redis=fake_redis)
    await store.save(user_id=str(user.id), jti=jti, ttl_seconds=30 * 86400)

    # Second active session (simulating a second device).
    other_jti = uuid4().hex
    await store.save(user_id=str(user.id), jti=other_jti, ttl_seconds=30 * 86400)

    # Rotate the first jti out (simulates a prior /refresh).
    await store.revoke(user_id=str(user.id), jti=jti)

    r = await client.post(
        "/api/v1/auth/pin-login",
        json={"refresh_token": refresh_token, "pin": "1234"},
    )
    assert r.status_code == 401, r.text
    assert r.json()["error"]["code"] == "INVALID_TOKEN"

    # Other session also revoked — replay defense nuked everything.
    assert await store.is_valid(user_id=str(user.id), jti=other_jti) is False


# ---------------------------------------------------------------------------
# 6. User without pin_hash → 400 PIN_NOT_SET
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pin_login_user_without_pin_returns_400(client, db_session):
    """Defense-in-depth: a user without pin_hash shouldn't have a refresh
    token in the first place, but if somehow they do, /pin-login refuses."""
    user = User(
        phone="+2348022222222",
        email="b13nopin@example.com",
        full_name="No PIN",
        password_hash=hash_password("Secret1!"),
        referral_code=f"B13N{uuid4().hex[:4].upper()}",
        kyc_level=KycLevel.tier_1,
        email_verified=True,
        is_phone_verified=True,
        pin_hash=None,
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    jti = uuid4().hex
    refresh = create_refresh_token(
        subject=str(user.id), jti=jti, expires_in=timedelta(days=30),
    )
    fake_redis = client._fake_redis  # type: ignore[attr-defined]
    store = RedisTokenStore(redis=fake_redis)
    await store.save(user_id=str(user.id), jti=jti, ttl_seconds=30 * 86400)

    r = await client.post(
        "/api/v1/auth/pin-login",
        json={"refresh_token": refresh, "pin": "1234"},
    )
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "PIN_NOT_SET"


# ---------------------------------------------------------------------------
# 7. PIN-locked user → 423
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pin_login_locked_user_returns_423(client, db_session):
    user, refresh_token, jti = _seed_user_with_active_refresh(db_session)
    fake_redis = client._fake_redis  # type: ignore[attr-defined]
    store = RedisTokenStore(redis=fake_redis)
    await store.save(user_id=str(user.id), jti=jti, ttl_seconds=30 * 86400)
    # Simulate lockout — same key as PinService._lock_key.
    await fake_redis.set(f"pin_locked:{user.id}", "1", ex=60)

    r = await client.post(
        "/api/v1/auth/pin-login",
        json={"refresh_token": refresh_token, "pin": "1234"},
    )
    assert r.status_code == 423, r.text
    assert r.json()["error"]["code"] == "PIN_LOCKED"


# ---------------------------------------------------------------------------
# 8. Inactive account → 403
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pin_login_inactive_account_returns_403(client, db_session):
    user, refresh_token, jti = _seed_user_with_active_refresh(db_session)
    fake_redis = client._fake_redis  # type: ignore[attr-defined]
    store = RedisTokenStore(redis=fake_redis)
    await store.save(user_id=str(user.id), jti=jti, ttl_seconds=30 * 86400)

    user.is_active = False
    db_session.commit()

    r = await client.post(
        "/api/v1/auth/pin-login",
        json={"refresh_token": refresh_token, "pin": "1234"},
    )
    assert r.status_code == 403, r.text
    assert r.json()["error"]["code"] == "ACCOUNT_DISABLED"
