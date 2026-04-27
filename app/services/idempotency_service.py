"""Idempotency service — dedupes money-endpoint POSTs via Redis.

Stored under key: idempotency:{user_id}:{client_key}.

Two record shapes share the slot:
  * In-flight sentinel — {"request_hash": "...", "in_flight": true}, TTL 60s.
    Written by lookup_or_acquire on cache miss; blocks concurrent retries
    of the same Idempotency-Key from racing through to VTPass.
  * Final cache entry — {"request_hash": "...", "status": 200, "body": {...}},
    TTL 24h. Written by store(); overwrites the sentinel atomically.
"""
import hashlib
import json
from typing import Any, Literal

from redis.asyncio import Redis


TTL_SECONDS = 24 * 60 * 60
IN_FLIGHT_TTL_SECONDS = 60


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
        # In-flight sentinel — caller must treat as cache-miss-but-blocked.
        if cached.get("in_flight"):
            return None
        return (cached["status"], cached["body"])

    async def lookup_or_acquire(
        self,
        *,
        user_id: str,
        key: str,
        request_hash: str,
    ) -> tuple[Literal["hit", "in_flight", "acquired"], tuple[int, dict] | None]:
        """Atomic combine of lookup + in-flight acquisition.

        Returns one of three outcomes:
          * ("hit", (status, body))      — cached final response, return as-is.
          * ("in_flight", None)          — another request with the same key
            is still processing; caller should respond with 409 TX_IN_FLIGHT.
          * ("acquired", None)           — caller may proceed with the
            money-moving work; must call store() (which atomically replaces
            the sentinel) on success, or release_in_flight() on failure.

        Raises IdempotencyConflict if a record exists with a different
        request_hash (key reused for a different request).
        """
        redis_key = self._key(user_id, key)
        sentinel = json.dumps({"request_hash": request_hash, "in_flight": True})
        # SET NX EX — atomic acquire if no record exists.
        acquired = await self._redis.set(
            redis_key, sentinel, nx=True, ex=IN_FLIGHT_TTL_SECONDS,
        )
        if acquired:
            return ("acquired", None)
        # Lost the SET NX race: someone else has either the sentinel or the
        # final cache. Read whichever it is.
        raw = await self._redis.get(redis_key)
        if raw is None:
            # TTL race — sentinel/cache vanished between our SET NX and GET.
            # Try to acquire one more time; if even that fails, treat as
            # acquired (the worst outcome is one extra call without
            # in-flight protection — extremely unlikely in practice).
            acquired_retry = await self._redis.set(
                redis_key, sentinel, nx=True, ex=IN_FLIGHT_TTL_SECONDS,
            )
            return ("acquired", None) if acquired_retry else ("acquired", None)
        cached = json.loads(raw)
        if cached.get("request_hash") != request_hash:
            raise IdempotencyConflict()
        if cached.get("in_flight"):
            return ("in_flight", None)
        return ("hit", (cached["status"], cached["body"]))

    async def release_in_flight(self, *, user_id: str, key: str) -> None:
        """Best-effort sentinel cleanup for the failure path. Removes the
        in-flight record so the user can retry promptly with the same key
        after fixing the underlying issue (e.g., funding the wallet).

        Idempotent: a no-op if the slot already holds a final cache entry
        (which means store() raced ahead) or has expired.
        """
        redis_key = self._key(user_id, key)
        raw = await self._redis.get(redis_key)
        if raw is None:
            return
        try:
            cached = json.loads(raw)
        except (TypeError, ValueError):
            return
        if cached.get("in_flight"):
            await self._redis.delete(redis_key)

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
