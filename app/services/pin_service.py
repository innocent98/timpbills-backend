"""PIN service — verifies user's 4-digit PIN and issues a short-lived
"money-ops" JWT used as X-Pin-Token on every money-moving endpoint.

Lockout: 5 consecutive wrong attempts locks the user for 30 minutes.
Counters live in Redis with TTL so they auto-expire.
"""
from datetime import timedelta
from uuid import UUID

from redis.asyncio import Redis
from sqlalchemy.orm import Session

from app.core.security import (
    create_access_token,
    hash_pin_async,
    pin_needs_rehash,
    verify_pin_async,
)
from app.db.models.user import User

PIN_TOKEN_TTL = timedelta(minutes=5)
MAX_ATTEMPTS = 5
LOCKOUT_TTL_SECONDS = 30 * 60


class InvalidPin(Exception):
    pass


class PinLocked(Exception):
    pass


class PinNotSet(Exception):
    pass


class PinService:
    def __init__(self, *, db: Session, redis: Redis) -> None:
        self._db = db
        self._redis = redis

    def _attempts_key(self, user_id: UUID) -> str:
        return f"pin_attempts:{user_id}"

    def _lock_key(self, user_id: UUID) -> str:
        return f"pin_locked:{user_id}"

    async def _is_locked(self, user_id: UUID) -> bool:
        return bool(await self._redis.exists(self._lock_key(user_id)))

    async def verify_async(self, *, user_id: UUID, pin: str) -> str:
        """Verify the PIN and return the pin_token JWT.

        Raises InvalidPin, PinLocked, or PinNotSet.
        """
        if await self._is_locked(user_id):
            raise PinLocked()

        user = self._db.query(User).filter(User.id == user_id).first()
        if user is None or user.pin_hash is None:
            raise PinNotSet()

        if not await verify_pin_async(pin, user.pin_hash):
            attempts = await self._redis.incr(self._attempts_key(user_id))
            if attempts == 1:
                await self._redis.expire(
                    self._attempts_key(user_id), LOCKOUT_TTL_SECONDS
                )
            if attempts >= MAX_ATTEMPTS:
                await self._redis.set(
                    self._lock_key(user_id), "1", ex=LOCKOUT_TTL_SECONDS
                )
            raise InvalidPin()

        await self._redis.delete(self._attempts_key(user_id))

        # Transparent rehash: legacy bcrypt PIN → argon2id on next successful
        # verify. Same pattern as the login password rehash. New users set PINs
        # directly with argon2id; this branch only fires for existing users.
        if pin_needs_rehash(user.pin_hash):
            user.pin_hash = await hash_pin_async(pin)
            self._db.commit()

        token = create_access_token(
            subject=str(user_id),
            extra={"scope": "money-ops"},
            expires_in=PIN_TOKEN_TTL,
        )
        return token
