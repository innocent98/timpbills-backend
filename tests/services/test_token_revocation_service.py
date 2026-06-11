"""Unit tests for TokenRevocationService (Sprint 5c · Task 3.3)."""
from __future__ import annotations

import time

import pytest

from app.services.token_revocation_service import TokenRevocationService


@pytest.mark.asyncio
async def test_revoke_then_is_revoked(fake_redis):
    svc = TokenRevocationService(redis=fake_redis)
    jti = "abc123"
    assert await svc.is_revoked(jti) is False

    await svc.revoke(jti=jti, exp_unix_seconds=int(time.time()) + 600)
    assert await svc.is_revoked(jti) is True


@pytest.mark.asyncio
async def test_unknown_jti_not_revoked(fake_redis):
    svc = TokenRevocationService(redis=fake_redis)
    assert await svc.is_revoked("never-seen") is False


@pytest.mark.asyncio
async def test_revoke_floors_ttl_for_already_expired(fake_redis):
    """A token whose exp is in the past still gets a 1s tombstone so
    immediate re-check returns True. (Real-world: clock skew between
    issuer and Redis; we'd rather over-block by a few seconds than
    miss a revoke.)"""
    svc = TokenRevocationService(redis=fake_redis)
    jti = "stale-token"
    await svc.revoke(jti=jti, exp_unix_seconds=int(time.time()) - 3600)
    assert await svc.is_revoked(jti) is True


@pytest.mark.asyncio
async def test_revoke_writes_redis_key_with_ttl(fake_redis):
    svc = TokenRevocationService(redis=fake_redis)
    jti = "ttl-check"
    exp = int(time.time()) + 60
    await svc.revoke(jti=jti, exp_unix_seconds=exp)

    ttl = await fake_redis.ttl(f"revoked:jwt:{jti}")
    assert 55 <= ttl <= 60
