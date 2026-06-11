"""PIN service — verifies user's 4-digit PIN and issues a short-lived
"money-ops" JWT used as X-Pin-Token on every money-moving endpoint.

Lockout: 5 consecutive wrong attempts locks the user for 30 minutes.
Counters live in Redis with TTL so they auto-expire.

B13 (Sprint 5d): adds ``pin_login`` for cold-start authentication —
mobile boots without a fresh access token, asks for the PIN, and trades
the persisted refresh + PIN for a fresh token pair. Reuses the same
lockout machinery as ``verify_async`` so brute-force attempts against
either entry point share the counter and 30-minute freeze.
"""
from datetime import UTC, timedelta
from uuid import UUID, uuid4

from jose import JWTError
from redis.asyncio import Redis
from sqlalchemy.orm import Session

from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_pin_async,
    pin_needs_rehash,
    verify_pin_async,
)
from app.db.models.user import User
from app.services.token_store import TokenStore

PIN_TOKEN_TTL = timedelta(minutes=5)
MAX_ATTEMPTS = 5
LOCKOUT_TTL_SECONDS = 30 * 60
REFRESH_TOKEN_TTL_DAYS = 30


class InvalidPin(Exception):
    pass


class PinLocked(Exception):
    pass


class PinNotSet(Exception):
    pass


class InvalidPinLoginToken(Exception):
    """Refresh token decode, type, or replay check failed."""


class UserNotFound(Exception):
    """User row for the refresh_token's sub doesn't exist."""


class AccountDisabled(Exception):
    """User row exists but is_active is False."""


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

    async def _register_failed_pin(self, user_id: UUID) -> None:
        """Increment the wrong-PIN counter and freeze on threshold. Shared
        between ``verify_async`` (warm-session money-ops) and ``pin_login``
        (cold-start) so brute force against either path trips the same lock."""
        attempts = await self._redis.incr(self._attempts_key(user_id))
        if attempts == 1:
            await self._redis.expire(
                self._attempts_key(user_id), LOCKOUT_TTL_SECONDS,
            )
        if attempts >= MAX_ATTEMPTS:
            await self._redis.set(
                self._lock_key(user_id), "1", ex=LOCKOUT_TTL_SECONDS,
            )

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
            await self._register_failed_pin(user_id)
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

    async def pin_login(
        self,
        *,
        refresh_token: str,
        pin: str,
        token_store: TokenStore,
    ) -> tuple[str, str]:
        """Cold-start PIN login: validate refresh_token + PIN → issue a
        fresh access + refresh token pair (rotates the refresh).

        Replay defense: a refresh_token whose jti is not in the
        RedisTokenStore is treated as a replay attempt and revokes ALL
        of the user's sessions before raising InvalidPinLoginToken.

        Raises:
            InvalidPinLoginToken — decode failure, wrong typ, missing
                claims, replay, or stale-iat (tokens_revoked_at).
            UserNotFound — sub does not resolve to a row.
            AccountDisabled — user.is_active is False.
            PinNotSet — defensive: should never trip with a valid refresh,
                but if pin_hash is null we refuse cold-start auth.
            PinLocked — too many recent wrong attempts.
            InvalidPin — PIN does not match.

        Returns: (access_token, refresh_token) on success.
        """
        # 1. Decode refresh_token.
        try:
            payload = decode_token(refresh_token)
        except JWTError:
            raise InvalidPinLoginToken()
        if payload.get("typ") != "refresh":
            raise InvalidPinLoginToken()
        user_id = payload.get("sub")
        jti = payload.get("jti")
        if not user_id or not jti:
            raise InvalidPinLoginToken()

        # 2. Replay defense — the token_store must say this jti is still
        # active. If not, this is either a stolen-and-already-rotated
        # token or a logout artefact. Either way, nuke every session.
        if not await token_store.is_valid(user_id=user_id, jti=jti):
            await token_store.revoke_all(user_id=user_id)
            raise InvalidPinLoginToken()

        # 3. Load user.
        try:
            user_uuid = UUID(user_id)
        except (TypeError, ValueError):
            raise InvalidPinLoginToken()
        user = self._db.query(User).filter(User.id == user_uuid).first()
        if user is None:
            raise UserNotFound()
        if user.is_active is False:
            raise AccountDisabled()

        # 4. tokens_revoked_at check — same gate get_current_user enforces
        # on the access-token side. A refresh token whose iat predates the
        # "log me out everywhere" stamp must not be honoured.
        if user.tokens_revoked_at is not None:
            iat = payload.get("iat")
            if iat is not None:
                revoked = user.tokens_revoked_at
                if revoked.tzinfo is None:
                    revoked = revoked.replace(tzinfo=UTC)
                if int(iat) <= int(revoked.timestamp()):
                    raise InvalidPinLoginToken()

        # 5. Lockout — checked AFTER the refresh validation so a stolen
        # refresh on a locked account doesn't leak lockout state (the
        # caller already failed at the token layer).
        if await self._is_locked(user_uuid):
            raise PinLocked()

        # 6. PIN must be set. Defensive: a user without a pin_hash
        # shouldn't have been issued a refresh token in the first place
        # (the post-verify flow requires PIN setup), but if somehow they
        # do, /pin-login refuses rather than minting tokens.
        if user.pin_hash is None:
            raise PinNotSet()

        # 7. Verify PIN with attempts counter / lockout.
        if not await verify_pin_async(pin, user.pin_hash):
            await self._register_failed_pin(user_uuid)
            raise InvalidPin()

        await self._redis.delete(self._attempts_key(user_uuid))

        # 8. Transparent argon2 rehash on legacy bcrypt PINs.
        if pin_needs_rehash(user.pin_hash):
            user.pin_hash = await hash_pin_async(pin)
            self._db.commit()

        # 9. Rotate refresh — drop the consumed jti, mint + persist a new
        # pair. Same shape as /auth/refresh so mobile can reuse the
        # existing token-storage path.
        await token_store.revoke(user_id=user_id, jti=jti)
        new_jti = uuid4().hex
        new_access = create_access_token(subject=user_id)
        new_refresh = create_refresh_token(
            subject=user_id,
            jti=new_jti,
            expires_in=timedelta(days=REFRESH_TOKEN_TTL_DAYS),
        )
        await token_store.save(
            user_id=user_id,
            jti=new_jti,
            ttl_seconds=REFRESH_TOKEN_TTL_DAYS * 86400,
        )
        return new_access, new_refresh
