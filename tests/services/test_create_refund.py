"""TransactionService.create_refund — new refund tx linked to original."""
import uuid
from decimal import Decimal

import pytest

from app.db.models._enums import TransactionStatus, TransactionType
from app.db.models.transaction import Transaction
from app.db.models.user import User
from app.services.transaction_service import TransactionService


def _seed_user(db) -> User:
    user = User(
        id=uuid.uuid4(),
        email=f"ref-{uuid.uuid4().hex[:6]}@t.co",
        phone=f"+234{uuid.uuid4().int % 10**10:010d}",
        full_name="Refund Test",
        password_hash="x",
        is_active=True,
    )
    db.add(user)
    db.commit()
    return user


def _seed_failed_tx(db, user_id) -> Transaction:
    tx = Transaction(
        id=uuid.uuid4(),
        user_id=user_id,
        reference=f"TMP-FAIL-{uuid.uuid4().hex[:6]}",
        type=TransactionType.airtime,
        status=TransactionStatus.failed,
        amount=Decimal('1000.00'),
        fee=Decimal('0'),
        currency='NGN',
    )
    db.add(tx)
    db.commit()
    return tx


def test_create_refund_creates_new_transaction_linked_to_original(db_session):
    user = _seed_user(db_session)
    original = _seed_failed_tx(db_session, user.id)
    svc = TransactionService(db=db_session)

    refund = svc.create_refund(
        original_tx=original,
        amount=Decimal('1000.00'),
        reason='paystack.charge.failed',
    )
    db_session.commit()

    assert refund.type == TransactionType.refund
    assert refund.status == TransactionStatus.success
    assert refund.amount == Decimal('1000.00')
    assert refund.user_id == user.id
    assert refund.reference.startswith('TMP-R-')
    assert refund.meta['original_reference'] == original.reference
    assert refund.meta['original_type'] == TransactionType.airtime.value


def test_create_refund_is_idempotent_by_original_reference(db_session):
    user = _seed_user(db_session)
    original = _seed_failed_tx(db_session, user.id)
    svc = TransactionService(db=db_session)

    first = svc.create_refund(original_tx=original, amount=Decimal('1000.00'), reason='r1')
    db_session.commit()
    second = svc.create_refund(original_tx=original, amount=Decimal('1000.00'), reason='r2')
    db_session.commit()

    # Same refund returned — no second row created.
    assert first.id == second.id

    count = db_session.query(Transaction).filter(
        Transaction.type == TransactionType.refund,
        Transaction.user_id == user.id,
    ).count()
    assert count == 1


def test_create_refund_records_event(db_session):
    user = _seed_user(db_session)
    original = _seed_failed_tx(db_session, user.id)
    svc = TransactionService(db=db_session)

    refund = svc.create_refund(
        original_tx=original,
        amount=Decimal('1000.00'),
        reason='paystack.charge.failed',
    )
    db_session.commit()

    events = svc.events_for(refund)
    assert len(events) >= 1
    assert any(e.reason == 'paystack.charge.failed' for e in events)
