from decimal import Decimal
from uuid import uuid4

import pytest

from app.db.models._enums import SpendLockReason
from app.db.models.kyc_record import KycRecord
from app.db.models.user import KycLevel, User
from app.db.models.wallet import Wallet
from app.services.kyc_service import KycService
from app.services.wallet_service import OverCapPolicy, WalletService


def _seed_user(db, kyc=KycLevel.tier_1) -> User:
    u = User(
        email=f"{uuid4().hex[:8]}@t.co",
        phone=f"+23480{uuid4().int % 10**9:09d}",
        full_name="T U", password_hash="x", kyc_level=kyc, email_verified=True,
        date_of_birth=None,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


@pytest.mark.asyncio
async def test_bvn_pass_clears_spend_lock_when_new_cap_covers_balance(db_session, monkeypatch):
    # User at tier_1 (cap 300,000). Lock the wallet with a 400,000 balance
    # (over the tier_1 cap). A BVN pass upgrades to tier_2 (cap 500,000),
    # which now covers 400,000 -> lock clears.
    u = _seed_user(db_session, kyc=KycLevel.tier_1)
    wsvc = WalletService(db=db_session)
    wsvc.get_or_create(user_id=u.id)
    wsvc.credit(user_id=u.id, amount=Decimal("400000.00"), over_cap=OverCapPolicy.LOCK)

    ref = "KYC-BVN-locktest"
    db_session.add(KycRecord(
        user_id=u.id, verification_type="bvn", provider="dojah",
        provider_reference=ref, status="pending", tier_before=1, masked_id=None,
    ))
    db_session.commit()

    # Stub the Dojah provider to return a clean pass for this reference.
    from app.integrations.dojah.schemas import KycVerificationResult

    class _FakeProvider:
        async def fetch_verification(self, *, reference_id):
            return KycVerificationResult(
                verification_type="bvn", status="success", id_verified=True,
                liveness_passed=True, face_match=True, face_match_confidence=99,
                masked_id="12", provider_reference=reference_id, identity_dob=None,
            )

    import app.services.kyc_service as kyc_mod
    monkeypatch.setattr(kyc_mod, "get_kyc_provider", lambda: _FakeProvider())

    svc = KycService(db=db_session)
    await svc.confirm_verification(reference_id=ref)

    db_session.expire_all()
    w = db_session.query(Wallet).filter(Wallet.user_id == u.id).one()
    assert w.spend_locked is False
    assert w.spend_locked_reason is None


@pytest.mark.asyncio
async def test_bvn_pass_leaves_lock_when_new_cap_still_below_balance(db_session, monkeypatch):
    # User at tier_1 (cap 300,000). Lock the wallet with a 600,000 balance
    # (over both the tier_1 cap AND the tier_2 cap of 500,000). A BVN pass
    # upgrades to tier_2, but the new cap still doesn't cover the balance
    # -> lock stays in place.
    u = _seed_user(db_session, kyc=KycLevel.tier_1)
    wsvc = WalletService(db=db_session)
    wsvc.get_or_create(user_id=u.id)
    wsvc.credit(user_id=u.id, amount=Decimal("600000.00"), over_cap=OverCapPolicy.LOCK)

    ref = "KYC-BVN-stilllockedtest"
    db_session.add(KycRecord(
        user_id=u.id, verification_type="bvn", provider="dojah",
        provider_reference=ref, status="pending", tier_before=1, masked_id=None,
    ))
    db_session.commit()

    from app.integrations.dojah.schemas import KycVerificationResult

    class _FakeProvider:
        async def fetch_verification(self, *, reference_id):
            return KycVerificationResult(
                verification_type="bvn", status="success", id_verified=True,
                liveness_passed=True, face_match=True, face_match_confidence=99,
                masked_id="12", provider_reference=reference_id, identity_dob=None,
            )

    import app.services.kyc_service as kyc_mod
    monkeypatch.setattr(kyc_mod, "get_kyc_provider", lambda: _FakeProvider())

    svc = KycService(db=db_session)
    await svc.confirm_verification(reference_id=ref)

    db_session.expire_all()
    w = db_session.query(Wallet).filter(Wallet.user_id == u.id).one()
    assert w.spend_locked is True
    assert w.spend_locked_reason == SpendLockReason.over_cap
