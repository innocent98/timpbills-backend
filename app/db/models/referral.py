"""Referral — one row per (referrer, referee) pair.

Sprint 5b. State machine + cap-check logic live in the referral_service
(B2 phase). This module only declares the schema + enum.

The row IS the idempotency guard for the credit pipeline: `attempt_credit`
selects with `FOR UPDATE` and bails when status != "pending". See spec
§5.4 for the pipeline details.
"""
import enum
import uuid

from sqlalchemy import (
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID

from app.db.base import Base
from app.db.mixins import TimestampMixin


class ReferralStatus(str, enum.Enum):
    pending              = "pending"
    attributed           = "attributed"
    credited             = "credited"
    referee_cap_pending  = "referee_cap_pending"
    clawback_pending     = "clawback_pending"
    clawed_back          = "clawed_back"
    voided               = "voided"


class Referral(TimestampMixin, Base):
    __tablename__ = "referrals"
    __table_args__ = (
        # One referrer per referee, forever — enforced at the DB level
        # so concurrent signups can't race a second referral row in.
        UniqueConstraint("referee_user_id", name="uq_referrals_referee"),
        Index("ix_referrals_referrer_status", "referrer_user_id", "status"),
        Index("ix_referrals_status_created", "status", "created_at"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    referrer_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    referee_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    code_used = Column(String(8), nullable=False)

    status = Column(
        Enum(ReferralStatus, name="referral_status_enum"),
        nullable=False,
        default=ReferralStatus.pending,
    )
    void_reason = Column(String(64), nullable=True)

    qualifying_tx_id = Column(
        UUID(as_uuid=True),
        ForeignKey("transactions.id", ondelete="SET NULL"),
        nullable=True,
    )

    attributed_at  = Column(DateTime(timezone=True), nullable=True)
    credited_at    = Column(DateTime(timezone=True), nullable=True)
    clawed_back_at = Column(DateTime(timezone=True), nullable=True)
