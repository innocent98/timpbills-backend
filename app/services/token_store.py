"""Redis-backed refresh token store.

Stores one key per issued refresh token JTI:
    refresh:{user_id}:{jti}  →  "1"   (TTL = token lifetime)

Operations:
    save(user_id, jti, ttl) — called on login / verify_otp / refresh
    is_valid(user_id, jti)  — called during /refresh to check liveness
    revoke(user_id, jti)    — called when refresh succeeds (rotate old jti out)
    revoke_all(user_id)     — called on password reset (logout all devices)
"""
from typing import Protocol

from redis.asyncio import Redis


class TokenStore(Protocol):
    async def save(self, *, user_id: str, jti: str, ttl_seconds: int) -> None: ...
    async def is_valid(self, *, user_id: str, jti: str) -> bool: ...
    async def revoke(self, *, user_id: str, jti: str) -> None: ...
    async def revoke_all(self, *, user_id: str) -> None: ...


class NullTokenStore:
    """No-op token store — skips all Redis calls.

    Used as the default when no token_store is injected (e.g., existing unit tests
    that do not need revocation semantics).  All tokens are considered valid.
    """

    async def save(self, *, user_id: str, jti: str, ttl_seconds: int) -> None:
        pass

    async def is_valid(self, *, user_id: str, jti: str) -> bool:
        return True

    async def revoke(self, *, user_id: str, jti: str) -> None:
        pass

    async def revoke_all(self, *, user_id: str) -> None:
        pass


class RedisTokenStore:
    def __init__(self, *, redis: Redis) -> None:
        self._r = redis

    def _key(self, user_id: str, jti: str) -> str:
        return f"refresh:{user_id}:{jti}"

    async def save(self, *, user_id: str, jti: str, ttl_seconds: int) -> None:
        await self._r.set(self._key(user_id, jti), "1", ex=ttl_seconds)

    async def revoke(self, *, user_id: str, jti: str) -> None:
        await self._r.delete(self._key(user_id, jti))

    async def revoke_all(self, *, user_id: str) -> None:
        # SCAN to avoid blocking on large keyspaces
        async for key in self._r.scan_iter(match=f"refresh:{user_id}:*"):
            await self._r.delete(key)

    async def is_valid(self, *, user_id: str, jti: str) -> bool:
        return bool(await self._r.exists(self._key(user_id, jti)))
