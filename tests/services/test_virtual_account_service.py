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


import pytest

from app.integrations.paystack.fake import FakePaystackClient
from app.services.virtual_account_service import (
    KycRequired,
    VirtualAccountService,
    split_full_name,
)


def test_split_full_name_variants():
    assert split_full_name("Ada Grace Obi") == ("Ada", "Grace", "Obi")
    assert split_full_name("Ada Obi") == ("Ada", "", "Obi")
    assert split_full_name("Ada Grace Mary Obi") == ("Ada", "Grace Mary", "Obi")
    assert split_full_name("Ada") == ("Ada", "", "Ada")  # single-token fallback


@pytest.mark.asyncio
async def test_provision_requires_kyc_tier_1(db_session):
    u = _seed_user(db_session, kyc=KycLevel.tier_0)
    svc = VirtualAccountService(db=db_session, paystack=FakePaystackClient())
    with pytest.raises(KycRequired):
        await svc.provision(
            user=u, bvn="22222222222", account_number="0123456789", bank_code="035"
        )


@pytest.mark.asyncio
async def test_provision_creates_pending_identity_row_and_assigns(db_session):
    u = _seed_user(db_session, kyc=KycLevel.tier_1)
    fake = FakePaystackClient()
    svc = VirtualAccountService(db=db_session, paystack=fake)
    va = await svc.provision(
        user=u, bvn="22222222222", account_number="0123456789", bank_code="035"
    )
    assert va.status == VirtualAccountStatus.pending_identity
    assert va.paystack_customer_code.startswith("CUS_")
    assert len(fake.assigned) == 1


@pytest.mark.asyncio
async def test_provision_is_idempotent(db_session):
    u = _seed_user(db_session, kyc=KycLevel.tier_1)
    fake = FakePaystackClient()
    svc = VirtualAccountService(db=db_session, paystack=fake)
    va1 = await svc.provision(
        user=u, bvn="22222222222", account_number="0123456789", bank_code="035"
    )
    va2 = await svc.provision(
        user=u, bvn="22222222222", account_number="0123456789", bank_code="035"
    )
    assert va1.id == va2.id
    rows = db_session.query(VirtualAccount).filter(VirtualAccount.user_id == u.id).all()
    assert len(rows) == 1
    assert len(fake.assigned) == 1  # second call returns existing, no re-assign


@pytest.mark.asyncio
async def test_provision_recovers_when_assign_throws(db_session):
    u = _seed_user(db_session, kyc=KycLevel.tier_1)
    fake = FakePaystackClient()
    svc = VirtualAccountService(db=db_session, paystack=fake)
    fake.will_raise_on_assign()

    with pytest.raises(Exception):
        await svc.provision(
            user=u, bvn="22222222222", account_number="0123456789", bank_code="035"
        )

    row = (
        db_session.query(VirtualAccount)
        .filter(VirtualAccount.user_id == u.id)
        .one()
    )
    assert row.status == VirtualAccountStatus.failed
    assert row.failure_reason is not None

    # Retry: assign now succeeds, row recovers to pending_identity.
    va2 = await svc.provision(
        user=u, bvn="22222222222", account_number="0123456789", bank_code="035"
    )
    assert va2.status == VirtualAccountStatus.pending_identity
    assert va2.id == row.id
    assert len(fake.assigned) == 1
