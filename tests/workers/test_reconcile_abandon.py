"""Regression: reconcile worker must stop polling Paystack forever for
abandoned checkouts.

Before this fix the candidate query had a lower age bound (>30s old) but no
upper bound, and the verify loop's only handling for a non-terminal verify
status ("abandoned") was `# abandoned → leave pending for next poll`. Result:
a checkout the user never completed stayed PENDING and got re-verified every
2 minutes forever — unbounded wasted Paystack API calls.

The fix adds a max-age abandon sweep (PAYMENT_ABANDON_AFTER_HOURS, default 24):
once a pending payment is older than the threshold we mark it terminally
(Payment -> failed, Transaction -> failed, reason="reconcile.abandoned")
WITHOUT calling Paystack verify, and refund only if the tx type is refundable.
"""
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from app.db.models._enums import TransactionStatus, TransactionType
from app.db.models.payment import Payment, PaymentStatus
from app.db.models.transaction import Transaction
from app.db.models.user import User
from app.db.models.wallet import Wallet


def _seed_pending(db, *, age: timedelta, tx_type=TransactionType.wallet_funding) -> Payment:
    """Seed a user + wallet + pending tx/payment aged `age` in the past."""
    user = User(
        id=uuid.uuid4(),
        email=f"aband-{uuid.uuid4().hex[:6]}@t.co",
        phone=f"+234{uuid.uuid4().int % 10**10:010d}",
        full_name="Abandon Test",
        password_hash="x",
        is_active=True,
    )
    db.add(user)
    db.flush()

    wallet = Wallet(
        id=uuid.uuid4(),
        user_id=user.id,
        balance=Decimal("0.00"),
        balance_cap=Decimal("50000.00"),
    )
    tx = Transaction(
        id=uuid.uuid4(),
        user_id=user.id,
        reference=f"TMP-ABD-{uuid.uuid4().hex[:6]}",
        type=tx_type,
        status=TransactionStatus.processing,
        amount=Decimal("5000.00"),
        fee=Decimal("0.00"),
        currency="NGN",
    )
    db.add_all([wallet, tx])
    db.flush()

    payment = Payment(
        id=uuid.uuid4(),
        transaction_id=tx.id,
        provider="paystack",
        provider_reference=f"ref_abd_{uuid.uuid4().hex[:8]}",
        status=PaymentStatus.pending,
    )
    db.add(payment)
    db.commit()
    # created_at is set by the TimestampMixin default at INSERT; backdate it.
    payment.created_at = datetime.now(UTC) - age
    tx.created_at = datetime.now(UTC) - age
    db.commit()
    return payment


class _ExplodingClient:
    """verify() must NOT be called for an over-age abandoned payment."""

    async def verify(self, *, reference: str):  # noqa: D401
        raise AssertionError(
            "verify() was called for an over-age payment — the abandon sweep "
            "should have closed it out WITHOUT hitting Paystack"
        )


def test_over_age_pending_is_abandoned_without_calling_paystack(db_session, monkeypatch):
    """A pending payment older than PAYMENT_ABANDON_AFTER_HOURS is swept
    terminal without a verify call. Payment -> failed, Transaction -> failed
    with reason='reconcile.abandoned'. wallet_funding is NOT refundable, so
    the balance stays untouched.
    """
    # 25h old > 24h default threshold.
    payment = _seed_pending(db_session, age=timedelta(hours=25))

    from app.workers.tasks import reconcile_tasks as rt

    with patch.object(rt, "SessionLocal", lambda: db_session), \
         patch.object(rt, "select_paystack_client", lambda: _ExplodingClient()):
        original_close = db_session.close
        db_session.close = lambda: None
        try:
            result = rt.reconcile_pending_payments()
        finally:
            db_session.close = original_close

    assert result["checked"] == 1
    assert result["abandoned"] == 1
    assert result["settled"] == 0

    db_session.expire_all()
    fresh_payment = db_session.query(Payment).filter(Payment.id == payment.id).one()
    assert fresh_payment.status == PaymentStatus.failed

    tx = db_session.query(Transaction).filter(
        Transaction.id == payment.transaction_id
    ).one()
    assert tx.status == TransactionStatus.failed

    # wallet_funding is inbound — not in _REFUNDABLE_ON_FAILURE — no refund row.
    refunds = (
        db_session.query(Transaction)
        .filter(Transaction.type == TransactionType.refund)
        .all()
    )
    assert refunds == []

    wallet = db_session.query(Wallet).filter(Wallet.user_id == tx.user_id).one()
    assert wallet.balance == Decimal("0.00")


def test_fresh_pending_still_polls_paystack(db_session, monkeypatch):
    """A pending payment under the abandon threshold is NOT swept — it still
    goes through the normal verify path. Guards against the sweep being too
    aggressive and closing out live, still-completable checkouts.
    """
    from app.integrations.paystack.schemas import VerifyResponse

    captured = {"verified": False}

    class _RecordingClient:
        async def verify(self, *, reference: str) -> VerifyResponse:
            captured["verified"] = True
            # Mid-flight abandoned status: not yet terminal, leave pending.
            return VerifyResponse(
                reference=reference, status="abandoned", amount=Decimal("5000.00"),
            )

    payment = _seed_pending(db_session, age=timedelta(minutes=5))

    from app.workers.tasks import reconcile_tasks as rt

    with patch.object(rt, "SessionLocal", lambda: db_session), \
         patch.object(rt, "select_paystack_client", lambda: _RecordingClient()):
        original_close = db_session.close
        db_session.close = lambda: None
        try:
            result = rt.reconcile_pending_payments()
        finally:
            db_session.close = original_close

    assert captured["verified"] is True
    assert result["abandoned"] == 0

    db_session.expire_all()
    fresh_payment = db_session.query(Payment).filter(Payment.id == payment.id).one()
    assert fresh_payment.status == PaymentStatus.pending
