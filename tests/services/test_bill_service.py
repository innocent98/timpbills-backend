"""BillService orchestration — covers every branch of _execute_bill:

 * delivered full      → tx.success, wallet unchanged after net flow
 * delivered partial   → tx.success, refund created for shortfall
 * provider failed     → tx.failed, refund created, wallet back to start
 * transient failure   → tx stays processing, wallet debited (reconcile owns)
 * permanent failure   → tx.failed, refund created, wallet back to start
 * insufficient bal.   → tx.failed, provider NOT called
"""
import uuid
from decimal import Decimal

import pytest
from fakeredis.aioredis import FakeRedis

from app.db.models._enums import TransactionStatus, TransactionType
from app.db.models.payment import Payment, PaymentStatus
from app.db.models.transaction import Transaction
from app.db.models.user import User
from app.db.models.wallet import Wallet
from app.integrations.vtpass.base import (
    ProviderPermanentFailure,
    ProviderTemporaryFailure,
)
from app.integrations.vtpass.fake import FakeVTPassClient
from app.services.bill_service import BillService, DataPlanNotFound
from app.services.transaction_service import TransactionService
from app.services.wallet_service import InsufficientBalance, WalletService


def _seed_user(db, *, balance: Decimal = Decimal("5000.00")) -> User:
    user = User(
        id=uuid.uuid4(),
        email=f"bill-{uuid.uuid4().hex[:6]}@t.co",
        phone=f"+234{uuid.uuid4().int % 10**10:010d}",
        full_name="Bill Test",
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


# ── airtime branches ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_airtime_happy_path(db_session):
    user = _seed_user(db_session)
    fake = FakeVTPassClient()  # default: success
    svc = _bill_service(db_session, fake=fake)

    result = await svc.purchase_airtime(
        user_id=user.id, network="MTN", phone="08012345678",
        amount_ngn=Decimal("500.00"),
    )

    assert result.tx.status == TransactionStatus.success
    assert result.tx.type == TransactionType.airtime
    assert _wallet_balance(db_session, user.id) == Decimal("4500.00")

    # Payment row exists with provider=wallet.
    payment = (
        db_session.query(Payment)
        .filter(Payment.provider_reference == result.tx.reference).one()
    )
    assert payment.provider == "wallet"
    assert payment.status == PaymentStatus.success


@pytest.mark.asyncio
async def test_airtime_partial_delivery_refunds_shortfall(db_session):
    user = _seed_user(db_session)
    fake = FakeVTPassClient()
    svc = _bill_service(db_session, fake=fake)

    # Set up a partial delivery on the NEXT generated reference — we
    # need to intercept before the provider call. Easiest: fire the
    # purchase and capture the reference from result; that's too late.
    # Instead, patch _DEFAULT_PLANS isn't useful either — we need
    # will_partial on a ref we don't know yet. Solution: call the same
    # flow but with a pre-set partial by patching tx reference. Simpler:
    # purchase_airtime uses request_id=tx.reference inside, so we hook
    # at the provider layer via a thin subclass.
    class PartialFake(FakeVTPassClient):
        async def purchase_airtime(self, **kw):  # type: ignore[override]
            self.will_partial(kw["request_id"], delivered_ngn=Decimal("450.00"))
            return await super().purchase_airtime(**kw)

    fake = PartialFake()
    svc = _bill_service(db_session, fake=fake)

    result = await svc.purchase_airtime(
        user_id=user.id, network="MTN", phone="08012345678",
        amount_ngn=Decimal("500.00"),
    )

    assert result.tx.status == TransactionStatus.success
    assert result.tx.meta["partial_delivery"] is True
    assert result.tx.meta["delivered_amount_ngn"] == "450.00"
    assert result.tx.meta["shortfall_ngn"] == "50.00"
    # Wallet: debited 500, refunded 50 → net 450 below start.
    assert _wallet_balance(db_session, user.id) == Decimal("4550.00")


@pytest.mark.asyncio
async def test_airtime_provider_failed_refunds_full(db_session):
    user = _seed_user(db_session)

    class FailingFake(FakeVTPassClient):
        async def purchase_airtime(self, **kw):  # type: ignore[override]
            self.will_fail(kw["request_id"])
            return await super().purchase_airtime(**kw)

    svc = _bill_service(db_session, fake=FailingFake())

    result = await svc.purchase_airtime(
        user_id=user.id, network="MTN", phone="08012345678",
        amount_ngn=Decimal("500.00"),
    )

    assert result.tx.status == TransactionStatus.failed
    # Wallet: debited 500, refunded 500 → net zero change.
    assert _wallet_balance(db_session, user.id) == Decimal("5000.00")
    # Refund tx exists.
    refunds = (
        db_session.query(Transaction)
        .filter(
            Transaction.user_id == user.id,
            Transaction.type == TransactionType.refund,
        ).all()
    )
    assert len(refunds) == 1
    assert refunds[0].amount == Decimal("500.00")
    assert refunds[0].status == TransactionStatus.success


@pytest.mark.asyncio
async def test_airtime_provider_pending_leaves_tx_processing(db_session):
    user = _seed_user(db_session)

    class PendingFake(FakeVTPassClient):
        async def purchase_airtime(self, **kw):  # type: ignore[override]
            self.will_remain_pending(kw["request_id"])
            return await super().purchase_airtime(**kw)

    svc = _bill_service(db_session, fake=PendingFake())

    result = await svc.purchase_airtime(
        user_id=user.id, network="MTN", phone="08012345678",
        amount_ngn=Decimal("500.00"),
    )

    assert result.tx.status == TransactionStatus.processing
    # Wallet is debited; reconcile worker will finalize later.
    assert _wallet_balance(db_session, user.id) == Decimal("4500.00")


@pytest.mark.asyncio
async def test_airtime_transient_failure_leaves_tx_processing(db_session):
    user = _seed_user(db_session)

    class TransientFake(FakeVTPassClient):
        async def purchase_airtime(self, **kw):  # type: ignore[override]
            raise ProviderTemporaryFailure("simulated network blip")

    svc = _bill_service(db_session, fake=TransientFake())

    result = await svc.purchase_airtime(
        user_id=user.id, network="MTN", phone="08012345678",
        amount_ngn=Decimal("500.00"),
    )

    assert result.tx.status == TransactionStatus.processing
    # Wallet debited; stays that way — reconcile will sort out.
    assert _wallet_balance(db_session, user.id) == Decimal("4500.00")
    # Response is the placeholder pending.
    assert result.response.code == "TIMP_TRANSIENT"


@pytest.mark.asyncio
async def test_airtime_permanent_failure_refunds(db_session):
    user = _seed_user(db_session)

    class PermanentFake(FakeVTPassClient):
        async def purchase_airtime(self, **kw):  # type: ignore[override]
            raise ProviderPermanentFailure("bad amount")

    svc = _bill_service(db_session, fake=PermanentFake())

    result = await svc.purchase_airtime(
        user_id=user.id, network="MTN", phone="08012345678",
        amount_ngn=Decimal("500.00"),
    )

    assert result.tx.status == TransactionStatus.failed
    assert result.response.code == "TIMP_PERMANENT"
    # Refund + credit back to original balance.
    assert _wallet_balance(db_session, user.id) == Decimal("5000.00")


@pytest.mark.asyncio
async def test_airtime_insufficient_balance_raises_and_marks_failed(db_session):
    user = _seed_user(db_session, balance=Decimal("100.00"))
    fake = FakeVTPassClient()
    svc = _bill_service(db_session, fake=fake)

    with pytest.raises(InsufficientBalance):
        await svc.purchase_airtime(
            user_id=user.id, network="MTN", phone="08012345678",
            amount_ngn=Decimal("500.00"),
        )

    # Wallet untouched.
    assert _wallet_balance(db_session, user.id) == Decimal("100.00")
    # A tx was created and marked failed (for audit trail).
    failed = (
        db_session.query(Transaction)
        .filter(
            Transaction.user_id == user.id,
            Transaction.type == TransactionType.airtime,
        ).all()
    )
    assert len(failed) == 1
    assert failed[0].status == TransactionStatus.failed


# ── data branches ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_data_happy_path_uses_server_side_price(db_session):
    """Price comes from the VTPass catalog, not the client. User can
    submit variation_code='mtn-1gb-monthly' and the ₦1000 price is
    applied regardless of what they'd like it to be."""
    user = _seed_user(db_session)
    fake = FakeVTPassClient()
    svc = _bill_service(db_session, fake=fake)

    result = await svc.purchase_data(
        user_id=user.id, network="MTN", phone="08012345678",
        variation_code="mtn-1gb-monthly",
    )

    assert result.tx.status == TransactionStatus.success
    assert result.tx.amount == Decimal("1000.00")
    assert result.tx.meta["plan_name"] == "1GB - 30 days"
    assert _wallet_balance(db_session, user.id) == Decimal("4000.00")


@pytest.mark.asyncio
async def test_data_unknown_variation_raises(db_session):
    user = _seed_user(db_session)
    fake = FakeVTPassClient()
    svc = _bill_service(db_session, fake=fake)

    with pytest.raises(DataPlanNotFound):
        await svc.purchase_data(
            user_id=user.id, network="MTN", phone="08012345678",
            variation_code="bogus-plan",
        )
    # No tx created — we bail before the wallet debit.
    assert _wallet_balance(db_session, user.id) == Decimal("5000.00")
    assert (
        db_session.query(Transaction).filter(
            Transaction.user_id == user.id
        ).count() == 0
    )


@pytest.mark.asyncio
async def test_list_data_plans_passthrough(db_session):
    fake = FakeVTPassClient()
    svc = _bill_service(db_session, fake=fake)
    plans = await svc.list_data_plans(network="MTN")
    assert plans.service_id == "mtn-data"
    assert any(v.variation_code == "mtn-1gb-monthly" for v in plans.variations)


# ── S3C-P2: concurrent-finalizer race on the sync path ────────────────


@pytest.mark.asyncio
async def test_sync_path_skips_apply_when_tx_already_finalized(db_session):
    """If the VTPass webhook or reconcile worker finalizes the tx while
    the sync purchase call is still awaiting the provider, the sync
    path MUST NOT re-apply state (it would double-refund via
    _refund_and_credit, and/or raise InvalidStateTransition at transition).

    The simulated race: provider_fn flips the tx to `failed` before
    returning — mimicking a concurrent vtpass webhook landing the
    failed status mid-request. Sync path sees the row-lock terminal
    status and bails without re-applying.
    """
    user = _seed_user(db_session)

    class RacingFake(FakeVTPassClient):
        def __init__(self, db):
            super().__init__()
            self._db = db

        async def purchase_airtime(self, **kw):  # type: ignore[override]
            # Mid-provider-call: pretend the vtpass webhook just landed
            # and flipped the tx to failed (via reconcile, whichever).
            ref = kw["request_id"]
            tx = self._db.query(Transaction).filter(
                Transaction.reference == ref,
            ).one()
            tx.status = TransactionStatus.failed
            self._db.commit()
            # Provider still returns a delivered response — a real
            # VTPass would. The sync path now has a stale view.
            return await super().purchase_airtime(**kw)

    fake = RacingFake(db_session)
    svc = _bill_service(db_session, fake=fake)

    result = await svc.purchase_airtime(
        user_id=user.id, network="MTN", phone="08012345678",
        amount_ngn=Decimal("500.00"),
    )

    # The sync path honored the concurrent finalizer.
    assert result.tx.status == TransactionStatus.failed

    # NO refund tx was minted by the sync path (the webhook path owns that).
    db_session.expire_all()
    refunds = db_session.query(Transaction).filter(
        Transaction.user_id == user.id,
        Transaction.type == TransactionType.refund,
    ).count()
    assert refunds == 0

    # And the wallet wasn't credited back by the sync path.
    assert _wallet_balance(db_session, user.id) == Decimal("4500.00")
