import pytest

from app.services.idempotency_service import IdempotencyConflict, IdempotencyService


@pytest.mark.asyncio
async def test_first_call_stores_and_returns_none(fake_redis):
    svc = IdempotencyService(redis=fake_redis)
    existing = await svc.lookup(user_id="u1", key="k1", request_hash="h1")
    assert existing is None

    await svc.store(
        user_id="u1", key="k1", request_hash="h1",
        response_status=200, response_body={"ok": True},
    )

    cached = await svc.lookup(user_id="u1", key="k1", request_hash="h1")
    assert cached == (200, {"ok": True})


@pytest.mark.asyncio
async def test_same_key_different_body_raises_conflict(fake_redis):
    svc = IdempotencyService(redis=fake_redis)
    await svc.store(
        user_id="u1", key="k1", request_hash="hA",
        response_status=200, response_body={"ok": True},
    )
    with pytest.raises(IdempotencyConflict):
        await svc.lookup(user_id="u1", key="k1", request_hash="hB")


@pytest.mark.asyncio
async def test_hash_body_is_deterministic():
    h1 = IdempotencyService.hash_body(
        user_id="u1", endpoint="/x", body={"a": 1, "b": 2}
    )
    h2 = IdempotencyService.hash_body(
        user_id="u1", endpoint="/x", body={"b": 2, "a": 1}
    )
    assert h1 == h2


@pytest.mark.asyncio
async def test_hash_body_differs_by_endpoint():
    a = IdempotencyService.hash_body(user_id="u", endpoint="/a", body={"x": 1})
    b = IdempotencyService.hash_body(user_id="u", endpoint="/b", body={"x": 1})
    assert a != b
