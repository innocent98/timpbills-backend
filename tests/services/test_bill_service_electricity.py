"""BillService.validate_meter + BillService.purchase_electricity —
Sprint 4 B4 + B5.

Validation (B4) is a pure pass-through wrapped in a 5-minute per-user
Redis cache. No tx row, no wallet debit: the validate_meter tests assert
caching behavior and error propagation, not DB state.

Purchase (B5) routes through the shared ``_execute_bill`` machinery with
``TransactionType.electricity``. On delivered (full or partial), the
VTPass-returned ``token`` + ``units`` must land in ``tx.meta`` via a
post-``_execute_bill`` commit — the core 6-step machinery owns the state
transition + partial_delivery meta, and the electricity-specific
token/units persistence is layered on after that.

Covers:
 * validate_meter: happy path + cache hit + per-user scoping + permanent
   failure not cached + TTL = 300s + Redis-outage degradation.
 * purchase_electricity: happy path (token + units in meta, wallet
   debited), provider-failed (refund, no token/units), provider-pending
   (processing, wallet debited, no refund), partial (shortfall refunded
   AND token/units persisted alongside partial_delivery meta),
   insufficient balance (provider not called).
"""
import uuid
from decimal import Decimal

import pytest
from fakeredis.aioredis import FakeRedis
from redis.exceptions import RedisError

from app.db.models._enums import TransactionStatus, TransactionType
from app.db.models.transaction import Transaction
from app.db.models.user import User
from app.db.models.wallet import Wallet
from app.integrations.vtpass.base import ProviderPermanentFailure
from app.integrations.vtpass.fake import FakeVTPassClient
from app.integrations.vtpass.schemas import MeterValidation
from app.services.bill_service import BillService
from app.services.transaction_service import TransactionService
from app.services.wallet_service import InsufficientBalance, WalletService


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
    # the call completes inside a millisecond, so the value is usually 300.
    # Under CI load the set → ttl round-trip can span a 1-second boundary
    # and return 299, so allow a small window rather than asserting exact.
    assert 295 <= ttl <= 300, f"TTL {ttl} outside expected 295-300 range"

    await redis.aclose()


# ── 6. Redis-down graceful degradation ───────────────────────────────────


@pytest.mark.asyncio
async def test_validate_meter_degrades_gracefully_when_redis_is_down(db_session):
    """Redis outage must NOT block validation. Provider still gets called;
    the result just isn't cached."""
    # Create a Redis mock whose .get / .set raise ConnectionError.
    class _BrokenRedis:
        async def get(self, key):
            raise RedisError("simulated redis outage")

        async def set(self, key, value, ex=None):
            raise RedisError("simulated redis outage")

    broken = _BrokenRedis()

    fake = FakeVTPassClient()
    provider = _CountingProvider(fake)
    # Construct BillService with the broken redis
    svc = BillService(
        db=db_session,
        tx_svc=TransactionService(db=db_session),
        wallet_svc=WalletService(db=db_session),
        provider=provider,
        redis=broken,
    )

    # First call: cache read fails, provider called, cache write also fails
    result = await svc.validate_meter(
        user_id=uuid.uuid4(),
        service_id="ikeja-electric",
        meter_number="1111111111111",
        meter_type="prepaid",
    )
    assert isinstance(result, MeterValidation)
    assert provider.validate_calls == 1

    # Second call: same (cache miss every time because writes fail)
    await svc.validate_meter(
        user_id=uuid.uuid4(),  # different user for clarity
        service_id="ikeja-electric",
        meter_number="1111111111111",
        meter_type="prepaid",
    )
    # Provider called TWICE — no caching happened
    assert provider.validate_calls == 2


# ── B5: purchase_electricity ─────────────────────────────────────────────
#
# Purchase tests seed a real user + wallet (validate tests don't need one
# because they never hit the DB). The shared `_execute_bill` machinery is
# exercised by the airtime/data test suite — here we focus on the bits
# specific to electricity:
#   * token + units persistence on delivered (full OR partial)
#   * phone plumbed through to the provider
#   * meter_number / meter_type / service_id present in tx.meta for the
#     receipt UI (no price lookup — electricity amount comes from the user)


def _seed_user(db, *, balance: Decimal = Decimal("10000.00")) -> User:
    user = User(
        id=uuid.uuid4(),
        email=f"elec-{uuid.uuid4().hex[:6]}@t.co",
        phone=f"+234{uuid.uuid4().int % 10**10:010d}",
        full_name="Electricity Test",
        password_hash="x",
        is_active=True,
    )
    db.add(user)
    db.flush()
    db.add(Wallet(
        id=uuid.uuid4(), user_id=user.id,
        balance=balance, balance_cap=Decimal("50000.00"),
    ))
    db.commit()
    return user


def _wallet_balance(db, user_id) -> Decimal:
    db.expire_all()
    return db.query(Wallet).filter(Wallet.user_id == user_id).one().balance


def _bill_service(db, *, fake: FakeVTPassClient) -> BillService:
    return BillService(
        db=db,
        tx_svc=TransactionService(db=db),
        wallet_svc=WalletService(db=db),
        provider=fake,
        redis=FakeRedis(decode_responses=True),
    )


# ── B5-1: happy path — delivered, token + units persisted ────────────────


@pytest.mark.asyncio
async def test_purchase_electricity_happy_path_persists_token_and_units(db_session):
    """Full delivery: tx goes to success, wallet debited by amount, and
    tx.meta includes the VTPass-returned 20-digit token + kWh units."""
    user = _seed_user(db_session)
    fake = FakeVTPassClient()  # default outcome is success
    svc = _bill_service(db_session, fake=fake)

    result = await svc.purchase_electricity(
        user_id=user.id,
        service_id="ikeja-electric",
        meter_number="1234567890123",
        meter_type="prepaid",
        phone="08012345678",
        amount_ngn=Decimal("2000.00"),
    )

    assert result.tx.status == TransactionStatus.success
    assert result.tx.type == TransactionType.electricity
    assert result.tx.amount == Decimal("2000.00")
    assert _wallet_balance(db_session, user.id) == Decimal("8000.00")

    # Request-shape meta fields (pre-debit) are present.
    meta = result.tx.meta
    assert meta["service_id"]   == "ikeja-electric"
    assert meta["meter_number"] == "1234567890123"
    assert meta["meter_type"]   == "prepaid"
    assert meta["phone"]        == "08012345678"

    # Post-delivery: token + units landed on tx.meta.
    token = meta.get("token")
    units = meta.get("units")
    assert isinstance(token, str) and len(token) == 20 and token.isdigit()
    # Units is a 2-dp decimal string (string-typed for JSON safety).
    assert isinstance(units, str)
    # Sanity: ₦2000 at ~₦40/kWh ≈ 50 kWh from the fake.
    assert Decimal(units) == Decimal("50.00")


# ── B5-2: failed — refund issued, no token/units ─────────────────────────


@pytest.mark.asyncio
async def test_purchase_electricity_failed_refunds_and_no_token(db_session):
    """Provider returns failed → tx marked failed, refund tx created,
    wallet restored to starting balance, and NO token/units in tx.meta
    (delivered guard in the post-processor skips the persistence path)."""
    user = _seed_user(db_session)

    class FailingFake(FakeVTPassClient):
        async def purchase_electricity(self, **kw):  # type: ignore[override]
            self.will_fail(kw["request_id"])
            return await super().purchase_electricity(**kw)

    svc = _bill_service(db_session, fake=FailingFake())

    result = await svc.purchase_electricity(
        user_id=user.id,
        service_id="ikeja-electric",
        meter_number="1234567890123",
        meter_type="prepaid",
        phone="08012345678",
        amount_ngn=Decimal("2000.00"),
    )

    assert result.tx.status == TransactionStatus.failed
    # Debited then refunded → net zero change on the wallet.
    assert _wallet_balance(db_session, user.id) == Decimal("10000.00")

    # Exactly one refund tx was minted for this user.
    refunds = (
        db_session.query(Transaction)
        .filter(
            Transaction.user_id == user.id,
            Transaction.type == TransactionType.refund,
        ).all()
    )
    assert len(refunds) == 1
    assert refunds[0].amount == Decimal("2000.00")

    # token/units must NOT be on the tx — failed path never reaches them.
    assert "token" not in (result.tx.meta or {})
    assert "units" not in (result.tx.meta or {})


# ── B5-3: pending — tx stays processing, wallet stays debited ────────────


@pytest.mark.asyncio
async def test_purchase_electricity_pending_keeps_tx_processing(db_session):
    """Provider returns pending → tx stays in processing (reconcile owns
    finalization), wallet stays debited (no refund), no token/units."""
    user = _seed_user(db_session)

    class PendingFake(FakeVTPassClient):
        async def purchase_electricity(self, **kw):  # type: ignore[override]
            self.will_remain_pending(kw["request_id"])
            return await super().purchase_electricity(**kw)

    svc = _bill_service(db_session, fake=PendingFake())

    result = await svc.purchase_electricity(
        user_id=user.id,
        service_id="ikeja-electric",
        meter_number="1234567890123",
        meter_type="prepaid",
        phone="08012345678",
        amount_ngn=Decimal("2000.00"),
    )

    assert result.tx.status == TransactionStatus.processing
    # Wallet debited, not yet refunded — reconcile worker finalizes later.
    assert _wallet_balance(db_session, user.id) == Decimal("8000.00")

    # No refund tx yet.
    refunds = (
        db_session.query(Transaction)
        .filter(
            Transaction.user_id == user.id,
            Transaction.type == TransactionType.refund,
        ).count()
    )
    assert refunds == 0

    # token/units absent — pending status doesn't carry them from the fake
    # and the post-processor's `delivered` guard would skip anyway.
    assert "token" not in (result.tx.meta or {})
    assert "units" not in (result.tx.meta or {})


# ── B5-4: partial — shortfall refunded AND token + units still persisted

@pytest.mark.asyncio
async def test_purchase_electricity_partial_persists_token_with_shortfall(
    db_session,
):
    """Partial delivery: BillDeliveryStatus.delivered covers BOTH full
    and partial, so the post-processor's delivered-guard fires on partial
    too. VTPass returns a token even on partial (the DisCo loaded some of
    the requested value), so we persist token + units alongside the
    shortfall/partial_delivery meta that apply_provider_result writes."""
    user = _seed_user(db_session)

    class PartialFake(FakeVTPassClient):
        async def purchase_electricity(self, **kw):  # type: ignore[override]
            # Deliver ₦1800 of the requested ₦2000.
            self.will_partial(
                kw["request_id"], delivered_ngn=Decimal("1800.00"),
            )
            return await super().purchase_electricity(**kw)

    svc = _bill_service(db_session, fake=PartialFake())

    result = await svc.purchase_electricity(
        user_id=user.id,
        service_id="ikeja-electric",
        meter_number="1234567890123",
        meter_type="prepaid",
        phone="08012345678",
        amount_ngn=Decimal("2000.00"),
    )

    # Still a success (delivered covers partial).
    assert result.tx.status == TransactionStatus.success

    # Wallet: debited 2000, refunded 200 shortfall → net -1800.
    assert _wallet_balance(db_session, user.id) == Decimal("8200.00")

    meta = result.tx.meta
    # apply_provider_result wrote these.
    assert meta["partial_delivery"] is True
    assert meta["delivered_amount_ngn"] == "1800.00"
    assert meta["shortfall_ngn"]        == "200.00"

    # And B5's post-processor layered token + units on top.
    token = meta.get("token")
    units = meta.get("units")
    assert isinstance(token, str) and len(token) == 20 and token.isdigit()
    # Units derived from the *delivered* amount, not the requested.
    assert Decimal(units) == (Decimal("1800.00") / Decimal("40")).quantize(
        Decimal("0.01")
    )


# ── B5-5: insufficient balance — tx failed, provider NOT called ──────────


@pytest.mark.asyncio
async def test_purchase_electricity_insufficient_balance_skips_provider(
    db_session,
):
    """Wallet below amount → InsufficientBalance raised, tx marked failed
    for audit, and the provider is NEVER called (we bail at the debit
    step). Uses a counting fake to prove purchase_electricity wasn't hit."""
    user = _seed_user(db_session, balance=Decimal("500.00"))

    class CountingFake(FakeVTPassClient):
        def __init__(self) -> None:
            super().__init__()
            self.purchase_calls = 0

        async def purchase_electricity(self, **kw):  # type: ignore[override]
            self.purchase_calls += 1
            return await super().purchase_electricity(**kw)

    fake = CountingFake()
    svc = _bill_service(db_session, fake=fake)

    with pytest.raises(InsufficientBalance):
        await svc.purchase_electricity(
            user_id=user.id,
            service_id="ikeja-electric",
            meter_number="1234567890123",
            meter_type="prepaid",
            phone="08012345678",
            amount_ngn=Decimal("2000.00"),
        )

    # Provider was NOT reached — debit failed first.
    assert fake.purchase_calls == 0

    # Wallet untouched.
    assert _wallet_balance(db_session, user.id) == Decimal("500.00")

    # Audit: tx row exists, marked failed.
    failed = (
        db_session.query(Transaction)
        .filter(
            Transaction.user_id == user.id,
            Transaction.type == TransactionType.electricity,
        ).all()
    )
    assert len(failed) == 1
    assert failed[0].status == TransactionStatus.failed


# ── B22 regression: post-processor merge is atomic vs concurrent writer ─


@pytest.mark.asyncio
async def test_purchase_electricity_post_processor_does_not_clobber_concurrent_meta_write(
    db_session,
):
    """Sprint 4 B22 review: the post-dispatch token/units persistence used
    to read ``result.tx.meta`` (stale in-memory copy), merge in-process,
    and commit — clobbering any concurrent writer (e.g. a VTPass
    delivery-webhook callback adding ``vtpass_transaction_id``) that
    touched the same column between ``_execute_bill``'s final commit and
    the post-processor's commit.

    The fix re-fetches the row under ``SELECT ... FOR UPDATE`` and merges
    on top of the *current* DB state, so any concurrent write that
    landed first is preserved. This test simulates that race by having
    the fake provider write ``webhook_id`` directly into ``tx.meta`` via
    a parallel UPDATE (bypasses the session identity map, emulating a
    foreign connection / Celery worker). After ``purchase_electricity``
    returns, all three keys — the original request-shape meta keys,
    the concurrent ``webhook_id``, AND the post-processor's
    ``token``/``units`` — must co-exist.
    """
    from sqlalchemy import text

    user = _seed_user(db_session)

    # SQLite (test-env) doesn't honor SELECT ... FOR UPDATE as a real
    # lock, but the invariant under test — re-read before merge — works
    # on both dialects. We use the session's own connection for the
    # external UPDATE so commit sequencing aligns.
    class ConcurrentWebhookFake(FakeVTPassClient):
        async def purchase_electricity(self, **kw):  # type: ignore[override]
            # Let _execute_bill create/commit the processing tx row
            # first, then simulate a webhook arriving on a different
            # connection and writing `webhook_id` into the row's meta.
            # We do this via raw SQL so we bypass the session's identity
            # map — same as a Celery worker using its own Session would.
            req_id = kw["request_id"]
            db_session.execute(
                text(
                    "UPDATE transactions "
                    "SET meta = json_patch(meta, :patch) "
                    "WHERE reference = :ref"
                ).bindparams(
                    patch='{"webhook_id": "wh_concurrent_abc"}',
                    ref=req_id,
                ),
            )
            db_session.commit()
            return await super().purchase_electricity(**kw)

    svc = _bill_service(db_session, fake=ConcurrentWebhookFake())

    result = await svc.purchase_electricity(
        user_id=user.id,
        service_id="ikeja-electric",
        meter_number="1234567890123",
        meter_type="prepaid",
        phone="08012345678",
        amount_ngn=Decimal("2000.00"),
    )

    # Refresh from DB so we read the merged state, not any stale copy.
    db_session.refresh(result.tx)
    meta = result.tx.meta

    # The webhook's concurrent write must survive the post-processor's
    # merge — this is the regression the B22 fix addresses.
    assert meta.get("webhook_id") == "wh_concurrent_abc", (
        "post-processor clobbered concurrent webhook write — B22 regression"
    )

    # The post-processor's token + units also landed.
    assert "token" in meta
    assert "units" in meta
    assert len(meta["token"]) == 20

    # Original request-shape meta keys preserved.
    assert meta["service_id"] == "ikeja-electric"
    assert meta["meter_number"] == "1234567890123"
