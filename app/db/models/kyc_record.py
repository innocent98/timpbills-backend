import uuid

from sqlalchemy import Boolean, Column, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID

from app.db.base import Base
from app.db.mixins import TimestampMixin


class KycRecord(TimestampMixin, Base):
    """Audit trail of every KYC verification attempt (A6 writes these).

    One row per provider call — a user re-attempting a failed tier upgrade
    produces a new row rather than mutating the old one, so support/compliance
    can see the full history. `masked_id` is last-2-digits only (spec §3.5) —
    no raw BVN/NIN/selfie/payload columns live here or anywhere else.
    """

    __tablename__ = "kyc_records"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    verification_type = Column(String(8), nullable=False)
    provider = Column(String(16), nullable=False)
    provider_reference = Column(String, nullable=False, unique=True)
    status = Column(String(12), nullable=False)
    liveness_passed = Column(Boolean, nullable=True)
    face_match = Column(Boolean, nullable=True)
    face_match_confidence = Column(Integer, nullable=True)
    tier_before = Column(Integer, nullable=False)
    tier_after = Column(Integer, nullable=True)
    masked_id = Column(String(16), nullable=True)
    failure_reason = Column(Text, nullable=True)
