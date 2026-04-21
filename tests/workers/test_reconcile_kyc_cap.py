"""Regression for S2C-2 / S2C-8: reconcile worker handling of KycCapExceeded.

When a Paystack verify returns success on a payment whose credit would push
the wallet over the user's KYC cap, the worker now defers that specific
payment (logs DEFERRED, rolls back, increments the `deferred` counter) and
keeps processing the rest of the batch. Pre-S2C-8 the exception propagated
out of the loop and every other pending payment was skipped.
"""
import uuid
from decimal import Decimal
from unittest.mock import patch

from app.db.models._enums import TransactionStatus, TransactionType
from app.db.models.payment import Payment, PaymentStatus
from app.db.models.transaction import Transaction
from app.db.models.user import User
from app.db.models.wallet import Wallet
from app.integrations.paystack.schemas import PaystackAuthorization, VerifyResponse


def _seed_user_wallet_tx(db, balance: Decimal, amount: Decimal) -> Payment:
    """Seed user with near-cap balance + pending funding tx/payment."""
    user = User(
        id=uuid.uuid4(),
        email=f"cap-{uuid.uuid4().hex[:6]}@t.co",
        phone=f"+234{uuid.uuid4().int % 10**10:010d}",
        full_name="Cap Test",
        password_hash="x",
        is_active=True,
    )
    db.add(user)
    db.flush()

    wallet = Wallet(
        id=uuid.uuid4(),
        user_id=user.id,
        balance=balance,
        balance_cap=Decimal("50000.00"),
    )
    tx = Transaction(
        id=uuid.uuid4(),
        user_id=user.id,
        reference=f"TMP-CAP-{uuid.uuid4().hex[:6]}",
        type=TransactionType.wallet_funding,
        status=TransactionStatus.processing,
        amount=amount,
        fee=Decimal("0.00"),
        currency="NGN",
    )
    db.add_all([wallet, tx])
    db.flush()

    payment = Payment(
        id=uuid.uuid4(),
        transaction_id=tx.id,
        provider="paystack",
        provider_reference=f"ref_cap_{uuid.uuid4().hex[:8]}",
        status=PaymentStatus.pending,
    )
    db.add(payment)
    db.commit()
    return payment


class _FakeVerifyingClient:
    """Minimal client that always returns success verifies for queued refs."""

    async def verify(self, *, reference: str) -> VerifyResponse:
        return VerifyResponse(
            reference=reference,
            status="success",
            amount=Decimal("5000.00"),
            paid_at="2026-04-21T12:00:00Z",
            authorization=PaystackAuthorization(channel="card", last4="4081", bank=None),
        )


def test_reconcile_defers_kyc_cap_and_keeps_batch_going(db_session, monkeypatch):
    """S2C-8 behaviour: KycCapExceeded on one payment defers that payment
    (leaves it pending, rolls back partial state) but doesn't derail the
    rest of the sweep. The `deferred` counter in the summary reflects the
    defer; the overall result is a successful task run, not a crash.
    """
    # Seed: balance ₦46k, funding ₦5k → would push to ₦51k > ₦50k cap.
    payment = _seed_user_wallet_tx(
        db_session, balance=Decimal("46000.00"), amount=Decimal("5000.00")
    )

    # Make it eligible for the 30s-old sweep.
    from datetime import datetime, timedelta, timezone
    payment.created_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    db_session.commit()

    from app.workers.tasks import reconcile_tasks as rt

    with patch.object(rt, "SessionLocal", lambda: db_session), \
         patch.object(rt, "select_paystack_client", lambda: _FakeVerifyingClient()):
        # Prevent the worker from closing our shared session at finally.
        original_close = db_session.close
        db_session.close = lambda: None
        try:
            result = rt.reconcile_pending_payments()
        finally:
            db_session.close = original_close

    assert result["checked"] == 1
    assert result["settled"] == 0
    assert result["deferred"] == 1

    # Payment still pending — the claim was rolled back by the deferred branch.
    db_session.expire_all()
    fresh = db_session.query(Payment).filter(Payment.id == payment.id).one()
    assert fresh.status == PaymentStatus.pending

    # Wallet balance unchanged — no partial credit.
    tx = db_session.query(Transaction).filter(
        Transaction.id == payment.transaction_id
    ).one()
    wallet = db_session.query(Wallet).filter(Wallet.user_id == tx.user_id).one()
    assert wallet.balance == Decimal("46000.00")
