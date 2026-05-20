"""TransactionService.create_refund — new refund tx linked to original."""
import uuid
from decimal import Decimal


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

    refund, was_created = svc.create_refund(
        original_tx=original,
        amount=Decimal('1000.00'),
        reason='paystack.charge.failed',
    )
    db_session.commit()

    assert was_created is True
    assert refund.type == TransactionType.refund
    assert refund.status == TransactionStatus.success
    assert refund.amount == Decimal('1000.00')
    assert refund.user_id == user.id
    # Refund marker "TMPR" appears after the 12-digit YYYYMMDDHHMI stamp
    # so the reference stays VTPass-compliant if it ever ends up in a
    # requery call. See app/utils/references.py.
    assert refund.reference[12:].startswith('TMPR')
    assert refund.meta['original_reference'] == original.reference
    assert refund.meta['original_type'] == TransactionType.airtime.value


def test_create_refund_is_idempotent_by_original_reference(db_session):
    user = _seed_user(db_session)
    original = _seed_failed_tx(db_session, user.id)
    svc = TransactionService(db=db_session)

    first, first_created = svc.create_refund(
        original_tx=original, amount=Decimal('1000.00'), reason='r1',
    )
    db_session.commit()
    second, second_created = svc.create_refund(
        original_tx=original, amount=Decimal('1000.00'), reason='r2',
    )
    db_session.commit()

    # Same refund returned — no second row created.
    assert first.id == second.id
    # And the was_created flag flips False on the second call so callers
    # can gate their wallet credits on it. See S3C-P1.
    assert first_created is True
    assert second_created is False

    count = db_session.query(Transaction).filter(
        Transaction.type == TransactionType.refund,
        Transaction.user_id == user.id,
    ).count()
    assert count == 1


def test_create_refund_was_created_false_even_when_same_reason(db_session):
    """S3C-P1 regression — two callers with identical `reason` strings
    must still see was_created=False on the second call. The old
    event-list heuristic (len(events) == 1 AND any event.reason matches)
    silently returned True for both, enabling a double-credit race."""
    user = _seed_user(db_session)
    original = _seed_failed_tx(db_session, user.id)
    svc = TransactionService(db=db_session)

    # Both calls use the SAME reason — this is exactly the race pattern
    # the old code got wrong (sync + webhook both pass "provider_failed_code_016").
    _, first_created = svc.create_refund(
        original_tx=original, amount=Decimal('1000.00'),
        reason='provider_failed_code_016',
    )
    db_session.commit()
    _, second_created = svc.create_refund(
        original_tx=original, amount=Decimal('1000.00'),
        reason='provider_failed_code_016',
    )
    db_session.commit()

    assert first_created is True
    assert second_created is False


def test_create_refund_records_event(db_session):
    user = _seed_user(db_session)
    original = _seed_failed_tx(db_session, user.id)
    svc = TransactionService(db=db_session)

    refund, _ = svc.create_refund(
        original_tx=original,
        amount=Decimal('1000.00'),
        reason='paystack.charge.failed',
    )
    db_session.commit()

    events = svc.events_for(refund)
    assert len(events) >= 1
    assert any(e.reason == 'paystack.charge.failed' for e in events)
