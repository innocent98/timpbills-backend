# tests/services/test_kyc_service.py
"""KycService: start_verification (tier gate + DOB) and confirm_verification
(idempotent validation matrix + tier upgrade). Uses FakeKycProvider via
FORCE_FAKE_PROVIDERS (autouse fixture in tests/conftest.py)."""
from datetime import date
from uuid import uuid4

import pytest

from app.db.models.kyc_record import KycRecord
from app.db.models.user import KycLevel, User
from app.integrations.dojah.schemas import KycVerificationResult
from app.services.kyc_service import (
    DobRequired,
    KycProviderError,
    KycService,
    KycTierPrecondition,
    UnknownReference,
)


def _seed_user(db, kyc=KycLevel.tier_0, dob=None) -> User:
    u = User(
        email=f"{uuid4().hex[:8]}@t.co",
        phone=f"+23480{uuid4().int % 10**9:09d}",
        full_name="T U",
        password_hash="x",
        kyc_level=kyc,
        email_verified=True,
        date_of_birth=dob,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _seed_record(db, *, user, reference_id, verification_type="bvn") -> KycRecord:
    record = KycRecord(
        user_id=user.id,
        verification_type=verification_type,
        provider="dojah",
        provider_reference=reference_id,
        status="pending",
        tier_before=user.kyc_level.numeric,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


class TestStartVerification:
    def test_wrong_tier_raises_kyc_tier_precondition(self, db_session):
        user = _seed_user(db_session, kyc=KycLevel.tier_0, dob=date(1990, 1, 1))
        svc = KycService(db=db_session)
        with pytest.raises(KycTierPrecondition):
            svc.start_verification(user=user, verification_type="bvn")

    def test_nin_requires_tier2(self, db_session):
        user = _seed_user(db_session, kyc=KycLevel.tier_1, dob=date(1990, 1, 1))
        svc = KycService(db=db_session)
        with pytest.raises(KycTierPrecondition):
            svc.start_verification(user=user, verification_type="nin")

    def test_tier1_bvn_mints_pending_record(self, db_session):
        user = _seed_user(db_session, kyc=KycLevel.tier_1, dob=date(1990, 1, 1))
        svc = KycService(db=db_session)
        reference_id = svc.start_verification(user=user, verification_type="bvn")

        assert reference_id.startswith("KYC-BVN-")
        record = (
            db_session.query(KycRecord)
            .filter(KycRecord.provider_reference == reference_id)
            .one()
        )
        assert record.status == "pending"
        assert record.tier_before == 1
        assert record.verification_type == "bvn"
        assert record.provider == "dojah"
        assert record.masked_id is None

    def test_unsupported_verification_type_raises_value_error(self, db_session):
        user = _seed_user(db_session, kyc=KycLevel.tier_1, dob=date(1990, 1, 1))
        svc = KycService(db=db_session)
        with pytest.raises(ValueError):
            svc.start_verification(user=user, verification_type="passport")

    def test_dob_required_when_missing_and_none_supplied(self, db_session):
        user = _seed_user(db_session, kyc=KycLevel.tier_1, dob=None)
        svc = KycService(db=db_session)
        with pytest.raises(DobRequired):
            svc.start_verification(user=user, verification_type="bvn")

    def test_dob_supplied_is_persisted_on_user(self, db_session):
        user = _seed_user(db_session, kyc=KycLevel.tier_1, dob=None)
        svc = KycService(db=db_session)
        svc.start_verification(
            user=user, verification_type="bvn", date_of_birth=date(1995, 5, 5),
        )
        db_session.refresh(user)
        assert user.date_of_birth == date(1995, 5, 5)

    def test_dob_already_on_file_is_used_as_is(self, db_session):
        user = _seed_user(db_session, kyc=KycLevel.tier_1, dob=date(1988, 3, 3))
        svc = KycService(db=db_session)
        svc.start_verification(user=user, verification_type="bvn")
        db_session.refresh(user)
        assert user.date_of_birth == date(1988, 3, 3)


class TestConfirmVerification:
    @pytest.mark.asyncio
    async def test_confirm_pass_upgrades_bvn_to_tier2(self, db_session):
        user = _seed_user(db_session, kyc=KycLevel.tier_1, dob=date(1990, 1, 1))
        _seed_record(db_session, user=user, reference_id="PASS-BVN-1")

        svc = KycService(db=db_session)
        record = await svc.confirm_verification(reference_id="PASS-BVN-1")

        assert record.status == "success"
        assert record.tier_after == 2
        assert record.failure_reason is None
        db_session.refresh(user)
        assert user.kyc_level == KycLevel.tier_2

    @pytest.mark.asyncio
    async def test_confirm_pass_upgrades_nin_to_tier3(self, db_session):
        user = _seed_user(db_session, kyc=KycLevel.tier_2, dob=date(1990, 1, 1))
        _seed_record(
            db_session, user=user, reference_id="PASS-NIN-1", verification_type="nin",
        )

        svc = KycService(db=db_session)
        record = await svc.confirm_verification(reference_id="PASS-NIN-1")

        assert record.status == "success"
        assert record.tier_after == 3
        db_session.refresh(user)
        assert user.kyc_level == KycLevel.tier_3

    @pytest.mark.asyncio
    async def test_confirm_face_fail_keeps_tier_unchanged(self, db_session):
        user = _seed_user(db_session, kyc=KycLevel.tier_1, dob=date(1990, 1, 1))
        _seed_record(db_session, user=user, reference_id="FAILFACE-BVN-1")

        svc = KycService(db=db_session)
        record = await svc.confirm_verification(reference_id="FAILFACE-BVN-1")

        assert record.status == "failed"
        assert record.failure_reason == "face_mismatch"
        assert record.tier_after is None
        assert record.face_match is False
        db_session.refresh(user)
        assert user.kyc_level == KycLevel.tier_1

    @pytest.mark.asyncio
    async def test_confirm_liveness_fail_reason(self, db_session):
        user = _seed_user(db_session, kyc=KycLevel.tier_1, dob=date(1990, 1, 1))
        _seed_record(db_session, user=user, reference_id="FAILLIVE-BVN-1")

        svc = KycService(db=db_session)
        record = await svc.confirm_verification(reference_id="FAILLIVE-BVN-1")

        assert record.status == "failed"
        assert record.failure_reason == "liveness_failed"
        db_session.refresh(user)
        assert user.kyc_level == KycLevel.tier_1

    @pytest.mark.asyncio
    async def test_confirm_pending_result_leaves_record_pending(self, db_session):
        user = _seed_user(db_session, kyc=KycLevel.tier_1, dob=date(1990, 1, 1))
        _seed_record(db_session, user=user, reference_id="PENDING-BVN-1")

        svc = KycService(db=db_session)
        record = await svc.confirm_verification(reference_id="PENDING-BVN-1")

        assert record.status == "pending"
        assert record.failure_reason is None
        db_session.refresh(user)
        assert user.kyc_level == KycLevel.tier_1

    @pytest.mark.asyncio
    async def test_confirm_is_idempotent_on_success(self, db_session, monkeypatch):
        user = _seed_user(db_session, kyc=KycLevel.tier_1, dob=date(1990, 1, 1))
        _seed_record(db_session, user=user, reference_id="PASS-BVN-2")

        svc = KycService(db=db_session)
        first = await svc.confirm_verification(reference_id="PASS-BVN-2")
        assert first.status == "success"
        db_session.refresh(user)
        assert user.kyc_level == KycLevel.tier_2

        # Second confirm (e.g. the webhook arriving after the api-confirm
        # already applied) must short-circuit on the already-success record
        # WITHOUT calling the provider again.
        calls = []
        import app.services.kyc_service as kyc_service_module

        original_get_provider = kyc_service_module.get_kyc_provider

        def _spy_get_provider():
            provider = original_get_provider()
            original_fetch = provider.fetch_verification

            async def _wrapped(*, reference_id):
                calls.append(reference_id)
                return await original_fetch(reference_id=reference_id)

            provider.fetch_verification = _wrapped
            return provider

        monkeypatch.setattr(kyc_service_module, "get_kyc_provider", _spy_get_provider)

        second = await svc.confirm_verification(reference_id="PASS-BVN-2")
        assert second.status == "success"
        assert calls == []  # no-op: provider never re-invoked
        db_session.refresh(user)
        assert user.kyc_level == KycLevel.tier_2  # not bumped again

    @pytest.mark.asyncio
    async def test_confirm_unknown_reference_raises(self, db_session):
        svc = KycService(db=db_session)
        with pytest.raises(UnknownReference):
            await svc.confirm_verification(reference_id="KYC-BVN-doesnotexist")

    @pytest.mark.asyncio
    async def test_confirm_minted_reference_succeeds_end_to_end(self, db_session):
        """start_verification's minted reference round-trips through the
        fake provider's default-success branch (prerequisite fake tweak)."""
        user = _seed_user(db_session, kyc=KycLevel.tier_1, dob=date(1990, 1, 1))
        svc = KycService(db=db_session)
        reference_id = svc.start_verification(user=user, verification_type="bvn")

        record = await svc.confirm_verification(reference_id=reference_id)

        assert record.status == "success"
        db_session.refresh(user)
        assert user.kyc_level == KycLevel.tier_2

    @pytest.mark.asyncio
    async def test_confirm_provider_error_leaves_record_pending(
        self, db_session, monkeypatch,
    ):
        user = _seed_user(db_session, kyc=KycLevel.tier_1, dob=date(1990, 1, 1))
        _seed_record(db_session, user=user, reference_id="PASS-BVN-3")

        import app.services.kyc_service as kyc_service_module

        class _BoomProvider:
            async def fetch_verification(self, *, reference_id):
                raise RuntimeError("dojah unreachable")

        monkeypatch.setattr(
            kyc_service_module, "get_kyc_provider", lambda: _BoomProvider(),
        )

        svc = KycService(db=db_session)
        with pytest.raises(KycProviderError):
            await svc.confirm_verification(reference_id="PASS-BVN-3")

        record = (
            db_session.query(KycRecord)
            .filter(KycRecord.provider_reference == "PASS-BVN-3")
            .one()
        )
        assert record.status == "pending"
        db_session.refresh(user)
        assert user.kyc_level == KycLevel.tier_1

    @pytest.mark.asyncio
    async def test_confirm_identity_dob_mismatch_fails(self, db_session, monkeypatch):
        user = _seed_user(db_session, kyc=KycLevel.tier_1, dob=date(1990, 1, 1))
        _seed_record(db_session, user=user, reference_id="PASS-BVN-4")

        import app.services.kyc_service as kyc_service_module

        class _MismatchProvider:
            async def fetch_verification(self, *, reference_id):
                return KycVerificationResult(
                    verification_type="bvn",
                    status="success",
                    id_verified=True,
                    liveness_passed=True,
                    face_match=True,
                    face_match_confidence=95,
                    masked_id="•••••••••99",
                    provider_reference=reference_id,
                    identity_dob=date(2000, 1, 1),
                    failure_reason=None,
                )

        monkeypatch.setattr(
            kyc_service_module, "get_kyc_provider", lambda: _MismatchProvider(),
        )

        svc = KycService(db=db_session)
        record = await svc.confirm_verification(reference_id="PASS-BVN-4")

        assert record.status == "failed"
        assert record.failure_reason == "identity_mismatch"
        db_session.refresh(user)
        assert user.kyc_level == KycLevel.tier_1

    @pytest.mark.asyncio
    async def test_confirm_absent_identity_dob_does_not_fail(self, db_session):
        """The fake never populates identity_dob — absent identity data must
        NOT fail the match (lenient identity check)."""
        user = _seed_user(db_session, kyc=KycLevel.tier_1, dob=date(1990, 1, 1))
        _seed_record(db_session, user=user, reference_id="PASS-BVN-5")

        svc = KycService(db=db_session)
        record = await svc.confirm_verification(reference_id="PASS-BVN-5")

        assert record.status == "success"
