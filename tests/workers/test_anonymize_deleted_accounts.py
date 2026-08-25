import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from app.db.models.user import User
from app.db.models.wallet import Wallet
from app.db.models.transaction import Transaction
from app.db.models._enums import TransactionStatus, TransactionType
from app.db.models.push_token import PushToken


def _deleted_user(db, *, days_ago, email="d@e.co", phone="+2348100000031",
                  referral_code="REFCODE1"):
    u = User(id=uuid.uuid4(), email=email, phone=phone, full_name="Real Name",
             password_hash="x", is_active=False, referral_code=referral_code,
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
    assert fresh.referral_code != "REFCODE1" and fresh.referral_code is not None
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


def test_nonzero_balance_is_skipped_not_anonymized(db_session):
    # Money can land via the still-live DVA during the grace window (the
    # funding webhook doesn't check account state). Scrubbing this user
    # would orphan the funds under a dead identity — the sweep must skip.
    u = _deleted_user(db_session, days_ago=31, email="rich@y.co",
                       phone="+2348100000034", referral_code="REFCODE2")
    wallet = db_session.query(Wallet).filter(Wallet.user_id == u.id).one()
    wallet.balance = Decimal("500.00")
    db_session.commit()

    result = _run(db_session)

    db_session.expire_all()
    fresh = db_session.query(User).filter(User.id == u.id).one()
    assert fresh.anonymized_at is None
    assert fresh.email == "rich@y.co"
    assert fresh.full_name == "Real Name"
    assert fresh.referral_code == "REFCODE2"
    # Wallet + its balance kept exactly as-is.
    fresh_wallet = db_session.query(Wallet).filter(Wallet.user_id == u.id).one()
    assert fresh_wallet.balance == Decimal("500.00")
    assert result["anonymized"] == 0


def test_cancelled_deletion_between_select_and_lock_is_not_scrubbed(db_session):
    # Simulates the cancel_deletion-vs-sweep race: the row IS in the
    # initial batch selection (deleted_at <= cutoff), but by the time the
    # per-row FOR UPDATE lock is taken, deleted_at has been cleared by a
    # concurrent cancel_deletion() commit. The per-row re-check must
    # honour the fresh, locked state rather than the stale batch snapshot.
    u = _deleted_user(db_session, days_ago=31, email="cancelled@y.co",
                       phone="+2348100000035")

    orig_query = db_session.query
    call_count = {"n": 0}

    def _query_with_race(*args, **kwargs):
        if args and args[0] is User:
            call_count["n"] += 1
            if call_count["n"] == 2:
                # This is the per-row lock query for u. Simulate
                # cancel_deletion() having committed in between.
                db_session.execute(
                    User.__table__.update()
                    .where(User.id == u.id)
                    .values(deleted_at=None, is_active=True)
                )
                db_session.commit()
        return orig_query(*args, **kwargs)

    db_session.query = _query_with_race
    try:
        result = _run(db_session)
    finally:
        db_session.query = orig_query

    db_session.expire_all()
    fresh = db_session.query(User).filter(User.id == u.id).one()
    assert fresh.anonymized_at is None
    assert fresh.email == "cancelled@y.co"
    assert fresh.is_active is True
    assert result["anonymized"] == 0
