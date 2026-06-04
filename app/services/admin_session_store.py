"""Opaque server-side admin sessions in Redis.

Admin auth deliberately does NOT use JWT: an opaque session id in an
httpOnly cookie keeps the token out of browser JS (XSS-safe) and lets us
revoke instantly by deleting one key. Sliding TTL — refreshed on every
authenticated request via ``refresh``.
"""
import json
import secrets
from datetime import UTC, datetime

from redis.asyncio import Redis


class AdminSessionStore:
    def __init__(self, *, redis: Redis, ttl_seconds: int) -> None:
        self._redis = redis
        self._ttl = ttl_seconds

    @staticmethod
    def _key(sid: str) -> str:
        return f"admin_session:{sid}"

    async def create(self, *, admin_id: str, role: str) -> str:
        sid = secrets.token_urlsafe(32)
        payload = json.dumps({
            "admin_id": admin_id,
            "role": role,
            "created_at": datetime.now(UTC).isoformat(),
        })
        await self._redis.set(self._key(sid), payload, ex=self._ttl)
        return sid

    async def get(self, sid: str) -> dict | None:
        raw = await self._redis.get(self._key(sid))
        if raw is None:
            return None
        return json.loads(raw)

    async def refresh(self, sid: str) -> None:
        await self._redis.expire(self._key(sid), self._ttl)

    async def delete(self, sid: str) -> None:
        await self._redis.delete(self._key(sid))
