import uuid

from app.db.models.kyc_record import KycRecord
from app.db.models.user import KycLevel, User


def test_tier3_numeric():
    assert KycLevel.tier_3.numeric == 3


def test_kyc_record_columns():
    cols = {c.name for c in KycRecord.__table__.columns}
    assert {
        "user_id",
        "verification_type",
        "provider",
        "provider_reference",
        "status",
        "liveness_passed",
        "face_match",
        "face_match_confidence",
        "tier_before",
        "tier_after",
        "masked_id",
        "failure_reason",
    } <= cols


def test_kyc_record_round_trip(db_session):
    user = User(
        email="kyc-test@example.com",
        phone="+2348010000099",
        full_name="Kyc Tester",
        password_hash="hashed",
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    record = KycRecord(
        user_id=user.id,
        verification_type="nin",
        provider="smile_id",
        provider_reference=str(uuid.uuid4()),
        status="pending",
        tier_before=0,
        masked_id="**34",
    )
    db_session.add(record)
    db_session.commit()
    db_session.refresh(record)

    assert record.id is not None
    assert record.tier_after is None
    assert record.liveness_passed is None
    assert record.face_match is None
    assert record.face_match_confidence is None
    assert record.failure_reason is None
    assert record.created_at is not None
