"""BillService.validate_meter — Sprint 4 B4.

Validation is a pure pass-through wrapped in a 5-minute per-user Redis
cache. No tx row, no wallet debit: these tests assert caching behavior
and error propagation, not DB state.

Covers:
 * happy path — cache miss populates the cache + returns MeterValidation
 * cache hit — second call skips the provider entirely
 * per-user caching — same meter, different user = second provider hit
 * ProviderPermanentFailure re-raises AND does not cache
 * TTL = 300s (5 min)
"""
import uuid

import pytest
from fakeredis.aioredis import FakeRedis

from app.integrations.vtpass.base import ProviderPermanentFailure
from app.integrations.vtpass.fake import FakeVTPassClient
from app.integrations.vtpass.schemas import MeterValidation
from app.services.bill_service import BillService
from app.services.transaction_service import TransactionService
from app.services.wallet_service import WalletService


class _CountingProvider:
    """Minimal counting proxy around FakeVTPassClient for assertions.

    We intentionally don't monkeypatch FakeVTPassClient — keeping the
    fake free of test-observation state means Sprint 4 B2 doesn't need
    a revisit. Only `validate_meter` is forwarded (BillService.validate_meter
    never touches any other method), so the proxy stays tiny.
    """

    def __init__(self, inner: FakeVTPassClient) -> None:
        self._inner = inner
        self.validate_calls: int = 0

    async def validate_meter(self, **kw) -> MeterValidation:
        self.validate_calls += 1
        return await self._inner.validate_meter(**kw)

    # BillService.validate_meter only hits validate_meter; forward
    # everything else so the Protocol is still structurally satisfied
    # if another path is accidentally exercised.
    def __getattr__(self, name: str):
        return getattr(self._inner, name)


def _make_svc(db_session, *, provider, redis) -> BillService:
    return BillService(
        db=db_session,
        tx_svc=TransactionService(db=db_session),
        wallet_svc=WalletService(db=db_session),
        provider=provider,
        redis=redis,
    )


# ── 1. Happy path ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_validate_meter_happy_path_caches_result(db_session):
    """First call hits provider, result is cached, MeterValidation returned."""
    redis = FakeRedis(decode_responses=True)
    provider = _CountingProvider(FakeVTPassClient())
    svc = _make_svc(db_session, provider=provider, redis=redis)
    user_id = uuid.uuid4()

    result = await svc.validate_meter(
        user_id=user_id,
        service_id="ikeja-electric",
        meter_number="1234567890123",
        meter_type="prepaid",
    )

    assert isinstance(result, MeterValidation)
    assert result.service_id == "ikeja-electric"
    assert result.meter_number == "1234567890123"
    assert result.meter_type == "prepaid"
    assert result.customer_name == "FAKE CUSTOMER 0123"
    assert provider.validate_calls == 1

    # Cache was populated under the expected key.
    key = f"bill_validate:meter:{user_id}:ikeja-electric:1234567890123"
    cached = await redis.get(key)
    assert cached is not None
    assert MeterValidation.model_validate_json(cached) == result

    await redis.aclose()


# ── 2. Cache hit short-circuits the provider ─────────────────────────────


@pytest.mark.asyncio
async def test_validate_meter_cache_hit_skips_provider(db_session):
    """Second call with same (user, service, meter) must NOT hit provider."""
    redis = FakeRedis(decode_responses=True)
    provider = _CountingProvider(FakeVTPassClient())
    svc = _make_svc(db_session, provider=provider, redis=redis)
    user_id = uuid.uuid4()

    first = await svc.validate_meter(
        user_id=user_id,
        service_id="ikeja-electric",
        meter_number="1234567890123",
        meter_type="prepaid",
    )
    second = await svc.validate_meter(
        user_id=user_id,
        service_id="ikeja-electric",
        meter_number="1234567890123",
        meter_type="prepaid",
    )

    assert first == second
    # Provider hit once on the cold call; cache owned the second lookup.
    assert provider.validate_calls == 1

    await redis.aclose()


# ── 3. Cache is scoped per-user ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_validate_meter_cache_is_per_user(db_session):
    """Same meter, different users → provider is hit twice (defence in depth:
    we don't let one user's cached customer_name leak into another's lookup)."""
    redis = FakeRedis(decode_responses=True)
    provider = _CountingProvider(FakeVTPassClient())
    svc = _make_svc(db_session, provider=provider, redis=redis)
    user_a = uuid.uuid4()
    user_b = uuid.uuid4()

    await svc.validate_meter(
        user_id=user_a,
        service_id="ikeja-electric",
        meter_number="1234567890123",
        meter_type="prepaid",
    )
    await svc.validate_meter(
        user_id=user_b,
        service_id="ikeja-electric",
        meter_number="1234567890123",
        meter_type="prepaid",
    )

    assert provider.validate_calls == 2

    await redis.aclose()


# ── 4. Permanent failure is re-raised AND not cached ────────────────────


@pytest.mark.asyncio
async def test_validate_meter_permanent_failure_not_cached(db_session):
    """ProviderPermanentFailure bubbles up; cache stays empty so the user
    can retry immediately after fixing a typo."""
    redis = FakeRedis(decode_responses=True)
    inner = FakeVTPassClient()
    inner.will_reject_meter("ikeja-electric", "9999999999999")
    provider = _CountingProvider(inner)
    svc = _make_svc(db_session, provider=provider, redis=redis)
    user_id = uuid.uuid4()

    with pytest.raises(ProviderPermanentFailure):
        await svc.validate_meter(
            user_id=user_id,
            service_id="ikeja-electric",
            meter_number="9999999999999",
            meter_type="prepaid",
        )

    # Cache was NOT populated — key should be absent.
    key = f"bill_validate:meter:{user_id}:ikeja-electric:9999999999999"
    assert await redis.get(key) is None

    # And a retry still hits the provider (not served from cache).
    with pytest.raises(ProviderPermanentFailure):
        await svc.validate_meter(
            user_id=user_id,
            service_id="ikeja-electric",
            meter_number="9999999999999",
            meter_type="prepaid",
        )
    assert provider.validate_calls == 2

    await redis.aclose()


# ── 5. TTL is 300 seconds ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_validate_meter_cache_ttl_is_300_seconds(db_session):
    """TTL on the cache entry is exactly 5 minutes."""
    redis = FakeRedis(decode_responses=True)
    provider = _CountingProvider(FakeVTPassClient())
    svc = _make_svc(db_session, provider=provider, redis=redis)
    user_id = uuid.uuid4()

    await svc.validate_meter(
        user_id=user_id,
        service_id="ikeja-electric",
        meter_number="1234567890123",
        meter_type="prepaid",
    )

    key = f"bill_validate:meter:{user_id}:ikeja-electric:1234567890123"
    ttl = await redis.ttl(key)
    # fakeredis returns the remaining TTL in whole seconds; we set 300 and
    # the call completes inside a millisecond, so the value is still 300.
    assert ttl == 300

    await redis.aclose()
