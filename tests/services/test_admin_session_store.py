import pytest
from fakeredis.aioredis import FakeRedis

from app.services.admin_session_store import AdminSessionStore


@pytest.mark.asyncio
async def test_create_get_refresh_delete():
    redis = FakeRedis(decode_responses=True)
    store = AdminSessionStore(redis=redis, ttl_seconds=100)

    sid = await store.create(admin_id="abc", role="superadmin")
    assert isinstance(sid, str) and len(sid) > 20

    data = await store.get(sid)
    assert data["admin_id"] == "abc"
    assert data["role"] == "superadmin"

    # refresh extends TTL (still present)
    await store.refresh(sid)
    assert await store.get(sid) is not None

    await store.delete(sid)
    assert await store.get(sid) is None
    await redis.aclose()


@pytest.mark.asyncio
async def test_get_unknown_returns_none():
    redis = FakeRedis(decode_responses=True)
    store = AdminSessionStore(redis=redis, ttl_seconds=100)
    assert await store.get("nope") is None
    await redis.aclose()
