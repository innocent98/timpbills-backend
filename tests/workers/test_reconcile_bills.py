"""Regression suite for Sprint 3 B12: reconcile_pending_bills.

The task polls VTPass via `provider.requery(request_id=...)` for any
bill tx (airtime/data/electricity/cable/flight) stuck in pending or
processing for >30s, then applies the result through
BillService.apply_provider_result. This test suite covers:

 • happy path — delivered requery → tx success
 • failed path — failed requery → tx failed + wallet restored
 • partial delivery — shortfall refunded, not full amount
 • pending requery — no state change (webhook will settle later)
 • race with webhook — if tx is already in a terminal state, skip
 • transient provider failure — log-and-continue, retry next tick
 • permanent provider failure — log-and-continue (not a refund —
   ops alert; we don't know the bill outcome from a 4xx requery)
 • S2C-8 defer — KycCapExceeded/InsufficientBalance on apply → defer
 • batch-freshness gate — bills newer than 30s are not picked up
"""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest

from app.db.models._enums import TransactionStatus, TransactionType
from app.db.models.payment import Payment, PaymentStatus
from app.db.models.transaction import Transaction
from app.db.models.user import User
from app.db.models.wallet import Wallet
from app.integrations.vtpass.base import (
    BillProvider,
    ProviderPermanentFailure,
    ProviderTemporaryFailure,
)
from app.integrations.vtpass.schemas import (
    BillDeliveryStatus,
    BillPurchaseResponse,
    DataPlanList,
)


# ─── Helpers ────────────────────────────────────────────────────────────


def _seed_bill(
    db,
    *,
    amount: Decimal = Decimal("500.00"),
    wallet_balance_after_debit: Decimal = Decimal("4500.00"),
    wallet_cap: Decimal = Decimal("50000.00"),
    tx_status: TransactionStatus = TransactionStatus.processing,
    tx_type: TransactionType = TransactionType.airtime,
    age_seconds: int = 120,
) -> Transaction:
    """Seed a bill tx that's `age_seconds` old — old enough to pass the
    30s reconcile cutoff by default. Wallet already debited (BillService
    sets PaymentStatus.success on a wallet debit), so the refund path
    puts money BACK."""
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

    wallet = Wallet(
        id=uuid.uuid4(),
        user_id=user.id,
        balance=wallet_balance_after_debit,
        balance_cap=wallet_cap,
    )
    tx = Transaction(
        id=uuid.uuid4(),
        user_id=user.id,
        reference=f"TMP-BILL-{uuid.uuid4().hex[:8]}",
        type=tx_type,
        status=tx_status,
        amount=amount,
        fee=Decimal("0.00"),
        currency="NGN",
        meta={"network": "MTN", "phone": "08012345678", "service_id": "mtn"},
    )
    db.add_all([wallet, tx])
    db.flush()

    payment = Payment(
        id=uuid.uuid4(),
        transaction_id=tx.id,
        provider="wallet",
        provider_reference=tx.reference,
        status=PaymentStatus.success,
    )
    db.add(payment)
    db.commit()

    # Back-date the tx so it passes the 30s cutoff.
    db.query(Transaction).filter(Transaction.id == tx.id).update(
        {"created_at": datetime.now(timezone.utc) - timedelta(seconds=age_seconds)}
    )
    db.commit()
    db.refresh(tx)
    return tx


class _FakeRequeryProvider(BillProvider):
    """Stub BillProvider where each test injects what requery returns for
    a given request_id. Simpler than subclassing FakeVTPassClient since
    the reconcile task only touches .requery()."""

    def __init__(self) -> None:
        self._by_ref: dict[str, BillPurchaseResponse] = {}
        self._raise_by_ref: dict[str, Exception] = {}

    def set_response(self, request_id: str, response: BillPurchaseResponse) -> None:
        self._by_ref[request_id] = response

    def set_raise(self, request_id: str, exc: Exception) -> None:
        self._raise_by_ref[request_id] = exc

    async def purchase_airtime(self, **kwargs):
        raise AssertionError("reconcile shouldn't purchase")

    async def purchase_data(self, **kwargs):
        raise AssertionError("reconcile shouldn't purchase")

    async def list_data_plans(self, **kwargs) -> DataPlanList:
        raise AssertionError("reconcile shouldn't list plans")

    async def requery(self, *, request_id: str) -> BillPurchaseResponse:
        if request_id in self._raise_by_ref:
            raise self._raise_by_ref[request_id]
        if request_id not in self._by_ref:
            raise AssertionError(
                f"test didn't configure a requery response for {request_id!r}"
            )
        return self._by_ref[request_id]


def _run_reconcile_bills(db_session, provider: _FakeRequeryProvider) -> dict:
    """Invoke the Celery task body with the shared test db session and
    a stub provider. Mirrors the harness used by the Paystack reconcile
    tests."""
    from app.workers.tasks import reconcile_tasks as rt

    original_close = db_session.close
    db_session.close = lambda: None
    try:
        with patch.object(rt, "SessionLocal", lambda: db_session), \
             patch.object(rt, "select_vtpass_client", lambda: provider):
            return rt.reconcile_pending_bills()
    finally:
        db_session.close = original_close


def _delivered(ref: str, amount: Decimal) -> BillPurchaseResponse:
    return BillPurchaseResponse(
        request_id=ref, transaction_id=f"vtp_{ref[:8]}",
        status=BillDeliveryStatus.delivered, code="000",
        requested_amount_ngn=amount, delivered_amount_ngn=amount,
        description="TRANSACTION SUCCESSFUL",
        raw={"code": "000", "_fake_requery": True},
    )


def _partial(ref: str, requested: Decimal, delivered: Decimal) -> BillPurchaseResponse:
    return BillPurchaseResponse(
        request_id=ref, transaction_id=f"vtp_{ref[:8]}",
        status=BillDeliveryStatus.delivered, code="000",
        requested_amount_ngn=requested, delivered_amount_ngn=delivered,
        description="PARTIAL DELIVERY",
        raw={"code": "000", "partial": True},
    )


def _pending(ref: str, amount: Decimal) -> BillPurchaseResponse:
    return BillPurchaseResponse(
        request_id=ref, transaction_id="",
        status=BillDeliveryStatus.pending, code="099",
        requested_amount_ngn=amount, delivered_amount_ngn=Decimal("0.00"),
        description="PENDING", raw={"code": "099"},
    )


def _failed(ref: str, amount: Decimal) -> BillPurchaseResponse:
    return BillPurchaseResponse(
        request_id=ref, transaction_id="",
        status=BillDeliveryStatus.failed, code="016",
        requested_amount_ngn=amount, delivered_amount_ngn=Decimal("0.00"),
        description="TRANSACTION FAILED", raw={"code": "016"},
    )


# ─── Happy path: delivered ──────────────────────────────────────────────


def test_reconcile_bills_delivered_transitions_tx_to_success(db_session):
    tx = _seed_bill(db_session, amount=Decimal("500.00"))
    provider = _FakeRequeryProvider()
    provider.set_response(tx.reference, _delivered(tx.reference, Decimal("500.00")))

    result = _run_reconcile_bills(db_session, provider)
    assert result == {
        "checked": 1, "settled": 1, "deferred": 0, "skipped": 0, "escalated": 0,
    }

    db_session.expire_all()
    fresh = db_session.query(Transaction).filter(Transaction.id == tx.id).one()
    assert fresh.status == TransactionStatus.success
    refunds = (
        db_session.query(Transaction)
        .filter(Transaction.user_id == tx.user_id, Transaction.type == TransactionType.refund)
        .count()
    )
    assert refunds == 0


# ─── Failed → refund + wallet restored ──────────────────────────────────


def test_reconcile_bills_failed_transitions_and_refunds(db_session):
    tx = _seed_bill(
        db_session, amount=Decimal("500.00"),
        wallet_balance_after_debit=Decimal("4500.00"),
    )
    provider = _FakeRequeryProvider()
    provider.set_response(tx.reference, _failed(tx.reference, Decimal("500.00")))

    result = _run_reconcile_bills(db_session, provider)
    assert result["settled"] == 1

    db_session.expire_all()
    fresh = db_session.query(Transaction).filter(Transaction.id == tx.id).one()
    assert fresh.status == TransactionStatus.failed
    wallet = db_session.query(Wallet).filter(Wallet.user_id == tx.user_id).one()
    assert wallet.balance == Decimal("5000.00")   # restored by refund credit
    refunds = (
        db_session.query(Transaction)
        .filter(Transaction.user_id == tx.user_id, Transaction.type == TransactionType.refund)
        .all()
    )
    assert len(refunds) == 1
    assert refunds[0].amount == Decimal("500.00")


# ─── Partial delivery — shortfall refunded ──────────────────────────────


def test_reconcile_bills_partial_delivery_refunds_shortfall(db_session):
    tx = _seed_bill(db_session, amount=Decimal("500.00"))
    provider = _FakeRequeryProvider()
    provider.set_response(
        tx.reference,
        _partial(tx.reference, requested=Decimal("500.00"), delivered=Decimal("450.00")),
    )

    result = _run_reconcile_bills(db_session, provider)
    assert result["settled"] == 1

    db_session.expire_all()
    fresh = db_session.query(Transaction).filter(Transaction.id == tx.id).one()
    assert fresh.status == TransactionStatus.success
    assert fresh.meta.get("partial_delivery") is True
    assert fresh.meta.get("shortfall_ngn") == "50.00"
    wallet = db_session.query(Wallet).filter(Wallet.user_id == tx.user_id).one()
    # Wallet back up by the shortfall only: 4500 + 50 = 4550.
    assert wallet.balance == Decimal("4550.00")


# ─── Pending requery — no-op ────────────────────────────────────────────


def test_reconcile_bills_pending_is_noop(db_session):
    tx = _seed_bill(db_session, amount=Decimal("500.00"))
    provider = _FakeRequeryProvider()
    provider.set_response(tx.reference, _pending(tx.reference, Decimal("500.00")))

    result = _run_reconcile_bills(db_session, provider)
    # Pending is a valid apply result — BillService leaves tx in processing.
    # The reconcile worker counts this as "settled" because the call
    # returned without raising.
    assert result["checked"] == 1

    db_session.expire_all()
    fresh = db_session.query(Transaction).filter(Transaction.id == tx.id).one()
    assert fresh.status == TransactionStatus.processing


# ─── Race with webhook: already-finalized tx ────────────────────────────


def test_reconcile_bills_skips_tx_already_finalized_by_webhook(db_session):
    """Webhook arrived first and transitioned the tx to success; the
    reconcile run a minute later picks it up (still uses the same pending/
    processing filter, but a race window can let a success-state tx slip
    past if it flipped between SELECT and the row-lock — the _TX_FINAL_STATES
    check after WITH FOR UPDATE catches it)."""
    tx = _seed_bill(
        db_session, amount=Decimal("500.00"),
        tx_status=TransactionStatus.processing,
    )
    # Simulate webhook winning: after the SELECT but before the lock,
    # the tx flipped to success. We bake this by pre-flipping before the
    # reconcile function's with_for_update query picks the row up.
    # Since the initial filter is status IN (pending, processing), a fully
    # success tx wouldn't be in the candidate set — so we have to flip it
    # AFTER the candidate query fires. The cleanest way to simulate: patch
    # provider.requery to flip the status before returning.
    provider = _FakeRequeryProvider()

    original_requery = provider.requery

    async def requery_then_flip(*, request_id: str):
        # Simulate concurrent webhook landing while the worker is mid-requery.
        db_session.query(Transaction).filter(
            Transaction.id == tx.id
        ).update({"status": TransactionStatus.success})
        db_session.commit()
        return _delivered(request_id, Decimal("500.00"))

    provider.requery = requery_then_flip   # type: ignore[method-assign]
    provider.set_response(tx.reference, _delivered(tx.reference, Decimal("500.00")))

    result = _run_reconcile_bills(db_session, provider)
    assert result["skipped"] == 1
    assert result["settled"] == 0

    db_session.expire_all()
    fresh = db_session.query(Transaction).filter(Transaction.id == tx.id).one()
    # Still at the status the webhook left it in — reconcile didn't double-apply.
    assert fresh.status == TransactionStatus.success
    # And no refund was duplicated.
    refunds = (
        db_session.query(Transaction)
        .filter(Transaction.user_id == tx.user_id, Transaction.type == TransactionType.refund)
        .count()
    )
    assert refunds == 0


# ─── Transient provider failure ─────────────────────────────────────────


def test_reconcile_bills_transient_failure_retries_next_tick(db_session):
    tx = _seed_bill(db_session, amount=Decimal("500.00"))
    provider = _FakeRequeryProvider()
    provider.set_raise(
        tx.reference, ProviderTemporaryFailure("vtpass 503"),
    )

    result = _run_reconcile_bills(db_session, provider)
    assert result["checked"] == 1
    assert result["settled"] == 0
    assert result["deferred"] == 0

    db_session.expire_all()
    fresh = db_session.query(Transaction).filter(Transaction.id == tx.id).one()
    assert fresh.status == TransactionStatus.processing   # unchanged


# ─── Permanent provider failure on requery itself ───────────────────────


def test_reconcile_bills_permanent_requery_error_bumps_attempts_counter(db_session):
    """ProviderPermanentFailure raised by requery means VTPass refused
    the requery call (e.g. 400). That does NOT tell us the bill outcome,
    so we don't auto-refund — but we also can't loop forever (see S3C-P3).
    First N-1 attempts: bump the attempts counter in tx.meta and leave
    the tx in processing. Nth attempt escalates via transition to failed.
    """
    tx = _seed_bill(db_session, amount=Decimal("500.00"))
    provider = _FakeRequeryProvider()
    provider.set_raise(
        tx.reference, ProviderPermanentFailure("vtpass 400 invalid request_id"),
    )

    result = _run_reconcile_bills(db_session, provider)
    assert result["settled"] == 0
    assert result["deferred"] == 0
    assert result["escalated"] == 0   # only 1 attempt so far

    db_session.expire_all()
    fresh = db_session.query(Transaction).filter(Transaction.id == tx.id).one()
    assert fresh.status == TransactionStatus.processing
    assert fresh.meta["requery_permanent_attempts"] == 1
    # Wallet unchanged — we didn't auto-refund based on a rejected requery.
    wallet = db_session.query(Wallet).filter(Wallet.user_id == tx.user_id).one()
    assert wallet.balance == Decimal("4500.00")
    refunds = (
        db_session.query(Transaction)
        .filter(Transaction.user_id == tx.user_id, Transaction.type == TransactionType.refund)
        .count()
    )
    assert refunds == 0


def test_reconcile_bills_escalates_after_max_permanent_attempts(db_session):
    """After _MAX_REQUERY_PERMANENT_ATTEMPTS consecutive permanent
    requery errors, the tx is transitioned to failed with reason
    `needs_ops_review` so ops has a concrete handle. The tx drops out
    of the pending/processing sweep on the next tick (no infinite loop).
    No auto-refund — the bill may have actually delivered upstream; ops
    decides. This is the S3C-P3 fix."""
    from app.workers.tasks.reconcile_tasks import (
        _MAX_REQUERY_PERMANENT_ATTEMPTS,
    )

    tx = _seed_bill(db_session, amount=Decimal("500.00"))
    provider = _FakeRequeryProvider()
    provider.set_raise(
        tx.reference, ProviderPermanentFailure("vtpass 400 invalid request_id"),
    )

    # Simulate (N-1) prior attempts — this run is the Nth.
    db_session.query(Transaction).filter(Transaction.id == tx.id).update(
        {"meta": {
            **tx.meta, "requery_permanent_attempts": _MAX_REQUERY_PERMANENT_ATTEMPTS - 1,
        }},
    )
    db_session.commit()

    result = _run_reconcile_bills(db_session, provider)
    assert result["escalated"] == 1
    assert result["settled"] == 0

    db_session.expire_all()
    fresh = db_session.query(Transaction).filter(Transaction.id == tx.id).one()
    assert fresh.status == TransactionStatus.failed
    # Wallet unchanged — escalation doesn't refund. Ops reviews.
    wallet = db_session.query(Wallet).filter(Wallet.user_id == tx.user_id).one()
    assert wallet.balance == Decimal("4500.00")


# ─── Cutoff: bills younger than 30s are skipped ─────────────────────────


def test_reconcile_bills_skips_bills_newer_than_30s(db_session):
    tx = _seed_bill(
        db_session, amount=Decimal("500.00"),
        age_seconds=5,   # younger than 30s cutoff
    )
    provider = _FakeRequeryProvider()
    provider.set_response(tx.reference, _delivered(tx.reference, Decimal("500.00")))

    result = _run_reconcile_bills(db_session, provider)
    assert result == {
        "checked": 0, "settled": 0, "deferred": 0, "skipped": 0, "escalated": 0,
    }

    db_session.expire_all()
    fresh = db_session.query(Transaction).filter(Transaction.id == tx.id).one()
    assert fresh.status == TransactionStatus.processing   # unchanged


# ─── S3C-L2: S2C-8 defer regression for reconcile_pending_bills ─────────


def test_reconcile_bills_defers_on_kyc_cap_and_keeps_batch_going(db_session):
    """S2C-8 taxonomy for the reconcile_bills path: when apply_provider_result
    raises KycCapExceeded (user's tier was lowered post-debit, refund
    credit now overshoots), the reconciler must roll back that row's
    partial state and continue the batch. Increments `deferred` in the
    result dict so ops can alert on non-zero.

    The module docstring advertised this behavior; no test existed
    before S3C-L2."""
    # Seed user near their cap so the refund credit will overshoot.
    # Balance ₦48k, cap ₦50k. A failed ₦5000 bill tries to refund
    # credit → 48 + 5 = 53 > 50 → KycCapExceeded.
    tx = _seed_bill(
        db_session,
        amount=Decimal("5000.00"),
        wallet_balance_after_debit=Decimal("48000.00"),
        wallet_cap=Decimal("50000.00"),
    )
    provider = _FakeRequeryProvider()
    provider.set_response(tx.reference, _failed(tx.reference, Decimal("5000.00")))

    result = _run_reconcile_bills(db_session, provider)
    assert result["deferred"] == 1
    assert result["settled"] == 0
    assert result["escalated"] == 0

    # Tx stays in processing so the next tick can retry (maybe ops
    # raises the cap in the interval). No partial state committed.
    db_session.expire_all()
    fresh = db_session.query(Transaction).filter(Transaction.id == tx.id).one()
    assert fresh.status == TransactionStatus.processing

    # Wallet unchanged — the rollback wiped the credit attempt.
    wallet = db_session.query(Wallet).filter(Wallet.user_id == tx.user_id).one()
    assert wallet.balance == Decimal("48000.00")

    # No refund tx was committed.
    refunds = (
        db_session.query(Transaction)
        .filter(Transaction.user_id == tx.user_id, Transaction.type == TransactionType.refund)
        .count()
    )
    assert refunds == 0
