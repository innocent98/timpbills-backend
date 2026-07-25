import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from app.db.models.user import User
from app.db.models.wallet import Wallet
from app.db.models.transaction import Transaction
from app.db.models._enums import TransactionStatus, TransactionType
from app.db.models.push_token import PushToken


def _deleted_user(db, *, days_ago, email="d@e.co", phone="+2348100000031"):
    u = User(id=uuid.uuid4(), email=email, phone=phone, full_name="Real Name",
             password_hash="x", is_active=False,
             deleted_at=datetime.now(UTC) - timedelta(days=days_ago))
    db.add(u); db.flush()
    db.add(Wallet(id=uuid.uuid4(), user_id=u.id, balance=Decimal("0.00"),
                  balance_cap=Decimal("50000.00")))
    db.add(Transaction(id=uuid.uuid4(), user_id=u.id, reference=f"T-{uuid.uuid4().hex[:8]}",
                       type=TransactionType.wallet_funding, status=TransactionStatus.success,
                       amount=Decimal("100.00"), fee=Decimal("0.00"), currency="NGN", meta={}))
    db.add(PushToken(id=uuid.uuid4(), user_id=u.id, fcm_token=f"tok-{uuid.uuid4().hex[:8]}",
                     platform="ios"))
    db.commit()
    return u


def _run(db):
    from app.workers.tasks import account_tasks as at
    orig = db.close
    db.close = lambda: None
    try:
        with patch.object(at, "SessionLocal", lambda: db):
            return at.anonymize_deleted_accounts()
    finally:
        db.close = orig


def test_over_grace_is_anonymized_ledger_kept(db_session):
    u = _deleted_user(db_session, days_ago=31)
    _run(db_session)
    db_session.expire_all()
    fresh = db_session.query(User).filter(User.id == u.id).one()
    assert fresh.anonymized_at is not None
    assert fresh.full_name == "Deleted User"
    assert fresh.email != "d@e.co" and "deleted" in fresh.email
    assert fresh.phone != "+2348100000031"
    # ledger kept
    assert db_session.query(Transaction).filter(Transaction.user_id == u.id).count() == 1
    assert db_session.query(Wallet).filter(Wallet.user_id == u.id).count() == 1
    # PII child rows gone
    assert db_session.query(PushToken).filter(PushToken.user_id == u.id).count() == 0


def test_inside_grace_untouched(db_session):
    u = _deleted_user(db_session, days_ago=5, email="x@y.co", phone="+2348100000032")
    _run(db_session)
    db_session.expire_all()
    fresh = db_session.query(User).filter(User.id == u.id).one()
    assert fresh.anonymized_at is None and fresh.email == "x@y.co"


def test_rerun_is_noop(db_session):
    u = _deleted_user(db_session, days_ago=31, email="z@y.co", phone="+2348100000033")
    _run(db_session)
    db_session.expire_all()
    first = db_session.query(User).filter(User.id == u.id).one().anonymized_at
    _run(db_session)
    db_session.expire_all()
    assert db_session.query(User).filter(User.id == u.id).one().anonymized_at == first
