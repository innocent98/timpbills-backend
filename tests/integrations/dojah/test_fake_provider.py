import pytest

from app.integrations.dojah.fake import FakeKycProvider


@pytest.mark.asyncio
async def test_fake_pass_bvn():
    r = await FakeKycProvider().fetch_verification(reference_id="PASS-BVN-123")
    assert r.status == "success" and r.id_verified and r.liveness_passed
    assert r.face_match and r.face_match_confidence >= 70
    assert r.verification_type == "bvn"


@pytest.mark.asyncio
async def test_fake_face_fail():
    r = await FakeKycProvider().fetch_verification(reference_id="FAILFACE-NIN-9")
    assert r.status == "failed" and r.face_match is False
    assert r.face_match_confidence < 70 and r.verification_type == "nin"


@pytest.mark.asyncio
async def test_fake_pending():
    r = await FakeKycProvider().fetch_verification(reference_id="PENDING-BVN-1")
    assert r.status == "pending"


@pytest.mark.asyncio
async def test_fake_unrecognized_reference_defaults_to_success():
    """A real backend-minted reference (no PASS/FAIL*/PENDING marker) must
    succeed, not raise — this lets KycService.start_verification's minted
    references round-trip through confirm_verification end-to-end."""
    r = await FakeKycProvider().fetch_verification(reference_id="KYC-BVN-xyz")
    assert r.status == "success"
    assert r.id_verified and r.liveness_passed and r.face_match
    assert r.verification_type == "bvn"
