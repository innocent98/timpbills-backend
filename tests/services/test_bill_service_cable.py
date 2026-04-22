"""BillService.validate_smartcard — Sprint 4 B6.

Structurally mirrors the B4 validate_meter suite — smartcard validation
is a pure pass-through wrapped in a 5-minute per-user Redis cache. No
``Transaction`` row, no wallet debit, so these tests assert caching
behavior, request-id prefix, and error / Redis-outage propagation only.

Covers:
 * cache miss → provider called with TMP-SCV prefix → SmartcardValidation
   returned → cache populated under the expected key.
 * cache hit on repeat call → provider NOT called twice.
 * per-user cache isolation — same smartcard on two users hits the
   provider twice (defence-in-depth on leaking subscriber identity).
 * permanent failure (``will_reject_smartcard``) → ProviderPermanentFailure
   re-raised, no cache populated.
 * Redis-down degradation: both .get and .set raise RedisError; the
   provider is still called, no crash, a SmartcardValidation is returned.

Lives in its own file (not appended to the electricity suite) because
that file is already 570+ LOC and B7 + B8 will add substantial cable
tests; splitting now keeps us under the 800-LOC-per-file guideline.
"""
import uuid
from decimal import Decimal

import pytest
from fakeredis.aioredis import FakeRedis
from redis.exceptions import RedisError

from app.integrations.vtpass.base import ProviderPermanentFailure
from app.integrations.vtpass.fake import FakeVTPassClient
from app.integrations.vtpass.schemas import SmartcardValidation
from app.services.bill_service import BillService
from app.services.transaction_service import TransactionService
from app.services.wallet_service import WalletService


class _CountingProvider:
    """Counting proxy around FakeVTPassClient for assertions.

    Mirrors the B4 electricity test's proxy pattern — we keep the fake
    itself free of test-observation state, so only the methods we care
    about (here: ``validate_smartcard``) are forwarded with a call
    counter. Everything else is delegated via ``__getattr__`` so the
    Protocol remains structurally satisfied if another path is
    accidentally exercised.
    """

    def __init__(self, inner: FakeVTPassClient) -> None:
        self._inner = inner
        self.validate_calls: int = 0
        self.last_request_id: str | None = None

    async def validate_smartcard(self, **kw) -> SmartcardValidation:
        self.validate_calls += 1
        self.last_request_id = kw.get("request_id")
        return await self._inner.validate_smartcard(**kw)

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


# ── 1. Happy path: cache miss → provider → cache populated ───────────────


@pytest.mark.asyncio
async def test_validate_smartcard_happy_path_caches_result(db_session):
    """First call hits provider with a TMP-SCV-prefixed request_id,
    returns a SmartcardValidation, and the result is cached under the
    expected per-user key."""
    redis = FakeRedis(decode_responses=True)
    provider = _CountingProvider(FakeVTPassClient())
    svc = _make_svc(db_session, provider=provider, redis=redis)
    user_id = uuid.uuid4()

    result = await svc.validate_smartcard(
        user_id=user_id,
        service_id="dstv",
        smartcard_number="1234567890",
    )

    assert isinstance(result, SmartcardValidation)
    assert result.service_id == "dstv"
    assert result.smartcard_number == "1234567890"
    # Fake seeds an active subscriber with the default "Fake Compact Plan".
    assert result.customer_name == "FAKE SUBSCRIBER 7890"
    assert result.current_plan_name == "Fake Compact Plan"
    assert result.status == "active"
    assert result.renewal_amount_ngn == Decimal("5000.00")
    assert provider.validate_calls == 1

    # Request-id prefix is TMP-SCV so log greps distinguish smartcard
    # validation refs from meter validation (TMP-MV) and real tx refs (TMP).
    assert provider.last_request_id is not None
    assert provider.last_request_id.startswith("TMP-SCV-")

    # Cache was populated under the :smartcard: key (NOT :meter:).
    key = f"bill_validate:smartcard:{user_id}:dstv:1234567890"
    cached = await redis.get(key)
    assert cached is not None
    assert SmartcardValidation.model_validate_json(cached) == result

    await redis.aclose()


# ── 2. Cache hit short-circuits the provider ─────────────────────────────


@pytest.mark.asyncio
async def test_validate_smartcard_cache_hit_skips_provider(db_session):
    """Second call with same (user, service, smartcard) must NOT hit
    provider — the cached entry answers the lookup."""
    redis = FakeRedis(decode_responses=True)
    provider = _CountingProvider(FakeVTPassClient())
    svc = _make_svc(db_session, provider=provider, redis=redis)
    user_id = uuid.uuid4()

    first = await svc.validate_smartcard(
        user_id=user_id,
        service_id="dstv",
        smartcard_number="1234567890",
    )
    second = await svc.validate_smartcard(
        user_id=user_id,
        service_id="dstv",
        smartcard_number="1234567890",
    )

    assert first == second
    # Provider hit once on the cold call; cache owned the second lookup.
    assert provider.validate_calls == 1

    await redis.aclose()


# ── 3. Cache is scoped per-user ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_validate_smartcard_cache_is_per_user(db_session):
    """Same smartcard, different users → provider is hit twice. Defence
    in depth: we don't let one user's cached customer_name / plan details
    answer another user's lookup (the SmartcardValidation carries PII)."""
    redis = FakeRedis(decode_responses=True)
    provider = _CountingProvider(FakeVTPassClient())
    svc = _make_svc(db_session, provider=provider, redis=redis)
    user_a = uuid.uuid4()
    user_b = uuid.uuid4()

    await svc.validate_smartcard(
        user_id=user_a,
        service_id="dstv",
        smartcard_number="1234567890",
    )
    await svc.validate_smartcard(
        user_id=user_b,
        service_id="dstv",
        smartcard_number="1234567890",
    )

    assert provider.validate_calls == 2

    await redis.aclose()


# ── 4. Permanent failure is re-raised AND not cached ────────────────────


@pytest.mark.asyncio
async def test_validate_smartcard_permanent_failure_not_cached(db_session):
    """ProviderPermanentFailure (invalid smartcard) bubbles up; cache
    stays empty so the user can retry immediately after fixing a typo.

    Note: inactive smartcards (status="inactive") are a different path —
    those are successful validations cached exactly like active ones;
    only non-000 responses raise. The B3 client-layer tests already pin
    that behavior; here we only need to verify the no-cache-on-raise
    policy at the BillService layer."""
    redis = FakeRedis(decode_responses=True)
    inner = FakeVTPassClient()
    inner.will_reject_smartcard("dstv", "9999999999")
    provider = _CountingProvider(inner)
    svc = _make_svc(db_session, provider=provider, redis=redis)
    user_id = uuid.uuid4()

    with pytest.raises(ProviderPermanentFailure):
        await svc.validate_smartcard(
            user_id=user_id,
            service_id="dstv",
            smartcard_number="9999999999",
        )

    # Cache was NOT populated — key should be absent.
    key = f"bill_validate:smartcard:{user_id}:dstv:9999999999"
    assert await redis.get(key) is None

    # And a retry still hits the provider (not served from a cached entry).
    with pytest.raises(ProviderPermanentFailure):
        await svc.validate_smartcard(
            user_id=user_id,
            service_id="dstv",
            smartcard_number="9999999999",
        )
    assert provider.validate_calls == 2

    await redis.aclose()


# ── 5. Redis-down graceful degradation ───────────────────────────────────


@pytest.mark.asyncio
async def test_validate_smartcard_degrades_gracefully_when_redis_is_down(
    db_session,
):
    """Redis outage must NOT block validation. Both cache read and cache
    write raise RedisError; the provider is still called, a
    SmartcardValidation is still returned, and every call goes to the
    provider (no caching happens)."""

    class _BrokenRedis:
        async def get(self, key):
            raise RedisError("simulated redis outage")

        async def set(self, key, value, ex=None):
            raise RedisError("simulated redis outage")

    broken = _BrokenRedis()
    fake = FakeVTPassClient()
    provider = _CountingProvider(fake)
    svc = BillService(
        db=db_session,
        tx_svc=TransactionService(db=db_session),
        wallet_svc=WalletService(db=db_session),
        provider=provider,
        redis=broken,
    )

    # First call: cache read fails, provider called, cache write also fails.
    result = await svc.validate_smartcard(
        user_id=uuid.uuid4(),
        service_id="dstv",
        smartcard_number="1111111111",
    )
    assert isinstance(result, SmartcardValidation)
    assert provider.validate_calls == 1

    # Second call: cache miss every time because writes fail.
    await svc.validate_smartcard(
        user_id=uuid.uuid4(),  # different user for clarity
        service_id="dstv",
        smartcard_number="1111111111",
    )
    assert provider.validate_calls == 2
