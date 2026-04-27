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


@pytest.mark.asyncio
async def test_lookup_or_acquire_first_call_acquires(fake_redis):
    svc = IdempotencyService(redis=fake_redis)
    state, cached = await svc.lookup_or_acquire(
        user_id="u1", key="k1", request_hash="h1",
    )
    assert state == "acquired"
    assert cached is None


@pytest.mark.asyncio
async def test_lookup_or_acquire_second_call_sees_in_flight(fake_redis):
    """Concurrent retry of the same Idempotency-Key while the first call
    is still processing must return ('in_flight', None) so the endpoint
    can answer 409 TX_IN_FLIGHT instead of racing through to VTPass."""
    svc = IdempotencyService(redis=fake_redis)
    # First call acquires the sentinel.
    await svc.lookup_or_acquire(user_id="u1", key="k1", request_hash="h1")
    # Second call (mid-flight) sees the sentinel.
    state, cached = await svc.lookup_or_acquire(
        user_id="u1", key="k1", request_hash="h1",
    )
    assert state == "in_flight"
    assert cached is None


@pytest.mark.asyncio
async def test_lookup_or_acquire_after_store_returns_hit(fake_redis):
    svc = IdempotencyService(redis=fake_redis)
    await svc.lookup_or_acquire(user_id="u1", key="k1", request_hash="h1")
    await svc.store(
        user_id="u1", key="k1", request_hash="h1",
        response_status=200, response_body={"ok": True},
    )
    state, cached = await svc.lookup_or_acquire(
        user_id="u1", key="k1", request_hash="h1",
    )
    assert state == "hit"
    assert cached == (200, {"ok": True})


@pytest.mark.asyncio
async def test_lookup_or_acquire_in_flight_with_different_hash_raises(fake_redis):
    svc = IdempotencyService(redis=fake_redis)
    await svc.lookup_or_acquire(user_id="u1", key="k1", request_hash="hA")
    with pytest.raises(IdempotencyConflict):
        await svc.lookup_or_acquire(
            user_id="u1", key="k1", request_hash="hB",
        )


@pytest.mark.asyncio
async def test_release_in_flight_clears_sentinel(fake_redis):
    """release_in_flight is called on the failure path so the user can
    retry promptly with the same key after fixing the underlying issue."""
    svc = IdempotencyService(redis=fake_redis)
    await svc.lookup_or_acquire(user_id="u1", key="k1", request_hash="h1")
    await svc.release_in_flight(user_id="u1", key="k1")
    # Slot is now free — next acquire wins.
    state, _ = await svc.lookup_or_acquire(
        user_id="u1", key="k1", request_hash="h1",
    )
    assert state == "acquired"


@pytest.mark.asyncio
async def test_release_in_flight_does_not_clear_final_cache(fake_redis):
    """If store() raced ahead and wrote the final cache, release_in_flight
    must NOT delete it. Otherwise a successful response could be lost."""
    svc = IdempotencyService(redis=fake_redis)
    await svc.lookup_or_acquire(user_id="u1", key="k1", request_hash="h1")
    await svc.store(
        user_id="u1", key="k1", request_hash="h1",
        response_status=200, response_body={"ok": True},
    )
    await svc.release_in_flight(user_id="u1", key="k1")
    # Cache survives.
    state, cached = await svc.lookup_or_acquire(
        user_id="u1", key="k1", request_hash="h1",
    )
    assert state == "hit"
    assert cached == (200, {"ok": True})


@pytest.mark.asyncio
async def test_lookup_treats_in_flight_as_cache_miss(fake_redis):
    """The pre-existing lookup() shape must keep working: when only an
    in-flight sentinel exists, callers using legacy lookup get None
    rather than the raw sentinel dict."""
    svc = IdempotencyService(redis=fake_redis)
    await svc.lookup_or_acquire(user_id="u1", key="k1", request_hash="h1")
    result = await svc.lookup(user_id="u1", key="k1", request_hash="h1")
    assert result is None
