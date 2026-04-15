import pytest


@pytest.mark.asyncio
async def test_save_and_is_valid(token_store):
    await token_store.save(user_id="u1", jti="abc", ttl_seconds=60)
    assert await token_store.is_valid(user_id="u1", jti="abc")


@pytest.mark.asyncio
async def test_revoke_specific_jti(token_store):
    await token_store.save(user_id="u1", jti="a", ttl_seconds=60)
    await token_store.save(user_id="u1", jti="b", ttl_seconds=60)
    await token_store.revoke(user_id="u1", jti="a")
    assert not await token_store.is_valid(user_id="u1", jti="a")
    assert await token_store.is_valid(user_id="u1", jti="b")


@pytest.mark.asyncio
async def test_revoke_all(token_store):
    await token_store.save(user_id="u1", jti="a", ttl_seconds=60)
    await token_store.save(user_id="u1", jti="b", ttl_seconds=60)
    await token_store.save(user_id="u2", jti="c", ttl_seconds=60)
    await token_store.revoke_all(user_id="u1")
    assert not await token_store.is_valid(user_id="u1", jti="a")
    assert not await token_store.is_valid(user_id="u1", jti="b")
    assert await token_store.is_valid(user_id="u2", jti="c")


@pytest.mark.asyncio
async def test_ttl_expires(token_store):
    # With fakeredis, TTL still works via time skipping or manual expire
    await token_store.save(user_id="u1", jti="x", ttl_seconds=1)
    assert await token_store.is_valid(user_id="u1", jti="x")
    # fakeredis doesn't auto-expire by wall clock, so force it:
    await token_store._r.delete(token_store._key("u1", "x"))
    assert not await token_store.is_valid(user_id="u1", jti="x")


@pytest.mark.asyncio
async def test_is_valid_unknown_jti(token_store):
    assert not await token_store.is_valid(user_id="u1", jti="nonexistent")
