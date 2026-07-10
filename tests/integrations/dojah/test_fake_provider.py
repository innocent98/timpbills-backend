from app.integrations.dojah.fake import FakeKycProvider


def test_fake_pass_bvn():
    r = FakeKycProvider().fetch_verification(reference_id="PASS-BVN-123")
    assert r.status == "success" and r.id_verified and r.liveness_passed
    assert r.face_match and r.face_match_confidence >= 70
    assert r.verification_type == "bvn"


def test_fake_face_fail():
    r = FakeKycProvider().fetch_verification(reference_id="FAILFACE-NIN-9")
    assert r.status == "failed" and r.face_match is False
    assert r.face_match_confidence < 70 and r.verification_type == "nin"


def test_fake_pending():
    r = FakeKycProvider().fetch_verification(reference_id="PENDING-BVN-1")
    assert r.status == "pending"
