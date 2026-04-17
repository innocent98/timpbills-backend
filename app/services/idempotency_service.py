"""Idempotency service — dedupes money-endpoint POSTs via Redis.

Stored under key: idempotency:{user_id}:{client_key}.
Value is JSON {"request_hash": "...", "status": 200, "body": {...}}.
TTL: 24 hours.
"""
import hashlib
import json
from typing import Any

from redis.asyncio import Redis


TTL_SECONDS = 24 * 60 * 60


class IdempotencyConflict(Exception):
    pass


class IdempotencyService:
    def __init__(self, *, redis: Redis) -> None:
        self._redis = redis

    def _key(self, user_id: str, client_key: str) -> str:
        return f"idempotency:{user_id}:{client_key}"

    @staticmethod
    def hash_body(*, user_id: str, endpoint: str, body: Any) -> str:
        raw = json.dumps(
            {"u": user_id, "e": endpoint, "b": body},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(raw).hexdigest()

    async def lookup(
        self,
        *,
        user_id: str,
        key: str,
        request_hash: str,
    ) -> tuple[int, dict] | None:
        raw = await self._redis.get(self._key(user_id, key))
        if raw is None:
            return None
        cached = json.loads(raw)
        if cached["request_hash"] != request_hash:
            raise IdempotencyConflict()
        return (cached["status"], cached["body"])

    async def store(
        self,
        *,
        user_id: str,
        key: str,
        request_hash: str,
        response_status: int,
        response_body: dict,
    ) -> None:
        payload = json.dumps(
            {"request_hash": request_hash, "status": response_status, "body": response_body}
        )
        await self._redis.set(self._key(user_id, key), payload, ex=TTL_SECONDS)
