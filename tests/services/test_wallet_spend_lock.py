from decimal import Decimal
from uuid import uuid4

import pytest

from app.db.models._enums import SpendLockReason
from app.db.models.user import KycLevel, User
from app.db.models.wallet import Wallet
from app.services.wallet_service import (
    KycCapExceeded,
    OverCapPolicy,
    WalletService,
    WalletSpendLocked,
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


def test_credit_raise_policy_unchanged(db_session):
    u = _seed_user(db_session, kyc=KycLevel.tier_0)  # 50,000 cap
    svc = WalletService(db=db_session)
    svc.get_or_create(user_id=u.id)
    svc.credit(user_id=u.id, amount=Decimal("40000.00"))
    with pytest.raises(KycCapExceeded):
        svc.credit(user_id=u.id, amount=Decimal("20000.00"))


def test_credit_lock_policy_credits_full_and_locks(db_session):
    u = _seed_user(db_session, kyc=KycLevel.tier_0)  # 50,000 cap
    svc = WalletService(db=db_session)
    svc.get_or_create(user_id=u.id)
    new_balance = svc.credit(
        user_id=u.id, amount=Decimal("70000.00"), over_cap=OverCapPolicy.LOCK
    )
    assert new_balance == Decimal("70000.00")  # gross, never rejected
    w = db_session.query(Wallet).filter(Wallet.user_id == u.id).one()
    assert w.spend_locked is True
    assert w.spend_locked_reason == SpendLockReason.over_cap


def test_raise_if_spend_locked(db_session):
    u = _seed_user(db_session)
    svc = WalletService(db=db_session)
    w = svc.get_or_create(user_id=u.id)
    svc.raise_if_spend_locked(user_id=u.id)  # not locked -> no raise
    w.spend_locked = True
    w.spend_locked_reason = SpendLockReason.over_cap
    db_session.commit()
    with pytest.raises(WalletSpendLocked):
        svc.raise_if_spend_locked(user_id=u.id)


def test_clear_spend_lock_when_within_new_cap(db_session):
    u = _seed_user(db_session, kyc=KycLevel.tier_0)
    svc = WalletService(db=db_session)
    svc.get_or_create(user_id=u.id)
    svc.credit(user_id=u.id, amount=Decimal("70000.00"), over_cap=OverCapPolicy.LOCK)
    # Simulate the tier upgrade to tier_1 (300,000 cap) that now covers 70,000.
    u.kyc_level = KycLevel.tier_1
    db_session.commit()
    cleared = svc.clear_spend_lock_if_within_cap(user_id=u.id)
    assert cleared is True
    w = db_session.query(Wallet).filter(Wallet.user_id == u.id).one()
    assert w.spend_locked is False
    assert w.spend_locked_reason is None


def test_clear_spend_lock_leaves_locked_when_still_over(db_session):
    u = _seed_user(db_session, kyc=KycLevel.tier_0)
    svc = WalletService(db=db_session)
    svc.get_or_create(user_id=u.id)
    svc.credit(user_id=u.id, amount=Decimal("400000.00"), over_cap=OverCapPolicy.LOCK)
    u.kyc_level = KycLevel.tier_1  # 300,000 cap still below 400,000
    db_session.commit()
    assert svc.clear_spend_lock_if_within_cap(user_id=u.id) is False
    w = db_session.query(Wallet).filter(Wallet.user_id == u.id).one()
    assert w.spend_locked is True
