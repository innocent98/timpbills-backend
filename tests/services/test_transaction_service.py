# tests/services/test_transaction_service.py
from decimal import Decimal
from uuid import uuid4

import pytest

from app.db.models._enums import TransactionStatus, TransactionType
from app.db.models.transaction import Transaction
from app.services.transaction_service import (
    InvalidStateTransition,
    TransactionService,
)


def _user_id() -> str:
    return str(uuid4())


def test_create_pending_transaction(db_session):
    svc = TransactionService(db=db_session)
    uid = _user_id()
    tx = svc.create(
        user_id=uid,
        type=TransactionType.wallet_funding,
        amount=Decimal("5000.00"),
        fee=Decimal("50.00"),
    )
    assert tx.status == TransactionStatus.pending
    assert tx.reference.startswith("TMP-")
    assert tx.amount == Decimal("5000.00")


def test_transition_logs_event(db_session):
    svc = TransactionService(db=db_session)
    uid = _user_id()
    tx = svc.create(
        user_id=uid,
        type=TransactionType.wallet_funding,
        amount=Decimal("100.00"),
    )
    svc.transition(tx, to_status=TransactionStatus.processing, reason="paystack_init")
    # Reload
    db_session.refresh(tx)
    assert tx.status == TransactionStatus.processing
    events = svc.events_for(tx.id)
    assert len(events) == 1
    assert events[0].from_status == TransactionStatus.pending
    assert events[0].to_status == TransactionStatus.processing


def test_illegal_transition_raises(db_session):
    svc = TransactionService(db=db_session)
    tx = svc.create(
        user_id=_user_id(),
        type=TransactionType.wallet_funding,
        amount=Decimal("100.00"),
    )
    # pending → refunded is not allowed (must go through success first)
    with pytest.raises(InvalidStateTransition):
        svc.transition(tx, to_status=TransactionStatus.refunded)


def test_same_status_is_noop(db_session):
    svc = TransactionService(db=db_session)
    tx = svc.create(
        user_id=_user_id(),
        type=TransactionType.wallet_funding,
        amount=Decimal("100.00"),
    )
    svc.transition(tx, to_status=TransactionStatus.pending)  # idempotent
    assert svc.events_for(tx.id) == []
