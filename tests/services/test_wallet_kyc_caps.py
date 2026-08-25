# tests/services/test_wallet_kyc_caps.py
"""4-tier KYC cap table (spec §2): tier_0 50k / tier_1 300k / tier_2 500k /
tier_3 unlimited. Only max-balance is enforced here — per-txn/daily caps are
display-only and live elsewhere.
"""
from decimal import Decimal
from uuid import uuid4

import pytest

from app.db.models.user import KycLevel, User
from app.services.wallet_service import KycCapExceeded, WalletService


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


def test_tier1_cap_is_300k(db_session):
    u = _seed_user(db_session, kyc=KycLevel.tier_1)
    svc = WalletService(db=db_session)
    svc.get_or_create(user_id=u.id)

    new_balance = svc.credit(user_id=u.id, amount=Decimal("300000.00"))
    assert new_balance == Decimal("300000.00")

    with pytest.raises(KycCapExceeded):
        svc.credit(user_id=u.id, amount=Decimal("0.01"))


def test_get_or_create_sets_tier1_cap_to_300k(db_session):
    u = _seed_user(db_session, kyc=KycLevel.tier_1)
    svc = WalletService(db=db_session)
    w = svc.get_or_create(user_id=u.id)
    assert w.balance_cap == Decimal("300000.00")


def test_tier3_unlimited(db_session):
    u = _seed_user(db_session, kyc=KycLevel.tier_3)
    svc = WalletService(db=db_session)
    svc.get_or_create(user_id=u.id)

    new_balance = svc.credit(user_id=u.id, amount=Decimal("10000000.00"))
    assert new_balance == Decimal("10000000.00")

    # A second, even larger credit must still never raise.
    new_balance = svc.credit(user_id=u.id, amount=Decimal("50000000.00"))
    assert new_balance == Decimal("60000000.00")


def test_tier3_get_or_create_writes_unlimited_sentinel_not_none(db_session):
    """balance_cap is NOT NULL — tier_3 wallets must get a concrete sentinel,
    never a null/None written to the column."""
    u = _seed_user(db_session, kyc=KycLevel.tier_3)
    svc = WalletService(db=db_session)
    w = svc.get_or_create(user_id=u.id)
    assert w.balance_cap is not None
    assert w.balance_cap > Decimal("10000000.00")
