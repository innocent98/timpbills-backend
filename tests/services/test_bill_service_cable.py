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
from app.integrations.vtpass.schemas import CablePlanList, SmartcardValidation
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
    # Fake pins the "current plan" to the first seeded dstv variation
    # (Compact at ₦15500/month) so a subsequent renew-purchase's catalog
    # lookup against BillService.purchase_cable resolves cleanly.
    assert result.customer_name == "FAKE SUBSCRIBER 7890"
    assert result.current_plan_name == "Compact"
    assert result.current_plan_code == "dstv-compact"
    assert result.status == "active"
    assert result.renewal_amount_ngn == Decimal("15500.00")
    assert provider.validate_calls == 1

    # Prefix "TMPSCV" appears after the 12-digit YYYYMMDDHHMI stamp so
    # log greps can distinguish smartcard validation refs from meter
    # validation (TMPMV) and real tx refs (TMP). Hyphens dropped in
    # refs.py to satisfy VTPass's alphanumeric-after-position-12 rule.
    assert provider.last_request_id is not None
    assert provider.last_request_id[12:].startswith("TMPSCV")

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


# ── 6-9. BillService.list_cable_plans (B7) ───────────────────────────────
#
# Thin, uncached passthrough onto the provider. "mode" is a BillService-
# layer concept (renew vs change) that the provider's list_cable_plans
# knows nothing about. Current decision: both modes return the full
# catalog — filtering to the currently-active plan for renew flows is
# an endpoint / mobile concern driven by the separately-cached
# validate_smartcard response (see B7 docstring).


@pytest.mark.asyncio
async def test_list_cable_plans_change_returns_full_catalog(db_session):
    """mode="change" → provider's full bouquet catalog unfiltered.
    The fake seeds 4 dstv plans (compact, compact-plus, premium, access)."""
    redis = FakeRedis(decode_responses=True)
    provider = FakeVTPassClient()
    svc = _make_svc(db_session, provider=provider, redis=redis)

    result = await svc.list_cable_plans(service_id="dstv", mode="change")

    assert isinstance(result, CablePlanList)
    assert result.service_id == "dstv"
    assert len(result.variations) == 4
    codes = {v.variation_code for v in result.variations}
    assert codes == {
        "dstv-compact",
        "dstv-compact-plus",
        "dstv-premium",
        "dstv-access",
    }

    await redis.aclose()


@pytest.mark.asyncio
async def test_list_cable_plans_renew_returns_full_catalog(db_session):
    """mode="renew" → ALSO returns the full catalog. Per the B7 design
    decision, filtering by the subscriber's current_plan_code is the
    endpoint / mobile layer's job (it has the validate_smartcard cache
    available); doing it here would require threading smartcard_number
    through this method and mixing concerns."""
    redis = FakeRedis(decode_responses=True)
    provider = FakeVTPassClient()
    svc = _make_svc(db_session, provider=provider, redis=redis)

    result = await svc.list_cable_plans(service_id="dstv", mode="renew")

    assert isinstance(result, CablePlanList)
    assert result.service_id == "dstv"
    # Same 4 dstv plans as the change-mode call — no filtering at this layer.
    assert len(result.variations) == 4


@pytest.mark.asyncio
async def test_list_cable_plans_unknown_service_returns_empty(db_session):
    """Unknown service_id → empty variations, no exception. Matches the
    fake's behavior (_DEFAULT_CABLE_PLANS.get(service_id, [])) and the
    real client's 'no variations' response. The endpoint layer can decide
    whether to 404 or return an empty list."""
    redis = FakeRedis(decode_responses=True)
    provider = FakeVTPassClient()
    svc = _make_svc(db_session, provider=provider, redis=redis)

    result = await svc.list_cable_plans(
        service_id="unknown-service", mode="change",
    )

    assert isinstance(result, CablePlanList)
    assert result.service_id == "unknown-service"
    assert result.variations == []

    await redis.aclose()


@pytest.mark.asyncio
async def test_list_cable_plans_invalid_mode_raises_value_error(db_session):
    """Anything other than 'renew' | 'change' is a programmer error —
    ValueError with a helpful message rather than a silent default."""
    redis = FakeRedis(decode_responses=True)
    provider = FakeVTPassClient()
    svc = _make_svc(db_session, provider=provider, redis=redis)

    with pytest.raises(ValueError, match="mode must be 'renew' or 'change'"):
        await svc.list_cable_plans(service_id="dstv", mode="invalid")

    await redis.aclose()


# ── B8: BillService.purchase_cable (renew + change) ──────────────────────
#
# Routes through the shared `_execute_bill` machinery with
# TransactionType.cable. Two modes:
#   * "renew" — reads current_plan_code + renewal_amount_ngn from the
#     SmartcardValidation cache (seeded in each test via validate_smartcard
#     or direct Redis write). Wire serviceID stays at the base slug.
#   * "change" — caller supplies variation_code; price is server-resolved
#     from the catalog (spoof prevention). Wire serviceID is
#     "{service_id}-change".
#
# Tests below use a _CableCountingProvider that captures the last-seen
# wire service_id + variation_code on purchase_cable, so we can assert the
# "-change" suffix forwarding without poking at VTPass wire internals.

from app.db.models._enums import TransactionStatus, TransactionType
from app.db.models.user import User
from app.db.models.wallet import Wallet
from app.services.bill_service import (
    CablePlanNotFound,
    CableRenewalUnavailable,
)


class _CableCountingProvider:
    """Counting proxy for the B8 purchase_cable tests.

    Forwards validate_smartcard / list_cable_plans / purchase_cable to the
    underlying FakeVTPassClient while capturing the last-seen call args
    on purchase_cable. Forwards everything else via ``__getattr__`` so the
    BillProvider Protocol is still structurally satisfied if another path
    is accidentally exercised."""

    def __init__(self, inner: FakeVTPassClient) -> None:
        self._inner = inner
        self.purchase_calls: int = 0
        self.last_purchase_kwargs: dict | None = None

    async def purchase_cable(self, **kw):
        self.purchase_calls += 1
        self.last_purchase_kwargs = dict(kw)
        # No more suffix stripping — Sprint 5 audit fix uses
        # subscription_type on the wire for renew/change, and serviceID
        # stays as the bare slug, so the fake's catalog resolves the
        # variation directly.
        return await self._inner.purchase_cable(**kw)

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


def _seed_cable_user(db, *, balance: Decimal = Decimal("60000.00")) -> User:
    user = User(
        id=uuid.uuid4(),
        email=f"cable-{uuid.uuid4().hex[:6]}@t.co",
        phone=f"+234{uuid.uuid4().int % 10**10:010d}",
        full_name="Cable Test",
        password_hash="x",
        is_active=True,
    )
    db.add(user)
    db.flush()
    db.add(Wallet(
        id=uuid.uuid4(), user_id=user.id,
        balance=balance, balance_cap=Decimal("100000.00"),
    ))
    db.commit()
    return user


def _wallet_balance_of(db, user_id) -> Decimal:
    db.expire_all()
    return db.query(Wallet).filter(Wallet.user_id == user_id).one().balance


async def _seed_smartcard_cache(
    redis,
    *,
    user_id,
    service_id: str,
    smartcard_number: str,
    current_plan_code: str,
    current_plan_name: str,
    renewal_amount_ngn: Decimal,
    status: str = "active",
    customer_name: str = "FAKE SUBSCRIBER CACHED",
) -> None:
    """Pre-populate the SmartcardValidation cache under the exact key that
    validate_smartcard (B6) writes to. Keeps the B8 renew tests free of
    dependence on the B6 method's side effects — one feature per test."""
    key = (
        f"bill_validate:smartcard:{user_id}:{service_id}:{smartcard_number}"
    )
    value = SmartcardValidation(
        service_id=service_id,
        smartcard_number=smartcard_number,
        customer_name=customer_name,
        current_plan_name=current_plan_name,
        current_plan_code=current_plan_code,
        status=status,
        renewal_amount_ngn=renewal_amount_ngn,
    )
    await redis.set(key, value.model_dump_json(), ex=300)


# ── B8-1: renew happy path ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_purchase_cable_renew_happy_path(db_session):
    """Cached SmartcardValidation drives the renewal: current_plan_code +
    renewal_amount_ngn are pulled from Redis, price is NOT the client's
    to choose, and the wire serviceID stays at the base slug ("dstv")
    rather than "-change"."""
    user = _seed_cable_user(db_session)
    redis = FakeRedis(decode_responses=True)
    provider = _CableCountingProvider(FakeVTPassClient())
    svc = _make_svc(db_session, provider=provider, redis=redis)

    await _seed_smartcard_cache(
        redis,
        user_id=user.id,
        service_id="dstv",
        smartcard_number="1234567890",
        current_plan_code="dstv-compact",
        current_plan_name="DStv Compact",
        renewal_amount_ngn=Decimal("15500.00"),
    )

    result = await svc.purchase_cable(
        user_id=user.id,
        service_id="dstv",
        smartcard_number="1234567890",
        mode="renew",
        phone="08011111111",
    )

    assert result.tx.status == TransactionStatus.success
    assert result.tx.type == TransactionType.cable
    assert result.tx.amount == Decimal("15500.00")
    # Meta fields: service_id, mode, plan_code/plan_name.
    meta = result.tx.meta
    assert meta["service_id"]       == "dstv"
    assert meta["smartcard_number"] == "1234567890"
    assert meta["mode"]             == "renew"
    assert meta["plan_code"]        == "dstv-compact"
    assert meta["plan_name"]        == "DStv Compact"

    # Wallet debited by the renewal amount.
    assert _wallet_balance_of(db_session, user.id) == Decimal("44500.00")

    # Wire assertions: provider called with base service_id, the
    # subscription_type field carrying the renew/change distinction,
    # and the cached variation_code.
    assert provider.purchase_calls == 1
    assert provider.last_purchase_kwargs is not None
    assert provider.last_purchase_kwargs["service_id"]        == "dstv"
    assert provider.last_purchase_kwargs["variation_code"]    == "dstv-compact"
    assert provider.last_purchase_kwargs["smartcard_number"]  == "1234567890"
    assert provider.last_purchase_kwargs["amount_ngn"]        == Decimal("15500.00")
    assert provider.last_purchase_kwargs["subscription_type"] == "renew"
    assert provider.last_purchase_kwargs["phone"]             == "08011111111"

    await redis.aclose()


# ── B8-2: renew with empty cache → CableRenewalUnavailable ───────────────


@pytest.mark.asyncio
async def test_purchase_cable_renew_cache_empty_raises(db_session):
    """No SmartcardValidation cached (user never validated, or the 5-min
    TTL expired) → CableRenewalUnavailable with the "Validate first"
    hint. We refuse to guess a renewal price."""
    user = _seed_cable_user(db_session)
    redis = FakeRedis(decode_responses=True)
    provider = _CableCountingProvider(FakeVTPassClient())
    svc = _make_svc(db_session, provider=provider, redis=redis)

    with pytest.raises(CableRenewalUnavailable, match="Validate smartcard first"):
        await svc.purchase_cable(
            user_id=user.id,
            service_id="dstv",
            smartcard_number="1234567890",
            mode="renew",
            phone="08011111111",
        )

    # Provider was NOT reached — we bailed before touching the wallet.
    assert provider.purchase_calls == 0
    # Wallet untouched.
    assert _wallet_balance_of(db_session, user.id) == Decimal("60000.00")

    await redis.aclose()


# ── B8-3: renew but cached plan is empty (inactive card) ─────────────────


@pytest.mark.asyncio
async def test_purchase_cable_renew_inactive_card_raises(db_session):
    """Cached validation shows an inactive/fresh smartcard (empty
    current_plan_code) → CableRenewalUnavailable with the "no active plan"
    hint. Distinguishing this from cache-miss helps the UI write a more
    helpful error ("upgrade first" vs "validate first")."""
    user = _seed_cable_user(db_session)
    redis = FakeRedis(decode_responses=True)
    provider = _CableCountingProvider(FakeVTPassClient())
    svc = _make_svc(db_session, provider=provider, redis=redis)

    await _seed_smartcard_cache(
        redis,
        user_id=user.id,
        service_id="dstv",
        smartcard_number="1234567890",
        current_plan_code="",        # fresh / inactive
        current_plan_name="",
        renewal_amount_ngn=Decimal("0.00"),
        status="inactive",
    )

    with pytest.raises(CableRenewalUnavailable, match="no active plan"):
        await svc.purchase_cable(
            user_id=user.id,
            service_id="dstv",
            smartcard_number="1234567890",
            mode="renew",
            phone="08011111111",
        )

    assert provider.purchase_calls == 0
    assert _wallet_balance_of(db_session, user.id) == Decimal("60000.00")

    await redis.aclose()


# ── B8-4: change happy path ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_purchase_cable_change_happy_path(db_session):
    """Change mode: client picks a variation, price is server-resolved
    from the catalog (NOT trusted from the client), wire serviceID is
    "dstv-change"."""
    user = _seed_cable_user(db_session)
    redis = FakeRedis(decode_responses=True)
    provider = _CableCountingProvider(FakeVTPassClient())
    svc = _make_svc(db_session, provider=provider, redis=redis)

    result = await svc.purchase_cable(
        user_id=user.id,
        service_id="dstv",
        smartcard_number="1234567890",
        mode="change",
        phone="08011111111",
        variation_code="dstv-premium",
    )

    # Fake's seeded dstv-premium price is ₦44500.
    assert result.tx.status == TransactionStatus.success
    assert result.tx.type == TransactionType.cable
    assert result.tx.amount == Decimal("44500.00")

    meta = result.tx.meta
    assert meta["service_id"]       == "dstv"
    assert meta["smartcard_number"] == "1234567890"
    assert meta["mode"]             == "change"
    assert meta["plan_code"]        == "dstv-premium"
    assert meta["plan_name"]        == "Premium"

    # Wallet debited by the catalog price.
    assert _wallet_balance_of(db_session, user.id) == Decimal("15500.00")

    # Wire: serviceID stays as "dstv"; subscription_type="change" is
    # the VTPass-canonical signal for bouquet switching.
    assert provider.purchase_calls == 1
    assert provider.last_purchase_kwargs is not None
    assert provider.last_purchase_kwargs["service_id"]        == "dstv"
    assert provider.last_purchase_kwargs["variation_code"]    == "dstv-premium"
    assert provider.last_purchase_kwargs["amount_ngn"]        == Decimal("44500.00")
    assert provider.last_purchase_kwargs["subscription_type"] == "change"

    await redis.aclose()


# ── B8-5: change with unknown variation_code → CablePlanNotFound ─────────


@pytest.mark.asyncio
async def test_purchase_cable_change_unknown_variation_raises(db_session):
    """Catalog lookup misses → CablePlanNotFound BEFORE any wallet debit.
    Mirrors DataPlanNotFound behavior on the purchase_data path."""
    user = _seed_cable_user(db_session)
    redis = FakeRedis(decode_responses=True)
    provider = _CableCountingProvider(FakeVTPassClient())
    svc = _make_svc(db_session, provider=provider, redis=redis)

    with pytest.raises(CablePlanNotFound):
        await svc.purchase_cable(
            user_id=user.id,
            service_id="dstv",
            smartcard_number="1234567890",
            mode="change",
            phone="08011111111",
            variation_code="dstv-does-not-exist",
        )

    # Provider's purchase_cable was NOT called (catalog lookup went through
    # list_cable_plans, but not purchase_cable).
    assert provider.purchase_calls == 0
    # Wallet untouched — failure was pre-debit.
    assert _wallet_balance_of(db_session, user.id) == Decimal("60000.00")

    await redis.aclose()


# ── B8-6: change without variation_code → ValueError ─────────────────────


@pytest.mark.asyncio
async def test_purchase_cable_change_missing_variation_raises_value_error(
    db_session,
):
    """mode="change" but caller omitted variation_code → ValueError. This
    is a programmer error at the endpoint layer (the route handler should
    reject it earlier); surfacing as ValueError rather than silently
    falling back to renew keeps the behavior obvious."""
    user = _seed_cable_user(db_session)
    redis = FakeRedis(decode_responses=True)
    provider = _CableCountingProvider(FakeVTPassClient())
    svc = _make_svc(db_session, provider=provider, redis=redis)

    with pytest.raises(ValueError, match="variation_code required"):
        await svc.purchase_cable(
            user_id=user.id,
            service_id="dstv",
            smartcard_number="1234567890",
            mode="change",
            phone="08011111111",
            variation_code=None,
        )

    assert provider.purchase_calls == 0
    assert _wallet_balance_of(db_session, user.id) == Decimal("60000.00")

    await redis.aclose()


# ── B8-7: invalid mode → ValueError ──────────────────────────────────────


@pytest.mark.asyncio
async def test_purchase_cable_invalid_mode_raises_value_error(db_session):
    """Mode validation runs FIRST (before any cache/catalog lookup) so a
    typo fails fast with a clear message rather than surfacing as "cache
    empty" or "plan not found"."""
    user = _seed_cable_user(db_session)
    redis = FakeRedis(decode_responses=True)
    provider = _CableCountingProvider(FakeVTPassClient())
    svc = _make_svc(db_session, provider=provider, redis=redis)

    with pytest.raises(ValueError, match="mode must be"):
        await svc.purchase_cable(
            user_id=user.id,
            service_id="dstv",
            smartcard_number="1234567890",
            mode="upgrade",   # not a real mode
            phone="08011111111",
        )

    assert provider.purchase_calls == 0
    assert _wallet_balance_of(db_session, user.id) == Decimal("60000.00")

    await redis.aclose()
