# tests/services/test_wallet_service.py
from decimal import Decimal
from uuid import uuid4

import pytest

from app.db.models.user import KycLevel, User
from app.db.models.wallet import Wallet
from app.services.wallet_service import (
    KycCapExceeded,
    InsufficientBalance,
    WalletService,
)


def _seed_user(db, kyc=KycLevel.tier_0) -> User:
    u = User(
        email=f"{uuid4().hex[:8]}@t.co",
        phone=f"+23480{uuid4().int % 10**9:09d}",
        full_name="T U",
        password_hash="x",
        kyc_level=kyc,
        email_verified=True,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def test_get_or_create_wallet_idempotent(db_session):
    u = _seed_user(db_session)
    svc = WalletService(db=db_session)
    w1 = svc.get_or_create(user_id=u.id)
    w2 = svc.get_or_create(user_id=u.id)
    assert w1.id == w2.id
    assert w1.balance == Decimal("0.00")


def test_credit_increases_balance(db_session):
    u = _seed_user(db_session)
    svc = WalletService(db=db_session)
    svc.get_or_create(user_id=u.id)
    new_balance = svc.credit(user_id=u.id, amount=Decimal("1000.00"))
    assert new_balance == Decimal("1000.00")


def test_debit_decreases_balance(db_session):
    u = _seed_user(db_session)
    svc = WalletService(db=db_session)
    svc.get_or_create(user_id=u.id)
    svc.credit(user_id=u.id, amount=Decimal("1000.00"))
    new = svc.debit(user_id=u.id, amount=Decimal("300.00"))
    assert new == Decimal("700.00")


def test_debit_rejects_insufficient(db_session):
    u = _seed_user(db_session)
    svc = WalletService(db=db_session)
    svc.get_or_create(user_id=u.id)
    with pytest.raises(InsufficientBalance):
        svc.debit(user_id=u.id, amount=Decimal("10.00"))


def test_credit_blocked_by_kyc_cap(db_session):
    # Tier 0 cap is 50,000 naira per our config defaults
    u = _seed_user(db_session, kyc=KycLevel.tier_0)
    svc = WalletService(db=db_session)
    svc.get_or_create(user_id=u.id)
    svc.credit(user_id=u.id, amount=Decimal("40000.00"))
    with pytest.raises(KycCapExceeded):
        svc.credit(user_id=u.id, amount=Decimal("20000.00"))
