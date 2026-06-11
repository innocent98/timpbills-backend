"""TokenRevocationService — JWT-jti blocklist (Sprint 5c · Task 3.3).

Stores one Redis key per revoked access-token jti, with TTL equal to the
token's remaining lifetime. After the TTL the key auto-expires — no
janitor needed and the keyspace stays bounded by the access-token TTL
(20 minutes), not the full refresh window.

    revoked:jwt:{jti}  →  "<exp_unix>"   (TTL = exp − now())

Operations are async because the project's Redis client is
``redis.asyncio.Redis``. ``get_current_user`` (which gates protected
routes) is async to match — see ``app/api/deps.py``.

Refresh-token rotation is intentionally NOT touched here: refresh tokens
go through ``RedisTokenStore.revoke``/``revoke_all`` which is the
keyspace ``AuthService.refresh`` already consults on every refresh.
"""
from __future__ import annotations

import time

from redis.asyncio import Redis


class TokenRevocationService:
    """Async JWT-jti blocklist backed by Redis."""

    def __init__(self, redis: Redis) -> None:
        self._r = redis

    @staticmethod
    def _key(jti: str) -> str:
        return f"revoked:jwt:{jti}"

    async def revoke(self, *, jti: str, exp_unix_seconds: int) -> None:
        """Add ``jti`` to the blocklist until its natural expiry.

        ``exp_unix_seconds`` is the JWT's ``exp`` claim. We derive the
        Redis TTL from (exp − now), with a floor of 1s so a token that
        just expired still gets a tombstone the next call can observe.
        """
        ttl = max(1, exp_unix_seconds - int(time.time()))
        await self._r.set(self._key(jti), str(exp_unix_seconds), ex=ttl)

    async def is_revoked(self, jti: str) -> bool:
        return await self._r.exists(self._key(jti)) == 1
