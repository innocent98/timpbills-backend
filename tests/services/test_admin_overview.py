import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.db.models._enums import TransactionStatus, TransactionType
from app.db.models.transaction import Transaction
from app.services.admin_service import AdminService
from app.utils.references import new_transaction_reference


def _tx(db, user_id, *, type_, status, amount):
    tx = Transaction(
        user_id=user_id, reference=new_transaction_reference(user_id=str(user_id)),
        type=type_, status=status, amount=Decimal(amount), fee=Decimal("0.00"), meta={},
    )
    db.add(tx); db.commit(); return tx


def test_overview_counts_and_success_rate(db_session):
    uid = uuid.uuid4()
    _tx(db_session, uid, type_=TransactionType.airtime, status=TransactionStatus.success, amount="1000")
    _tx(db_session, uid, type_=TransactionType.data, status=TransactionStatus.success, amount="500")
    _tx(db_session, uid, type_=TransactionType.airtime, status=TransactionStatus.failed, amount="200")
    svc = AdminService(db=db_session)
    ov = svc.overview(days=7)
    assert ov["transaction_count"] == 3
    assert ov["success_rate"] == round(2 / 3, 4)
    assert ov["volume_ngn"] == "1500.00"   # successful volume only
    types = {m["type"]: m["pct"] for m in ov["service_mix"]}
    assert set(types) <= {"airtime", "data"}
    assert "daily_volume" in ov
    assert ov["needs_attention"]["refunds_awaiting"] == 0


def _tx_at(db, user_id, *, type_, status, amount, at):
    tx = Transaction(
        user_id=user_id, reference=new_transaction_reference(user_id=str(user_id)),
        type=type_, status=status, amount=Decimal(amount), fee=Decimal("0.00"),
        meta={}, created_at=at,
    )
    db.add(tx); db.commit(); return tx


def test_overview_deltas_compare_prior_window(db_session):
    uid = uuid.uuid4()
    now = datetime.now(UTC)
    prior = now - timedelta(days=10)   # inside [now-14d, now-7d)
    cur = now - timedelta(days=1)      # inside [now-7d, now)
    # prior window: 1 success / 1 failed -> rate 0.5, volume 1000
    _tx_at(db_session, uid, type_=TransactionType.airtime, status=TransactionStatus.success, amount="1000", at=prior)
    _tx_at(db_session, uid, type_=TransactionType.airtime, status=TransactionStatus.failed, amount="500", at=prior)
    # current window: 3 success / 1 failed -> rate 0.75, volume 3000
    _tx_at(db_session, uid, type_=TransactionType.airtime, status=TransactionStatus.success, amount="1000", at=cur)
    _tx_at(db_session, uid, type_=TransactionType.data, status=TransactionStatus.success, amount="1000", at=cur)
    _tx_at(db_session, uid, type_=TransactionType.airtime, status=TransactionStatus.success, amount="1000", at=cur)
    _tx_at(db_session, uid, type_=TransactionType.airtime, status=TransactionStatus.failed, amount="500", at=cur)

    ov = AdminService(db=db_session).overview(days=7)
    # rate 0.75 vs 0.5 -> +25.0 percentage points
    assert ov["deltas"]["success_rate_pp"] == 25.0
    # volume 3000 vs 1000 -> +2.0 (200%) relative fraction
    assert ov["deltas"]["volume_pct"] == 2.0


def test_overview_deltas_refund_total_pct(db_session):
    uid = uuid.uuid4()
    now = datetime.now(UTC)
    _tx_at(db_session, uid, type_=TransactionType.refund, status=TransactionStatus.success, amount="100", at=now - timedelta(days=10))
    _tx_at(db_session, uid, type_=TransactionType.refund, status=TransactionStatus.success, amount="150", at=now - timedelta(days=1))

    ov = AdminService(db=db_session).overview(days=7)
    # refund total 150 vs 100 -> +0.5 (50%)
    assert ov["deltas"]["refund_total_pct"] == 0.5


def test_overview_deltas_null_when_prior_window_empty(db_session):
    uid = uuid.uuid4()
    _tx(db_session, uid, type_=TransactionType.airtime, status=TransactionStatus.success, amount="1000")
    ov = AdminService(db=db_session).overview(days=7)
    assert ov["deltas"]["success_rate_pp"] is None
    assert ov["deltas"]["volume_pct"] is None
    assert ov["deltas"]["refund_total_pct"] is None
