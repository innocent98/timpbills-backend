import uuid
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
