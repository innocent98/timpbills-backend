"""Tests for PinService — PIN verification, JWT issuance, lockout logic."""
import pytest
from uuid import uuid4

from fakeredis.aioredis import FakeRedis
from jose import jwt

from app.core.config import settings
from app.core.security import hash_pin
from app.db.models.user import User
from app.services.pin_service import InvalidPin, PinLocked, PinNotSet, PinService


def _make_user(db, *, pin: str | None = "1357") -> User:
    """Seed a user with the given PIN (or no PIN if None) into the DB."""
    suffix = uuid4().hex[:8]
    user = User(
        email=f"pintest_{suffix}@test.co",
        phone=f"+234800{suffix[:7]}",
        full_name="Pin Test User",
        password_hash="irrelevant",
        pin_hash=hash_pin(pin) if pin is not None else None,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.mark.asyncio
async def test_verify_correct_pin_issues_short_lived_token(db_session):
    """Correct PIN → JWT with sub=user_id, scope=money-ops, TTL=300s."""
    fake_redis = FakeRedis(decode_responses=True)
    svc = PinService(db=db_session, redis=fake_redis)
    user = _make_user(db_session, pin="1357")

    token = await svc.verify_async(user_id=user.id, pin="1357")

    payload = jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])
    assert payload["sub"] == str(user.id)
    assert payload["scope"] == "money-ops"
    # exp - iat should be 300 seconds (5 minutes)
    assert payload["exp"] - payload["iat"] == 300

    await fake_redis.aclose()


@pytest.mark.asyncio
async def test_wrong_pin_raises_and_increments_attempts(db_session):
    """One wrong PIN attempt → InvalidPin raised, Redis counter == 1."""
    fake_redis = FakeRedis(decode_responses=True)
    svc = PinService(db=db_session, redis=fake_redis)
    user = _make_user(db_session, pin="1357")

    with pytest.raises(InvalidPin):
        await svc.verify_async(user_id=user.id, pin="9999")

    counter = await fake_redis.get(svc._attempts_key(user.id))
    assert int(counter) == 1

    await fake_redis.aclose()


@pytest.mark.asyncio
async def test_five_wrong_attempts_locks(db_session):
    """5 consecutive wrong PINs lock the account; even correct PIN raises PinLocked."""
    fake_redis = FakeRedis(decode_responses=True)
    svc = PinService(db=db_session, redis=fake_redis)
    user = _make_user(db_session, pin="1357")

    for _ in range(5):
        with pytest.raises(InvalidPin):
            await svc.verify_async(user_id=user.id, pin="9999")

    # Now even the correct PIN should be rejected
    with pytest.raises(PinLocked):
        await svc.verify_async(user_id=user.id, pin="1357")

    await fake_redis.aclose()


@pytest.mark.asyncio
async def test_pin_not_set(db_session):
    """User with pin_hash=None raises PinNotSet."""
    fake_redis = FakeRedis(decode_responses=True)
    svc = PinService(db=db_session, redis=fake_redis)
    user = _make_user(db_session, pin=None)

    with pytest.raises(PinNotSet):
        await svc.verify_async(user_id=user.id, pin="1357")

    await fake_redis.aclose()


@pytest.mark.asyncio
async def test_correct_pin_clears_attempts(db_session):
    """One failure then success → Redis counter key is deleted."""
    fake_redis = FakeRedis(decode_responses=True)
    svc = PinService(db=db_session, redis=fake_redis)
    user = _make_user(db_session, pin="1357")

    # One wrong attempt
    with pytest.raises(InvalidPin):
        await svc.verify_async(user_id=user.id, pin="9999")

    # Correct PIN clears counter
    await svc.verify_async(user_id=user.id, pin="1357")

    counter = await fake_redis.get(svc._attempts_key(user.id))
    assert counter is None

    await fake_redis.aclose()
