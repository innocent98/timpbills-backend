from decimal import Decimal
from uuid import uuid4

from app.db.models._enums import SpendLockReason, VirtualAccountStatus
from app.db.models.user import KycLevel, User
from app.db.models.virtual_account import VirtualAccount
from app.db.models.wallet import Wallet


def _seed_user(db, kyc=KycLevel.tier_1) -> User:
    u = User(
        email=f"{uuid4().hex[:8]}@t.co",
        phone=f"+23480{uuid4().int % 10**9:09d}",
        full_name="Ada Grace Obi",
        password_hash="x",
        kyc_level=kyc,
        email_verified=True,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def test_virtual_account_row_persists(db_session):
    u = _seed_user(db_session)
    va = VirtualAccount(
        user_id=u.id,
        paystack_customer_code="CUS_test_1",
        status=VirtualAccountStatus.pending_identity,
        currency="NGN",
    )
    db_session.add(va)
    db_session.commit()
    db_session.refresh(va)
    assert va.status == VirtualAccountStatus.pending_identity
    assert va.account_number is None


def test_wallet_spend_lock_columns_default(db_session):
    u = _seed_user(db_session)
    w = Wallet(user_id=u.id, balance=Decimal("0.00"), balance_cap=Decimal("300000.00"))
    db_session.add(w)
    db_session.commit()
    db_session.refresh(w)
    assert w.spend_locked is False
    assert w.spend_locked_reason is None
    w.spend_locked = True
    w.spend_locked_reason = SpendLockReason.over_cap
    db_session.commit()
    db_session.refresh(w)
    assert w.spend_locked_reason == SpendLockReason.over_cap
