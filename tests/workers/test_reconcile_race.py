"""Regression: webhook + reconcile must never double-credit a wallet.

The critical invariant is enforced by `_claim_payment()` in both paths:
it re-fetches the Payment row with SELECT FOR UPDATE and only flips the
status if it's still pending. Whichever caller grabs the lock first wins;
the other returns False and skips its credit/transition.
"""
import uuid

import pytest

from app.db.models.payment import Payment, PaymentStatus
from app.db.models.transaction import Transaction
from app.db.models._enums import TransactionStatus, TransactionType
from app.db.models.wallet import Wallet
from app.db.models.user import User


def _seed_payment(db) -> Payment:
    user = User(
        id=uuid.uuid4(),
        email=f"race-{uuid.uuid4().hex[:6]}@t.co",
        phone=f"+234{uuid.uuid4().int % 10**10:010d}",
        full_name="Race Test",
        password_hash="x",
        is_active=True,
    )
    db.add(user)
    db.flush()

    wallet = Wallet(id=uuid.uuid4(), user_id=user.id, balance=0, balance_cap=50000)
    tx = Transaction(
        id=uuid.uuid4(),
        user_id=user.id,
        reference=f"TMP-TEST-{uuid.uuid4().hex[:6]}",
        type=TransactionType.wallet_funding,
        status=TransactionStatus.processing,
        amount=1000,
        fee=0,
        currency="NGN",
    )
    db.add_all([wallet, tx])
    db.flush()

    payment = Payment(
        id=uuid.uuid4(),
        transaction_id=tx.id,
        provider="paystack",
        provider_reference=f"ref_{uuid.uuid4().hex[:8]}",
        status=PaymentStatus.pending,
    )
    db.add(payment)
    db.commit()
    return payment


def test_claim_payment_pending_to_success_succeeds_once(db_session):
    """First caller wins, second caller sees status != pending and returns False."""
    from app.workers.tasks.reconcile_tasks import _claim_payment

    payment = _seed_payment(db_session)

    first = _claim_payment(db_session, payment.id, PaymentStatus.success)
    assert first is True
    db_session.commit()

    second = _claim_payment(db_session, payment.id, PaymentStatus.success)
    assert second is False


def test_claim_payment_webhook_and_reconcile_use_same_guard(db_session):
    """Webhook and reconcile both call _claim_payment — only one mutation lands."""
    from app.api.v1.endpoints.webhooks import _claim_payment as webhook_claim
    from app.workers.tasks.reconcile_tasks import _claim_payment as reconcile_claim

    payment = _seed_payment(db_session)

    # Webhook wins the race
    assert webhook_claim(db_session, payment.id, PaymentStatus.success) is True
    db_session.commit()

    # Reconcile arrives later, must bail
    assert reconcile_claim(db_session, payment.id, PaymentStatus.success) is False

    db_session.refresh(payment)
    assert payment.status == PaymentStatus.success


def test_claim_payment_failed_path_is_also_guarded(db_session):
    """Failed branch has the same lock-and-claim guarantee."""
    from app.workers.tasks.reconcile_tasks import _claim_payment

    payment = _seed_payment(db_session)

    assert _claim_payment(db_session, payment.id, PaymentStatus.failed) is True
    db_session.commit()
    assert _claim_payment(db_session, payment.id, PaymentStatus.success) is False

    db_session.refresh(payment)
    assert payment.status == PaymentStatus.failed
